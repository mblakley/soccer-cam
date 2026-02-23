"""Tests for configurable end-trimming (Change 2)."""

from unittest.mock import MagicMock, patch

import pytest

from video_grouper.task_processors.tasks.video.trim_task import TrimTask
from video_grouper.utils.config import ProcessingConfig


class TestProcessingConfigTrimEnd:
    """Tests that trim_end_enabled config defaults to False."""

    def test_trim_end_enabled_defaults_to_false(self):
        config = ProcessingConfig()
        assert config.trim_end_enabled is False

    def test_trim_end_enabled_can_be_set_true(self):
        config = ProcessingConfig(trim_end_enabled=True)
        assert config.trim_end_enabled is True


class TestTrimTaskOptionalEndTime:
    """Tests for TrimTask with optional end_time."""

    def test_trim_task_without_end_time(self):
        task = TrimTask(group_dir="/some/dir", start_time="00:05:00")
        assert task.end_time is None

    def test_trim_task_with_end_time(self):
        task = TrimTask(
            group_dir="/some/dir", start_time="00:05:00", end_time="01:30:00"
        )
        assert task.end_time == "01:30:00"

    def test_serialize_without_end_time(self):
        task = TrimTask(group_dir="/some/dir", start_time="00:05:00")
        data = task.serialize()
        assert "end_time" not in data
        assert data["start_time"] == "00:05:00"
        assert data["task_type"] == "trim"

    def test_serialize_with_end_time(self):
        task = TrimTask(
            group_dir="/some/dir", start_time="00:05:00", end_time="01:30:00"
        )
        data = task.serialize()
        assert data["end_time"] == "01:30:00"

    def test_deserialize_without_end_time(self):
        data = {"task_type": "trim", "group_dir": "/some/dir", "start_time": "00:05:00"}
        task = TrimTask.deserialize(data)
        assert task.end_time is None
        assert task.start_time == "00:05:00"

    def test_deserialize_with_end_time(self):
        data = {
            "task_type": "trim",
            "group_dir": "/some/dir",
            "start_time": "00:05:00",
            "end_time": "01:30:00",
        }
        task = TrimTask.deserialize(data)
        assert task.end_time == "01:30:00"

    def test_str_without_end_time(self):
        task = TrimTask(group_dir="/some/dir", start_time="00:05:00")
        assert "00:05:00-end" in str(task)

    def test_str_with_end_time(self):
        task = TrimTask(
            group_dir="/some/dir", start_time="00:05:00", end_time="01:30:00"
        )
        assert "00:05:00-01:30:00" in str(task)


class TestTrimTaskFromMatchInfo:
    """Tests for TrimTask.from_match_info with trim_end_enabled flag."""

    def _make_match_info(self, start_offset="00:05:00", total_duration_seconds=5400):
        mock = MagicMock()
        mock.get_start_offset.return_value = start_offset
        mock.get_total_duration_seconds.return_value = total_duration_seconds
        return mock

    def test_from_match_info_trim_end_enabled(self):
        match_info = self._make_match_info()
        task = TrimTask.from_match_info("/some/dir", match_info, trim_end_enabled=True)
        assert task.start_time == "00:05:00"
        assert task.end_time is not None
        # 5 min start + 90 min duration = 95 min = 01:35:00
        assert task.end_time == "01:35:00"

    def test_from_match_info_trim_end_disabled(self):
        match_info = self._make_match_info()
        task = TrimTask.from_match_info("/some/dir", match_info, trim_end_enabled=False)
        assert task.start_time == "00:05:00"
        assert task.end_time is None

    def test_from_match_info_defaults_to_trim_end_enabled(self):
        """Default behavior (backward compatibility) includes end time."""
        match_info = self._make_match_info()
        task = TrimTask.from_match_info("/some/dir", match_info)
        assert task.end_time is not None


class TestNtfyProcessorEndTrimConfig:
    """Tests that NtfyProcessor respects trim_end_enabled config."""

    @pytest.mark.asyncio
    async def test_game_end_task_not_queued_when_trim_end_disabled(self, mock_config):
        """When trim_end_enabled=False, GameEndTask should not be created."""
        from video_grouper.task_processors.ntfy_processor import NtfyProcessor
        from video_grouper.utils.config import ProcessingConfig

        # Use a proper ProcessingConfig with trim_end_enabled=False
        mock_config.processing = ProcessingConfig(trim_end_enabled=False)

        mock_ntfy_service = MagicMock()
        mock_ntfy_service.has_been_processed.return_value = False
        mock_ntfy_service.is_waiting_for_input.return_value = False
        mock_ntfy_service.is_failed_to_send.return_value = False

        mock_match_info_service = MagicMock()

        processor = NtfyProcessor(
            storage_path="/tmp/test",
            config=mock_config,
            ntfy_service=mock_ntfy_service,
            match_info_service=mock_match_info_service,
        )
        processor._queue = __import__("asyncio").Queue()

        # Mock match_info that has start_time_offset set and team info populated
        # (so no TeamInfoTask is created - avoids abstract class issues)
        mock_match_info = MagicMock()
        mock_match_info.is_populated.return_value = False
        mock_match_info.get_team_info.return_value = {
            "my_team_name": "Team A",
            "opponent_team_name": "Team B",
            "location": "Home",
        }
        mock_match_info.start_time_offset = "00:05:00"

        with patch("video_grouper.task_processors.ntfy_processor.MatchInfo") as mock_mi:
            mock_mi.get_or_create.return_value = (mock_match_info, False)
            await processor.request_match_info_for_directory(
                "/some/dir", "/some/dir/combined.mp4"
            )

        # Check that no GameEndTask was queued
        queued_items = []
        while not processor._queue.empty():
            queued_items.append(processor._queue.get_nowait())

        task_types = [t.__class__.__name__ for t in queued_items]
        assert "GameEndTask" not in task_types, (
            "GameEndTask should not be queued when trim_end_enabled=False"
        )
        assert "GameStartTask" in task_types, "GameStartTask should still be queued"

    @pytest.mark.asyncio
    async def test_game_end_task_queued_when_trim_end_enabled(self, mock_config):
        """When trim_end_enabled=True, GameEndTask should be created."""
        from video_grouper.task_processors.ntfy_processor import NtfyProcessor
        from video_grouper.utils.config import ProcessingConfig

        # Use a proper ProcessingConfig with trim_end_enabled=True
        mock_config.processing = ProcessingConfig(trim_end_enabled=True)

        mock_ntfy_service = MagicMock()
        mock_ntfy_service.has_been_processed.return_value = False
        mock_ntfy_service.is_waiting_for_input.return_value = False
        mock_ntfy_service.is_failed_to_send.return_value = False

        mock_match_info_service = MagicMock()

        processor = NtfyProcessor(
            storage_path="/tmp/test",
            config=mock_config,
            ntfy_service=mock_ntfy_service,
            match_info_service=mock_match_info_service,
        )
        processor._queue = __import__("asyncio").Queue()

        mock_match_info = MagicMock()
        mock_match_info.is_populated.return_value = False
        mock_match_info.get_team_info.return_value = {
            "my_team_name": "Team A",
            "opponent_team_name": "Team B",
            "location": "Home",
        }
        mock_match_info.start_time_offset = "00:05:00"

        # Mock GameEndTask since it's abstract (missing deserialize)
        mock_game_end_cls = MagicMock()
        mock_game_end_instance = MagicMock()
        mock_game_end_cls.return_value = mock_game_end_instance

        with (
            patch("video_grouper.task_processors.ntfy_processor.MatchInfo") as mock_mi,
            patch(
                "video_grouper.task_processors.tasks.ntfy.GameEndTask",
                mock_game_end_cls,
            ),
        ):
            mock_mi.get_or_create.return_value = (mock_match_info, False)
            await processor.request_match_info_for_directory(
                "/some/dir", "/some/dir/combined.mp4"
            )

        # Verify GameEndTask constructor was called
        mock_game_end_cls.assert_called_once()
        call_args = mock_game_end_cls.call_args
        assert call_args[0][0] == "/some/dir", (
            "GameEndTask should be called with group_dir"
        )
