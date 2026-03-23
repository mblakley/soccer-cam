"""Tests for the CommandExecutor module."""

import pytest
from unittest.mock import MagicMock

from video_grouper.api_integrations.command_executor import (
    CommandExecutor,
    SUPPORTED_COMMANDS,
)


class TestCommandExecutor:
    def setup_method(self):
        self.app = MagicMock()
        self.executor = CommandExecutor(self.app)

    @pytest.mark.asyncio
    async def test_unknown_command(self):
        result = await self.executor.execute({"command_type": "unknown"})
        assert not result["success"]
        assert "unknown" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_missing_command_type(self):
        result = await self.executor.execute({})
        assert not result["success"]

    @pytest.mark.asyncio
    async def test_restart_command(self):
        result = await self.executor.execute({"command_type": "restart"})
        assert result["success"]

    @pytest.mark.asyncio
    async def test_start_recording_command(self):
        result = await self.executor.execute({"command_type": "start_recording"})
        assert result["success"]

    @pytest.mark.asyncio
    async def test_stop_recording_command(self):
        result = await self.executor.execute({"command_type": "stop_recording"})
        assert result["success"]

    @pytest.mark.asyncio
    async def test_delete_old_files(self):
        result = await self.executor.execute(
            {
                "command_type": "delete_old_files",
                "parameters": {"older_than_days": 7},
            }
        )
        assert result["success"]
        assert "7" in result["message"]

    @pytest.mark.asyncio
    async def test_delete_old_files_default_days(self):
        result = await self.executor.execute(
            {
                "command_type": "delete_old_files",
                "parameters": {},
            }
        )
        assert result["success"]
        assert "30" in result["message"]

    @pytest.mark.asyncio
    async def test_delete_old_files_no_parameters(self):
        result = await self.executor.execute({"command_type": "delete_old_files"})
        assert result["success"]

    @pytest.mark.asyncio
    async def test_all_supported_commands_succeed(self):
        for cmd_type in SUPPORTED_COMMANDS:
            result = await self.executor.execute({"command_type": cmd_type})
            assert result["success"], f"Command {cmd_type!r} should succeed"

    @pytest.mark.asyncio
    async def test_result_always_has_success_and_message(self):
        result = await self.executor.execute({"command_type": "restart"})
        assert "success" in result
        assert "message" in result

    @pytest.mark.asyncio
    async def test_unknown_command_result_has_success_and_message(self):
        result = await self.executor.execute({"command_type": "eject_disc"})
        assert "success" in result
        assert "message" in result
        assert result["success"] is False
