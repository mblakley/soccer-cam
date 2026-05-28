"""End-to-end test of the auto-upgrade flow against a real local
copy of ``scripts/serve_test_release.py``.

We spin up uvicorn on an ephemeral port in a background thread so
the test exercises the actual httpx -> network -> FastAPI -> file
streaming path that runs in production -- not just an in-process
ASGI dance. This catches Content-Length, chunked download, and
streaming-response bugs that the unit-test mock layer would miss.

What we verify:

  - The processor resolves the env-var URL, hits the mock server,
    parses GitHub-shaped JSON, downloads the .exe, verifies the
    digest, and reaches the spawn stage.
  - The ``--tamper`` mode (bit-flipped served bytes) refuses to
    spawn -- digest mismatch is the gate.
  - The journal records the full stage list per attempt.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Generator
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
import uvicorn

from scripts.serve_test_release import build_app
from video_grouper.task_processors.update_check_processor import UpdateCheckProcessor

# The whole module needs a real port + background server; opt out of
# the fast default run (`pytest -m "not integration and not e2e"`).
pytestmark = pytest.mark.integration


# Override two autouse fixtures from conftest.py: we need real
# httpx (to hit the in-test uvicorn) and real filesystem reads (to
# digest the downloaded artifact). Pytest resolves fixture names
# closest-first, so these win against the conftest defaults.


@pytest.fixture(autouse=True)
def mock_httpx():
    """No-op override of the conftest's autouse httpx mock."""
    yield


@pytest.fixture(autouse=True)
def mock_file_system():
    """No-op override of the conftest's autouse filesystem mock."""
    yield


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_healthy(port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=0.5)
            if r.status_code == 200:
                return
        except Exception as exc:
            last_err = exc
        time.sleep(0.05)
    raise RuntimeError(f"Server not healthy on port {port}: {last_err}")


def _make_config() -> MagicMock:
    cfg = MagicMock()
    cfg.app.github_repo = "mblakley/soccer-cam"
    cfg.app.update_api_url = None
    cfg.app.auto_update = True
    return cfg


async def _always_idle() -> tuple[bool, str | None]:
    return True, None


@pytest.fixture
def fake_installer(tmp_path) -> Path:
    """Stand-in for VideoGrouperSetup.exe. Real PE header + 3MB
    filler so the streaming download loop iterates a few times."""
    p = tmp_path / "VideoGrouperSetup.exe"
    p.write_bytes(b"MZ" + b"\x00" * (3 * 1024 * 1024))
    return p


@pytest.fixture
def release_server(fake_installer) -> Generator[tuple[str, int]]:
    """Spin up the GitHub-API mock server on an ephemeral port.
    Yields (base_url, port). The server's actual /repos/... route
    uses these to expose the .exe."""
    port = _free_port()
    app = build_app(fake_installer, version="0.3.7")
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", access_log=False
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        _wait_healthy(port)
        yield f"http://127.0.0.1:{port}", port
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture
def tamper_server(fake_installer) -> Generator[tuple[str, int]]:
    """Same as ``release_server`` but with ``--tamper`` semantics:
    served bytes don't match the JSON-advertised digest."""
    port = _free_port()
    app = build_app(fake_installer, version="0.3.7", tamper=True)
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", access_log=False
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        _wait_healthy(port)
        yield f"http://127.0.0.1:{port}", port
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_full_pipeline_against_local_mock(tmp_path, monkeypatch, release_server):
    """End-to-end: detect -> quiesce -> download -> verify -> spawn."""
    base_url, _ = release_server
    monkeypatch.setenv(
        "SOCCER_CAM_UPDATE_API_URL",
        f"{base_url}/repos/mblakley/soccer-cam/releases/latest",
    )

    shutdown = MagicMock()
    proc = UpdateCheckProcessor(
        storage_path=str(tmp_path),
        config=_make_config(),
        current_version="0.3.6",
        quiescence_check=_always_idle,
        shutdown_callback=shutdown,
    )

    with patch("video_grouper.update.update_manager.subprocess.Popen") as mock_popen:
        mock_popen.return_value.pid = 99999
        await proc._run_one_check()

    assert proc._last_check_outcome == "spawned"
    assert proc._pending_version == "0.3.7"
    mock_popen.assert_called_once()
    cmd = mock_popen.call_args[0][0]
    assert cmd[-1] == "/S"
    assert "VideoGrouperSetup.exe" in cmd[0]
    shutdown.assert_called_once()


@pytest.mark.asyncio
async def test_tampered_bytes_block_spawn(tmp_path, monkeypatch, tamper_server):
    """Served bytes don't match the JSON-advertised digest -> the
    processor refuses to spawn."""
    base_url, _ = tamper_server
    monkeypatch.setenv(
        "SOCCER_CAM_UPDATE_API_URL",
        f"{base_url}/repos/mblakley/soccer-cam/releases/latest",
    )

    shutdown = MagicMock()
    proc = UpdateCheckProcessor(
        storage_path=str(tmp_path),
        config=_make_config(),
        current_version="0.3.6",
        quiescence_check=_always_idle,
        shutdown_callback=shutdown,
    )

    with patch("video_grouper.update.update_manager.subprocess.Popen") as mock_popen:
        await proc._run_one_check()

    assert proc._last_check_outcome == "failed"
    assert proc._last_check_error == "digest_mismatch"
    mock_popen.assert_not_called()
    shutdown.assert_not_called()


@pytest.mark.asyncio
async def test_journal_records_full_stage_list(tmp_path, monkeypatch, release_server):
    base_url, _ = release_server
    monkeypatch.setenv(
        "SOCCER_CAM_UPDATE_API_URL",
        f"{base_url}/repos/mblakley/soccer-cam/releases/latest",
    )

    proc = UpdateCheckProcessor(
        storage_path=str(tmp_path),
        config=_make_config(),
        current_version="0.3.6",
        quiescence_check=_always_idle,
    )

    with patch("video_grouper.update.update_manager.subprocess.Popen") as mock_popen:
        mock_popen.return_value.pid = 12345
        await proc._run_one_check()

    import json

    journal = (tmp_path / "logs" / "update_history.jsonl").read_text()
    entry = json.loads(journal.splitlines()[0])
    assert entry["outcome"] == "spawned"
    assert entry["from_version"] == "0.3.6"
    assert entry["to_version"] == "0.3.7"
    assert entry["stages_completed"] == [
        "check",
        "quiescence",
        "download",
        "verify",
        "spawn",
    ]
    assert entry["digest_expected"] == entry["digest_actual"]


@pytest.mark.asyncio
async def test_skip_when_already_current(tmp_path, monkeypatch, release_server):
    """Server says v0.3.7 -- we're already on v0.3.7 -- no update."""
    base_url, _ = release_server
    monkeypatch.setenv(
        "SOCCER_CAM_UPDATE_API_URL",
        f"{base_url}/repos/mblakley/soccer-cam/releases/latest",
    )

    proc = UpdateCheckProcessor(
        storage_path=str(tmp_path),
        config=_make_config(),
        current_version="0.3.7",  # same as the server's release
        quiescence_check=_always_idle,
    )

    with patch("video_grouper.update.update_manager.subprocess.Popen") as mock_popen:
        await proc._run_one_check()

    assert proc._last_check_outcome == "skipped"
    assert proc._pending_version is None
    mock_popen.assert_not_called()
