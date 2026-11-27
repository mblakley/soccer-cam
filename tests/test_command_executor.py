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
        for cmd_type in SUPPORTED_COMMANDS:
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
