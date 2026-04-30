"""Tests for the remote-worker entry point (``python -m video_grouper.worker``).

The worker uses ``httpx.AsyncClient`` against a master URL. We mount the
real master FastAPI app behind ``httpx.ASGITransport`` so register /
poll / heartbeat / complete travel the same code path they would in
production, no network or sockets involved.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from video_grouper.utils.config import TTTConfig
from video_grouper.web.auth_server import create_app
from video_grouper.web.worker_api import enqueue_task
from video_grouper.worker.__main__ import (
    _heartbeat_loop,
    _poll_once,
    _register,
)

MASTER_URL = "http://localhost:8765"
SAME_ORIGIN = {"origin": MASTER_URL}


# tests/conftest.py autouse-patches httpx.AsyncClient with an AsyncMock for
# unit tests that don't want network. The worker tests intentionally use
# the real httpx + ASGITransport to exercise the master in-process, so we
# override the conftest fixture with a no-op for this module.
@pytest.fixture(autouse=True)
def mock_httpx():
    yield


@pytest.fixture
def storage(tmp_path):
    return tmp_path


def _master_client(storage_path) -> httpx.AsyncClient:
    """An AsyncClient wired straight to the master's FastAPI app via
    ASGITransport. Sets the same-origin Origin header so the auth_server
    middleware accepts state-changing methods."""
    app = create_app(TTTConfig(), str(storage_path), node_role="master")
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport,
        base_url=MASTER_URL,
        headers=SAME_ORIGIN,
        timeout=10.0,
    )


@pytest.mark.asyncio
async def test_register_advertises_capabilities(storage):
    async with _master_client(storage) as client:
        token = await _register(client, MASTER_URL, "worker1", ["combine", "trim"])

    assert token
    registry = json.loads((storage / "workers" / "registry.json").read_text())
    assert registry["workers"]["worker1"]["capabilities"] == ["combine", "trim"]


@pytest.mark.asyncio
async def test_poll_once_returns_false_when_queue_empty(storage):
    async with _master_client(storage) as client:
        token = await _register(client, MASTER_URL, "w", ["combine"])
        client.headers["Authorization"] = f"Bearer {token}"

        processed = await _poll_once(client, MASTER_URL, heartbeat_interval=999)

    assert processed is False


@pytest.mark.asyncio
async def test_poll_once_claims_runs_completes(storage):
    async with _master_client(storage) as client:
        token = await _register(client, MASTER_URL, "w", ["combine"])
        client.headers["Authorization"] = f"Bearer {token}"

        task_id = enqueue_task(storage, "combine", {"group_dir": "x"})

        processed = await _poll_once(client, MASTER_URL, heartbeat_interval=999)

    assert processed is True
    registry = json.loads((storage / "workers" / "registry.json").read_text())
    task = registry["tasks"][task_id]
    assert task["status"] == "complete"
    assert task["assigned_to"] == "w"
    # The stub runner reports its task type back as the output so we can
    # tell a real run happened end-to-end.
    assert task["outputs"] == {"runner": "stub", "task_type": "combine"}


@pytest.mark.asyncio
async def test_poll_once_skips_capability_mismatch(storage):
    """A worker only advertising 'combine' must not claim 'ball_tracking'."""
    async with _master_client(storage) as client:
        token = await _register(client, MASTER_URL, "cpu-only", ["combine"])
        client.headers["Authorization"] = f"Bearer {token}"

        task_id = enqueue_task(storage, "ball_tracking", {})

        processed = await _poll_once(client, MASTER_URL, heartbeat_interval=999)

    assert processed is False
    registry = json.loads((storage / "workers" / "registry.json").read_text())
    assert registry["tasks"][task_id]["status"] == "queued"


@pytest.mark.asyncio
async def test_heartbeat_loop_pings_master_during_long_task(storage):
    """A long task must keep its heartbeat fresh so the master doesn't
    mark it stalled. We simulate by running _heartbeat_loop directly
    around an asyncio.sleep that's longer than the heartbeat interval,
    then check the master observed the heartbeat."""
    async with _master_client(storage) as client:
        token = await _register(client, MASTER_URL, "w", ["combine"])
        client.headers["Authorization"] = f"Bearer {token}"

        task_id = enqueue_task(storage, "combine", {})
        # Claim it so heartbeats are accepted.
        claim = await client.get("/api/work/next")
        assert claim.status_code == 200

        before = json.loads((storage / "workers" / "registry.json").read_text())
        before_hb = before["tasks"][task_id].get("last_heartbeat")

        stop = asyncio.Event()
        hb_task = asyncio.create_task(
            _heartbeat_loop(client, MASTER_URL, task_id, interval=0.05, stop=stop)
        )
        # Let the loop fire at least twice.
        await asyncio.sleep(0.18)
        stop.set()
        await hb_task

        after = json.loads((storage / "workers" / "registry.json").read_text())
        after_hb = after["tasks"][task_id]["last_heartbeat"]

    # Master saw a heartbeat that didn't exist before.
    assert before_hb is None
    assert after_hb is not None
    assert after_hb > 0
