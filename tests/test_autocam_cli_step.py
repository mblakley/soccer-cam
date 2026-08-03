"""Unit tests for the ``autocam_cli`` pipeline step.

No real process is ever started: ``_spawn`` is patched to return a fake process
whose stdout replays recorded-shape progress lines. What's under test is the
step's contract with that process — the argv it builds, how it reads progress,
how it maps exit codes, and that it never lets a partial output survive a
failure.

The output-validation gate runs for real (real files, real moov scan), so these
tests override the repo's autouse ``mock_file_system`` fixture, which otherwise
pins ``os.path.getsize`` to 1 MB and would make every validation verdict a
tautology.
"""

from __future__ import annotations

import asyncio

import pytest

# Importing register_steps registers all built-ins as a side effect.
import video_grouper.pipeline.register_steps  # noqa: F401
from video_grouper.pipeline import create_step, get_step_meta
from video_grouper.pipeline.base import StepContext
from video_grouper.pipeline.manifest import PipelineManifest
from video_grouper.pipeline.steps.autocam_cli import (
    AutocamCliStep,
    AutocamCliStepConfig,
    _failure_reason,
    _format_progress,
    _kill_tree,
    build_command,
    parse_progress,
    resolve_executable,
)


@pytest.fixture(autouse=True)
def mock_file_system():
    """Override the conftest autouse mock; the validator needs real os.path I/O."""
    yield None


# ----------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------


class _FakeStdout:
    def __init__(self, lines: list[bytes]):
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        return b""  # EOF


class _HangingStdout:
    async def readline(self) -> bytes:
        await asyncio.sleep(3600)
        return b""


class _FakeProc:
    """Minimal stand-in for ``asyncio.subprocess.Process``."""

    def __init__(self, stdout, rc: int = 0, pid: int = 4242):
        self.stdout = stdout
        self.pid = pid
        self.returncode: int | None = None
        self._rc = rc
        self.killed = False

    async def wait(self) -> int:
        self.returncode = self._rc
        return self._rc

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


# Verbatim shapes captured from a real AutocamCLI/Core 3.1.1 run log. Note the
# HH:MM:SS durations, that `processed` need not reach `total` on success, and
# that the terminal line carries `eta=N/A`.
PROGRESS_LINES = [
    b"[cli] starting\n",
    b"status=Running processed=1 dropped=0 total=547 elapsed=00:00:01 "
    b"msPerFrame=117.25 eta=00:01:04\n",
    b"status=Running processed=522 dropped=0 total=547 elapsed=00:01:04 "
    b"msPerFrame=117.25 eta=00:00:02\n",
    b"status=Succeeded processed=532 dropped=0 total=547 elapsed=00:01:07 "
    b"msPerFrame=127.43 eta=N/A\n",
]

# A real failing run: the cause, then a Failed progress line with N/A counters,
# then telemetry whose event name contains "RuntimeError".
FAILURE_LINES = [
    b"[cli] Error: Could not open input 'C:/x.mp4'. It may be unavailable, "
    b"in use, or in an unsupported format.\n",
    b"status=Failed processed=0 dropped=0 total=N/A elapsed=00:00:00 "
    b"msPerFrame=0.00 eta=N/A\n",
    b"[cli] status: Failed\n",
    b"[logger] sending Autocam.Core.RuntimeError to https://example.invalid/post/x\n",
    b"[logger] event sent to remote: Autocam.Core.RuntimeError\n",
]


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _ctx(tmp_path):
    return StepContext(group_dir=tmp_path, team_name=None, storage_path=tmp_path)


_FTYP = bytes.fromhex(
    "000000206674797069736f6d0000020069736f6d69736f32617663316d703431"
)


def _write_valid_output(path, size_mb: int = 11) -> None:
    """ftyp + moov + padding, over the validator's 10 MB absolute floor."""
    moov = bytes.fromhex("00000010") + b"moov" + b"\x00" * 8
    with open(path, "wb") as f:
        f.write(_FTYP + moov)
        f.write(b"\x00" * (size_mb * 1024 * 1024))


def _write_partial_output(path, size_mb: int = 11) -> None:
    """The crashed-render shape: ftyp present, no moov, over the size floor."""
    with open(path, "wb") as f:
        f.write(_FTYP)
        f.write(b"\x00" * (size_mb * 1024 * 1024))


def _make_step(tmp_path, exe, **overrides) -> AutocamCliStep:
    cfg = AutocamCliStepConfig(executable=str(exe), **overrides)
    return AutocamCliStep(config=cfg)


def _manifest(tmp_path, input_path, output_path) -> PipelineManifest:
    return PipelineManifest.load_or_init(tmp_path, str(input_path), str(output_path))


def _fake_exe(tmp_path):
    exe = tmp_path / "AutocamCLI.exe"
    exe.write_bytes(b"MZ")
    return exe


# ----------------------------------------------------------------------
# Registration + contract
# ----------------------------------------------------------------------


def test_registered_with_service_runtime_and_gpu_resource():
    """The whole point of the step: Session-0 safe, no autocam_ui contention."""
    meta = get_step_meta("autocam_cli")
    assert meta.runtime == "service"
    assert meta.resources == ("gpu",)
    assert meta.requires == ()
    assert meta.available is True


def test_step_consumes_input_and_produces_output():
    step = create_step("autocam_cli", {"executable": "AutocamCLI.exe"})
    assert isinstance(step, AutocamCliStep)
    assert step.consumes == ("input_path",)
    assert step.produces == ("output_path",)


def test_gui_step_still_registered_alongside():
    """This change adds a step; it must not disturb the GUI path."""
    gui = get_step_meta("autocam")
    assert gui.runtime == "tray"
    assert gui.resources == ("autocam_ui",)


def test_step_imports_without_the_desktop_automation_stack():
    """The load-bearing property of this step: it must import in the Windows
    service, where pywinauto/win32gui are unavailable or unusable. Verified in a
    clean interpreter with both modules poisoned so importing them raises.

    This is why the MP4 validation helpers moved to video_grouper.utils.mp4 —
    importing them from tray.autocam_automation would drag pywinauto in.
    """
    import subprocess
    import sys
    import textwrap
    from pathlib import Path

    code = textwrap.dedent("""
        import sys
        sys.modules["pywinauto"] = None
        sys.modules["win32gui"] = None
        from video_grouper.pipeline.steps.autocam_cli import AutocamCliStep
        from video_grouper.utils.mp4 import validate_video_output  # noqa: F401
        assert AutocamCliStep.runtime == "service"
        assert "video_grouper.tray.autocam_automation" not in sys.modules
        print("ok")
    """)
    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


# ----------------------------------------------------------------------
# Argument construction
# ----------------------------------------------------------------------


def test_minimal_command_is_the_proven_invocation():
    """With nothing configured beyond the executable, argv is exactly the
    minimal command — no unmeasured flag on the default path."""
    cfg = AutocamCliStepConfig(executable="C:/ac/AutocamCLI.exe")
    assert build_command(cfg, "C:/in.mp4", "C:/out.mp4") == [
        "C:/ac/AutocamCLI.exe",
        "--mode",
        "basic",
        "--input",
        "C:/in.mp4",
        "--output",
        "C:/out.mp4",
    ]


def test_optional_flags_are_appended_when_configured():
    cfg = AutocamCliStepConfig(
        executable="AutocamCLI.exe",
        execution_provider="DML",
        video_bitrate="12M",
        output_resolution="1920x1080",
        overlay_icon="C:/branding/team.png",
        overlay_scale=0.5,
        extra_args="--enable-tracking-log, --start, 30",
    )
    argv = build_command(cfg, "in.mp4", "out.mp4")
    assert argv[:7] == [
        "AutocamCLI.exe",
        "--mode",
        "basic",
        "--input",
        "in.mp4",
        "--output",
        "out.mp4",
    ]
    assert argv[7:] == [
        "--execution-provider",
        "dml",  # normalized by the validator
        "--video-bitrate",
        "12M",
        "--output-resolution",
        "1920x1080",
        "--overlay-icon",
        "C:/branding/team.png",
        "--overlay-scale",
        "0.5",
        # extra_args always last so it adds rather than replaces
        "--enable-tracking-log",
        "--start",
        "30",
    ]


def test_overlay_icon_alone_is_enough():
    """overlay_scale is independent — a logo with the vendor's default scale."""
    cfg = AutocamCliStepConfig(executable="x", overlay_icon="logo.png")
    argv = build_command(cfg, "in", "out")
    assert argv[-2:] == ["--overlay-icon", "logo.png"]
    assert "--overlay-scale" not in argv


def test_overlay_scale_zero_is_still_passed():
    """0.0 is falsy but meaningful; the None check must not swallow it."""
    cfg = AutocamCliStepConfig(executable="x", overlay_scale=0.0)
    assert "--overlay-scale" in build_command(cfg, "in", "out")


def test_mode_defaults_to_basic():
    assert AutocamCliStepConfig(executable="x").mode == "basic"


def test_stitch_mode_is_refused():
    """The CLI's other mode takes --input-left/--input-right/--stitch-maps, a
    command shape this step doesn't build; accepting it would spawn a process
    that cannot succeed."""
    with pytest.raises(ValueError, match="stitch"):
        AutocamCliStepConfig(executable="x", mode="stitch")


def test_coreml_is_an_accepted_execution_provider():
    """The binary's own help lists auto, cpu, cuda, dml, coreml. An earlier
    whitelist omitted coreml and would have rejected a valid config."""
    cfg = AutocamCliStepConfig(executable="x", execution_provider="coreml")
    assert cfg.execution_provider == "coreml"
    assert build_command(cfg, "in", "out")[-1] == "coreml"


def test_overlay_scale_out_of_range_is_rejected():
    """Documented as 0..1, relative to the output video width."""
    with pytest.raises(ValueError, match="overlay_scale"):
        AutocamCliStepConfig(executable="x", overlay_scale=1.5)


def test_extra_args_accepts_a_python_literal_list():
    cfg = AutocamCliStepConfig(executable="x", extra_args="['--foo', '--bar']")
    assert cfg.extra_args == ["--foo", "--bar"]


def test_extra_args_empty_string_is_empty_list():
    assert AutocamCliStepConfig(executable="x", extra_args="").extra_args == []


def test_bad_execution_provider_fails_at_config_time():
    """A typo must fail loudly when the step is constructed, not as an opaque
    argument-error exit minutes into a render."""
    with pytest.raises(ValueError, match="execution_provider"):
        AutocamCliStepConfig(executable="x", execution_provider="cudaa")


def test_empty_execution_provider_means_unset():
    cfg = AutocamCliStepConfig(executable="x", execution_provider="")
    assert cfg.execution_provider is None
    assert "--execution-provider" not in build_command(cfg, "in", "out")


# ----------------------------------------------------------------------
# Progress parsing
# ----------------------------------------------------------------------


def test_parse_progress_extracts_tokens():
    """Verbatim line from a real 3.1.1 run — HH:MM:SS durations included."""
    tokens = parse_progress(
        "status=Running processed=522 dropped=0 total=547 elapsed=00:01:04 "
        "msPerFrame=117.25 eta=00:00:02"
    )
    assert tokens == {
        "status": "Running",
        "processed": "522",
        "dropped": "0",
        "total": "547",
        "elapsed": "00:01:04",
        "msPerFrame": "117.25",
        "eta": "00:00:02",
    }


def test_parse_progress_handles_na_counters():
    """A failed run reports total=N/A and eta=N/A; parsing must not choke and
    the formatter must not divide by a non-number."""
    line = (
        "status=Failed processed=0 dropped=0 total=N/A elapsed=00:00:00 "
        "msPerFrame=0.00 eta=N/A"
    )
    tokens = parse_progress(line)
    assert tokens is not None
    assert tokens["total"] == "N/A"
    rendered = _format_progress(tokens)
    assert "status=Failed" in rendered
    assert "msPerFrame=0.00" in rendered


def test_format_progress_keeps_every_reported_field():
    """No allowlist: whatever throughput field the build reports survives into
    the log. An earlier version hardcoded a key set and dropped others."""
    rendered = _format_progress(
        {"status": "Running", "processed": "10", "total": "20", "fps": "7.8"}
    )
    assert "50.0%" in rendered
    assert "fps=7.8" in rendered


def test_parse_progress_ignores_non_progress_lines():
    assert parse_progress("[cli] Error: could not open input") is None
    assert parse_progress("") is None


def test_parse_progress_reads_terminal_status():
    tokens = parse_progress("status=Succeeded processed=532 dropped=0 total=547")
    assert tokens is not None
    assert tokens["status"] == "Succeeded"


def test_failure_reason_prefers_the_cli_error_over_telemetry():
    """Regression: telemetry lines contain "RuntimeError", so a naive
    last-line-mentioning-error scan reported a telemetry URL as the cause."""
    tail = [ln.decode().strip() for ln in FAILURE_LINES if b"status=" not in ln]
    reason = _failure_reason(tail)
    assert reason.startswith("[cli] Error: Could not open input")


def test_failure_reason_skips_telemetry_when_no_cli_error():
    tail = [
        "something odd happened",
        "[logger] sending Autocam.Core.RuntimeError to https://example.invalid/x",
    ]
    assert _failure_reason(tail) == "something odd happened"


def test_failure_reason_with_no_output():
    assert _failure_reason([]) == "(no diagnostic output)"


# ----------------------------------------------------------------------
# Executable resolution
# ----------------------------------------------------------------------


def test_resolve_executable_prefers_an_existing_path(tmp_path):
    exe = _fake_exe(tmp_path)
    assert resolve_executable(str(exe)) == str(exe)


def test_resolve_executable_returns_none_when_missing(tmp_path):
    assert resolve_executable(str(tmp_path / "nope.exe")) is None
    assert resolve_executable(None) is None
    assert resolve_executable("") is None


# ----------------------------------------------------------------------
# run(): success
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_success_validates_output_and_records_artifact(tmp_path, monkeypatch):
    exe = _fake_exe(tmp_path)
    src = tmp_path / "in.mp4"
    src.write_bytes(b"\x00" * 1024)
    out = tmp_path / "out.mp4"

    seen: dict[str, list[str]] = {}

    async def fake_spawn(argv):
        seen["argv"] = argv
        _write_valid_output(out)  # the "render"
        return _FakeProc(_FakeStdout(PROGRESS_LINES), rc=0)

    monkeypatch.setattr(
        "video_grouper.pipeline.steps.autocam_cli._spawn", fake_spawn, raising=True
    )

    step = _make_step(tmp_path, exe)
    manifest = _manifest(tmp_path, src, out)
    assert await step.run(manifest, _ctx(tmp_path)) is True

    assert seen["argv"][0] == str(exe)
    assert "--input" in seen["argv"] and str(src) in seen["argv"]
    assert manifest.get("output_path") == str(out)
    assert out.exists()


@pytest.mark.asyncio
async def test_run_uses_the_resolved_executable_path(tmp_path, monkeypatch):
    """The configured value is resolved once; argv[0] is the resolved path."""
    exe = _fake_exe(tmp_path)
    src = tmp_path / "in.mp4"
    src.write_bytes(b"\x00" * 1024)
    out = tmp_path / "out.mp4"
    captured: list[list[str]] = []

    async def fake_spawn(argv):
        captured.append(argv)
        _write_valid_output(out)
        return _FakeProc(_FakeStdout(PROGRESS_LINES), rc=0)

    monkeypatch.setattr(
        "video_grouper.pipeline.steps.autocam_cli._spawn", fake_spawn, raising=True
    )
    step = _make_step(tmp_path, exe, execution_provider="cuda")
    assert await step.run(_manifest(tmp_path, src, out), _ctx(tmp_path)) is True
    assert captured[0][:3] == [str(exe), "--mode", "basic"]
    assert captured[0][-2:] == ["--execution-provider", "cuda"]


# ----------------------------------------------------------------------
# run(): failure mapping
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nonzero_exit_fails_and_deletes_the_partial(tmp_path, monkeypatch):
    exe = _fake_exe(tmp_path)
    src = tmp_path / "in.mp4"
    src.write_bytes(b"\x00" * 1024)
    out = tmp_path / "out.mp4"

    async def fake_spawn(argv):
        _write_partial_output(out)  # crashed mid-render
        return _FakeProc(_FakeStdout(FAILURE_LINES), rc=1)

    monkeypatch.setattr(
        "video_grouper.pipeline.steps.autocam_cli._spawn", fake_spawn, raising=True
    )
    step = _make_step(tmp_path, exe)
    assert await step.run(_manifest(tmp_path, src, out), _ctx(tmp_path)) is False
    assert not out.exists(), "a partial output must not survive a failed run"


@pytest.mark.asyncio
async def test_argument_error_exit_fails(tmp_path, monkeypatch):
    """Exit 2 = argument error: no output was ever created."""
    exe = _fake_exe(tmp_path)
    src = tmp_path / "in.mp4"
    src.write_bytes(b"\x00" * 1024)
    out = tmp_path / "out.mp4"

    async def fake_spawn(argv):
        return _FakeProc(_FakeStdout([b"[cli] Error: unknown option --nope\n"]), rc=2)

    monkeypatch.setattr(
        "video_grouper.pipeline.steps.autocam_cli._spawn", fake_spawn, raising=True
    )
    step = _make_step(tmp_path, exe)
    assert await step.run(_manifest(tmp_path, src, out), _ctx(tmp_path)) is False
    assert not out.exists()


@pytest.mark.asyncio
async def test_exit_zero_with_partial_output_is_rejected(tmp_path, monkeypatch):
    """The 2026-06-01 failure mode, ported: a clean exit code is not evidence
    the container was finalized. The moov gate is authoritative."""
    exe = _fake_exe(tmp_path)
    src = tmp_path / "in.mp4"
    src.write_bytes(b"\x00" * 1024)
    out = tmp_path / "out.mp4"

    async def fake_spawn(argv):
        _write_partial_output(out)
        return _FakeProc(_FakeStdout(PROGRESS_LINES), rc=0)

    monkeypatch.setattr(
        "video_grouper.pipeline.steps.autocam_cli._spawn", fake_spawn, raising=True
    )
    step = _make_step(tmp_path, exe)
    assert await step.run(_manifest(tmp_path, src, out), _ctx(tmp_path)) is False
    assert not out.exists()


@pytest.mark.asyncio
async def test_mkv_output_is_accepted(tmp_path, monkeypatch):
    """AutoCam's GUI defaults its destination to .mkv and
    get_ball_tracking_io_paths() already offers output_ext="mkv", so a
    Matroska output is reachable. An MP4-only moov gate would reject a good
    render as "no moov atom"."""
    exe = _fake_exe(tmp_path)
    src = tmp_path / "in.mp4"
    src.write_bytes(b"\x00" * 1024)
    out = tmp_path / "out.mkv"

    async def fake_spawn(argv):
        with open(out, "wb") as f:
            f.write(bytes.fromhex("1a45dfa3"))  # EBML header ID
            f.write(b"\x00" * (11 * 1024 * 1024))
        return _FakeProc(_FakeStdout(PROGRESS_LINES), rc=0)

    monkeypatch.setattr(
        "video_grouper.pipeline.steps.autocam_cli._spawn", fake_spawn, raising=True
    )
    step = _make_step(tmp_path, exe)
    assert await step.run(_manifest(tmp_path, src, out), _ctx(tmp_path)) is True
    assert out.exists()


@pytest.mark.asyncio
async def test_mkv_output_without_ebml_header_is_rejected(tmp_path, monkeypatch):
    exe = _fake_exe(tmp_path)
    src = tmp_path / "in.mp4"
    src.write_bytes(b"\x00" * 1024)
    out = tmp_path / "out.mkv"

    async def fake_spawn(argv):
        out.write_bytes(b"\x00" * (11 * 1024 * 1024))  # no EBML magic
        return _FakeProc(_FakeStdout(PROGRESS_LINES), rc=0)

    monkeypatch.setattr(
        "video_grouper.pipeline.steps.autocam_cli._spawn", fake_spawn, raising=True
    )
    step = _make_step(tmp_path, exe)
    assert await step.run(_manifest(tmp_path, src, out), _ctx(tmp_path)) is False
    assert not out.exists()


@pytest.mark.asyncio
async def test_exit_zero_with_no_output_at_all_is_rejected(tmp_path, monkeypatch):
    exe = _fake_exe(tmp_path)
    src = tmp_path / "in.mp4"
    src.write_bytes(b"\x00" * 1024)
    out = tmp_path / "out.mp4"

    async def fake_spawn(argv):
        return _FakeProc(_FakeStdout(PROGRESS_LINES), rc=0)

    monkeypatch.setattr(
        "video_grouper.pipeline.steps.autocam_cli._spawn", fake_spawn, raising=True
    )
    step = _make_step(tmp_path, exe)
    assert await step.run(_manifest(tmp_path, src, out), _ctx(tmp_path)) is False


@pytest.mark.asyncio
async def test_missing_executable_fails_before_spawning(tmp_path, monkeypatch):
    src = tmp_path / "in.mp4"
    src.write_bytes(b"\x00" * 1024)
    spawned = []

    async def fake_spawn(argv):
        spawned.append(argv)
        raise AssertionError("must not spawn")

    monkeypatch.setattr(
        "video_grouper.pipeline.steps.autocam_cli._spawn", fake_spawn, raising=True
    )
    step = _make_step(tmp_path, tmp_path / "nope.exe")
    manifest = _manifest(tmp_path, src, tmp_path / "out.mp4")
    assert await step.run(manifest, _ctx(tmp_path)) is False
    assert spawned == []


@pytest.mark.asyncio
async def test_unset_executable_fails(tmp_path):
    src = tmp_path / "in.mp4"
    src.write_bytes(b"\x00" * 1024)
    step = AutocamCliStep(config=AutocamCliStepConfig())
    manifest = _manifest(tmp_path, src, tmp_path / "out.mp4")
    assert await step.run(manifest, _ctx(tmp_path)) is False


@pytest.mark.asyncio
async def test_missing_input_fails_before_spawning(tmp_path, monkeypatch):
    exe = _fake_exe(tmp_path)
    spawned = []

    async def fake_spawn(argv):
        spawned.append(argv)
        raise AssertionError("must not spawn")

    monkeypatch.setattr(
        "video_grouper.pipeline.steps.autocam_cli._spawn", fake_spawn, raising=True
    )
    step = _make_step(tmp_path, exe)
    manifest = _manifest(tmp_path, tmp_path / "gone.mp4", tmp_path / "out.mp4")
    assert await step.run(manifest, _ctx(tmp_path)) is False
    assert spawned == []


@pytest.mark.asyncio
async def test_spawn_oserror_is_a_clean_failure(tmp_path, monkeypatch):
    exe = _fake_exe(tmp_path)
    src = tmp_path / "in.mp4"
    src.write_bytes(b"\x00" * 1024)

    async def fake_spawn(argv):
        raise OSError("access denied")

    monkeypatch.setattr(
        "video_grouper.pipeline.steps.autocam_cli._spawn", fake_spawn, raising=True
    )
    step = _make_step(tmp_path, exe)
    manifest = _manifest(tmp_path, src, tmp_path / "out.mp4")
    assert await step.run(manifest, _ctx(tmp_path)) is False


# ----------------------------------------------------------------------
# run(): stall guard + cancellation
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stalled_process_is_killed_and_fails(tmp_path, monkeypatch):
    exe = _fake_exe(tmp_path)
    src = tmp_path / "in.mp4"
    src.write_bytes(b"\x00" * 1024)
    out = tmp_path / "out.mp4"
    proc = _FakeProc(_HangingStdout(), rc=0)
    killed: list[int] = []

    async def fake_spawn(argv):
        _write_partial_output(out)
        return proc

    monkeypatch.setattr(
        "video_grouper.pipeline.steps.autocam_cli._spawn", fake_spawn, raising=True
    )
    monkeypatch.setattr(
        "video_grouper.pipeline.steps.autocam_cli._kill_tree",
        lambda p: killed.append(p.pid),
        raising=True,
    )

    step = _make_step(tmp_path, exe, progress_timeout_seconds=1)
    assert await step.run(_manifest(tmp_path, src, out), _ctx(tmp_path)) is False
    assert killed == [proc.pid]
    assert not out.exists()


@pytest.mark.asyncio
async def test_cancellation_kills_the_child_and_propagates(tmp_path, monkeypatch):
    """Service shutdown must not orphan an hour-long GPU render."""
    exe = _fake_exe(tmp_path)
    src = tmp_path / "in.mp4"
    src.write_bytes(b"\x00" * 1024)
    out = tmp_path / "out.mp4"
    proc = _FakeProc(_HangingStdout(), rc=0)
    killed: list[int] = []

    async def fake_spawn(argv):
        return proc

    monkeypatch.setattr(
        "video_grouper.pipeline.steps.autocam_cli._spawn", fake_spawn, raising=True
    )
    monkeypatch.setattr(
        "video_grouper.pipeline.steps.autocam_cli._kill_tree",
        lambda p: killed.append(p.pid),
        raising=True,
    )

    step = _make_step(tmp_path, exe)
    task = asyncio.create_task(step.run(_manifest(tmp_path, src, out), _ctx(tmp_path)))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert killed == [proc.pid]


def test_kill_tree_is_a_noop_for_an_exited_process(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "video_grouper.pipeline.steps.autocam_cli.subprocess.run",
        lambda *a, **k: calls.append(a),
        raising=True,
    )
    proc = _FakeProc(_FakeStdout([]), rc=0)
    proc.returncode = 0
    _kill_tree(proc)
    assert calls == []
    assert proc.killed is False


def test_kill_tree_taskkills_a_live_process(monkeypatch):
    """The kill is by PID — no coupling to a process image name, which is the
    exact thing that broke the GUI path when the vendor renamed its binary."""
    import sys

    if sys.platform != "win32":
        pytest.skip("taskkill path is Windows-only")
    calls: list[tuple] = []
    monkeypatch.setattr(
        "video_grouper.pipeline.steps.autocam_cli.subprocess.run",
        lambda *a, **k: calls.append(a),
        raising=True,
    )
    proc = _FakeProc(_FakeStdout([]), rc=0)
    _kill_tree(proc)
    assert calls, "expected a taskkill invocation"
    assert calls[0][0] == ["taskkill", "/F", "/T", "/PID", str(proc.pid)]
