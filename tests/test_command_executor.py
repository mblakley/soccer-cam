"""Tests for the CommandExecutor module."""

from unittest.mock import MagicMock

import pytest

from video_grouper.api_integrations.command_executor import (
    SUPPORTED_COMMANDS,
    CommandExecutor,
)
from video_grouper.integrations.device_link import DeviceCommand


def _cmd(command_type: str = "", parameters: dict | None = None) -> DeviceCommand:
    return DeviceCommand("cmd-1", command_type, parameters or {})


class TestCommandExecutor:
    def setup_method(self):
        self.app = MagicMock()
        self.executor = CommandExecutor(self.app)

    @pytest.mark.asyncio
    async def test_unknown_command(self):
        result = await self.executor.execute(_cmd("unknown"))
        assert not result["success"]
        assert "unknown" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_missing_command_type(self):
        result = await self.executor.execute(_cmd())
        assert not result["success"]

    @pytest.mark.asyncio
    async def test_restart_command(self):
        result = await self.executor.execute(_cmd("restart"))
        assert result["success"]

    @pytest.mark.asyncio
    async def test_start_recording_command(self):
        result = await self.executor.execute(_cmd("start_recording"))
        assert result["success"]

    @pytest.mark.asyncio
    async def test_stop_recording_command(self):
        result = await self.executor.execute(_cmd("stop_recording"))
        assert result["success"]

    @pytest.mark.asyncio
    async def test_delete_old_files(self):
        result = await self.executor.execute(
            _cmd("delete_old_files", {"older_than_days": 7})
        )
        assert result["success"]
        assert "7" in result["message"]

    @pytest.mark.asyncio
    async def test_delete_old_files_default_days(self):
        result = await self.executor.execute(_cmd("delete_old_files", {}))
        assert result["success"]
        assert "30" in result["message"]

    @pytest.mark.asyncio
    async def test_delete_old_files_no_parameters(self):
        result = await self.executor.execute(_cmd("delete_old_files"))
        assert result["success"]

    @pytest.mark.asyncio
    async def test_all_supported_commands_succeed(self):
        # skip_download needs its parameters; it has its own tests below.
        for cmd_type in SUPPORTED_COMMANDS - {"skip_download"}:
            result = await self.executor.execute(_cmd(cmd_type))
            assert result["success"], f"Command {cmd_type!r} should succeed"

    @pytest.mark.asyncio
    async def test_result_always_has_success_and_message(self):
        result = await self.executor.execute(_cmd("restart"))
        assert "success" in result
        assert "message" in result

    @pytest.mark.asyncio
    async def test_unknown_command_result_has_success_and_message(self):
        result = await self.executor.execute(_cmd("eject_disc"))
        assert "success" in result
        assert "message" in result
        assert result["success"] is False


# ---------------------------------------------------------------------------
# skip_download: a person in TTT gives up on a download that keeps failing
# ---------------------------------------------------------------------------

GROUP = "2026.07.01-10.00.00"


def _storage_with(tmp_path, status="download_failed"):
    import json

    group_dir = tmp_path / GROUP
    group_dir.mkdir()  # Path.mkdir: conftest patches os.makedirs
    (group_dir / "state.json").write_text(
        json.dumps(
            {
                "status": "pending",
                "files": {
                    str(group_dir / "bad.mp4"): {
                        "status": status,
                        "failed_attempts": 3,
                        "start_time": "2026-07-01T10:00:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return group_dir / "state.json"


def _skip(**params):
    base = {
        "machine_id": "m-1",
        "group_dir": GROUP,
        "file_name": "bad.mp4",
        "reason": "corrupt on the SD card",
        "requested_by": "coach@example.com",
    }
    base.update(params)
    return _cmd("skip_download", {k: v for k, v in base.items() if v is not ...})


def _executor_for(storage):
    app = MagicMock()
    app.storage_path = str(storage)
    return CommandExecutor(app)


def _entry(state_file):
    import json

    data = json.loads(state_file.read_text(encoding="utf-8"))
    return next(iter(data["files"].values()))


@pytest.mark.asyncio
async def test_skip_download_skips_the_file_for_the_ttt_user(tmp_path):
    state_file = _storage_with(tmp_path)
    result = await _executor_for(tmp_path).execute(_skip())
    assert result["success"] is True
    assert result["changed"] is True
    assert "bad.mp4" in result["message"]
    entry = _entry(state_file)
    assert entry["status"] == "skipped"
    assert entry["skipped_by"] == "TTT: coach@example.com"
    assert entry["skip_reason"] == "corrupt on the SD card"


@pytest.mark.asyncio
async def test_skip_download_again_is_a_successful_no_op(tmp_path):
    _storage_with(tmp_path)
    executor = _executor_for(tmp_path)
    await executor.execute(_skip())
    again = await executor.execute(_skip(reason=None))
    assert again["success"] is True and again["changed"] is False


@pytest.mark.asyncio
async def test_skip_download_refusal_is_reported_not_raised(tmp_path):
    state_file = _storage_with(tmp_path, status="downloaded")
    result = await _executor_for(tmp_path).execute(_skip())
    assert result == {
        "success": False,
        "message": "bad.mp4 is already downloaded. Skip is for downloads that "
        "keep failing; this one succeeded.",
    }
    assert _entry(state_file)["status"] == "downloaded"


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["group_dir", "file_name", "requested_by"])
async def test_skip_download_without_its_parameters_fails(tmp_path, missing):
    state_file = _storage_with(tmp_path)
    result = await _executor_for(tmp_path).execute(_skip(**{missing: ...}))
    assert result["success"] is False
    assert missing in result["message"]
    assert _entry(state_file)["status"] == "download_failed"


@pytest.mark.asyncio
async def test_skip_download_with_a_blank_parameter_fails(tmp_path):
    _storage_with(tmp_path)
    result = await _executor_for(tmp_path).execute(_skip(file_name="  "))
    assert result["success"] is False


# The TTT command may only skip what this install listed as stuck, on the camera
# it was sent for (TTT's generic command route could otherwise send any file).


@pytest.mark.asyncio
async def test_a_file_that_never_failed_is_not_skipped_from_ttt(tmp_path):
    import json

    state_file = _storage_with(tmp_path, status="pending")
    data = json.loads(state_file.read_text(encoding="utf-8"))
    next(iter(data["files"].values()))["failed_attempts"] = 0
    state_file.write_text(json.dumps(data), encoding="utf-8")

    result = await _executor_for(tmp_path).execute(_skip())
    assert result["success"] is False
    assert "has not failed" in result["message"]
    assert _entry(state_file)["status"] == "pending"


@pytest.mark.asyncio
async def test_a_file_from_another_camera_is_not_skipped(tmp_path):
    import json

    state_file = _storage_with(tmp_path)
    data = json.loads(state_file.read_text(encoding="utf-8"))
    next(iter(data["files"].values()))["metadata"] = {"camera_name": "private"}
    state_file.write_text(json.dumps(data), encoding="utf-8")

    command = _skip()
    command = type(command)(
        id=command.id, type=command.type, parameters=command.parameters, camera="shared"
    )
    result = await _executor_for(tmp_path).execute(command)
    assert result["success"] is False
    assert "private" in result["message"]
    assert _entry(state_file)["status"] == "download_failed"


@pytest.mark.asyncio
async def test_a_skip_not_addressed_to_an_install_is_refused(tmp_path):
    state_file = _storage_with(tmp_path)
    result = await _executor_for(tmp_path).execute(_skip(machine_id=...))
    assert result["success"] is False
    assert "machine_id" in result["message"]
    assert _entry(state_file)["status"] == "download_failed"


@pytest.mark.asyncio
async def test_requested_by_cannot_forge_log_lines(tmp_path):
    state_file = _storage_with(tmp_path)
    result = await _executor_for(tmp_path).execute(
        _skip(requested_by="Pat\nERROR forged line " + "x" * 300)
    )
    assert result["success"] is True
    by = _entry(state_file)["skipped_by"]
    assert "\n" not in by
    assert by.startswith("TTT: Pat ERROR forged line")
    assert len(by) <= len("TTT: ") + 100
