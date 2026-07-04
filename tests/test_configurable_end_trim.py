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
        # end_time is the game DURATION (01:30:00 = 5400s), NOT the absolute
        # end position in the combined video (01:35:00 = start + duration).
        # _trim_video_sync receives this as its `duration` arg and internally
        # computes end_pts = start_seconds + duration_seconds, so the actual
        # cut lands at 00:05:00 + 01:30:00 = 01:35:00 in the combined video.
        assert task.end_time == "01:30:00"

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
            _, _, task = processor._queue.get_nowait()
            queued_items.append(task)

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


class TestTrimVideoEndCalculation:
    """Verify that TrimTask passes a true duration (not an absolute end position)
    to trim_video so _trim_video_sync does not double-count the start offset."""

    def _make_match_info(self, start_offset="00:05:00", total_duration_seconds=5400):
        from unittest.mock import MagicMock

        mock = MagicMock()
        mock.get_start_offset.return_value = start_offset
        mock.get_total_duration_seconds.return_value = total_duration_seconds
        return mock

    def test_end_time_is_duration_not_absolute_position(self):
        """end_time must be the game duration so trim_video adds start internally."""
        match_info = self._make_match_info(
            start_offset="00:05:00", total_duration_seconds=5400
        )
        task = TrimTask.from_match_info("/d", match_info, trim_end_enabled=True)
        # start is 300s, duration is 5400s, absolute end is 5700s = 01:35:00.
        # end_time must be the DURATION (01:30:00), NOT the absolute end (01:35:00).
        assert task.end_time == "01:30:00", (
            "end_time should be the duration; trim_video adds the start offset internally"
        )
        assert task.end_time != "01:35:00", (
            "absolute end position must not be passed as duration"
        )

    def test_zero_start_offset_duration_equals_end(self):
        """When start offset is 0, duration == absolute end (no offset to add)."""
        match_info = self._make_match_info(
            start_offset="00:00:00", total_duration_seconds=5400
        )
        task = TrimTask.from_match_info("/d", match_info, trim_end_enabled=True)
        assert task.end_time == "01:30:00"

    def test_start_offset_does_not_appear_in_end_time(self):
        """Changing start_offset should NOT change end_time (it is pure duration)."""
        mi_early = self._make_match_info(
            start_offset="00:02:00", total_duration_seconds=5400
        )
        mi_late = self._make_match_info(
            start_offset="00:10:00", total_duration_seconds=5400
        )
        task_early = TrimTask.from_match_info("/d", mi_early, trim_end_enabled=True)
        task_late = TrimTask.from_match_info("/d", mi_late, trim_end_enabled=True)
        assert task_early.end_time == task_late.end_time, (
            "end_time (duration) must be the same regardless of start offset"
        )


class TestGameEndTaskPersistsTotal:
    """Verify GameEndTask.process_response writes total_duration via update_game_times."""

    @staticmethod
    def _make_task(
        tmp_path,
        start_time_offset="00:05:00",
        time_offset="01:35:00",
        time_seconds=5700,
    ):
        """Concrete GameEndTask subclass (implements abstract deserialize)."""
        from unittest.mock import MagicMock

        from video_grouper.task_processors.tasks.ntfy.game_end_task import GameEndTask

        class _ConcreteGameEndTask(GameEndTask):
            @classmethod
            def deserialize(cls, data):
                return cls(**data)

        mock_config = MagicMock()
        mock_config.ntfy.topic = "test"
        mock_config.ntfy.server_url = "https://ntfy.sh"
        mock_config.ntfy.enabled = True

        return _ConcreteGameEndTask(
            group_dir=str(tmp_path),
            config=mock_config,
            ntfy_service=MagicMock(),
            combined_video_path=str(tmp_path / "combined.mp4"),
            start_time_offset=start_time_offset,
            time_offset=time_offset,
            time_seconds=time_seconds,
        )

    @pytest.mark.asyncio
    async def test_yes_response_persists_total_duration(self, tmp_path):
        """A 'yes' response must compute and write total_duration to match_info.ini."""
        from unittest.mock import patch

        task = self._make_task(
            tmp_path,
            start_time_offset="00:05:00",  # 300s
            time_offset="01:35:00",
            time_seconds=5700,  # 300s start + 5400s game = 5700s end
        )

        captured = {}

        def fake_update_game_times(gdir, *, total_duration=None, **kw):
            captured["group_dir"] = gdir
            captured["total_duration"] = total_duration

        # MatchInfo is imported lazily inside process_response; patch at the
        # point of lookup (video_grouper.models.MatchInfo).
        with patch("video_grouper.models.MatchInfo") as mock_mi:
            mock_mi.update_game_times.side_effect = fake_update_game_times
            result = await task.process_response("Yes, game ended at 01:35:00")

        assert result.success is True
        assert "total_duration" in captured, "update_game_times was not called"
        # duration = 5700 - 300 = 5400s = 01:30:00
        assert captured["total_duration"] == "01:30:00", (
            f"Expected total_duration='01:30:00', got {captured['total_duration']!r}"
        )

    @pytest.mark.asyncio
    async def test_no_response_does_not_persist(self, tmp_path):
        """A 'no' response must NOT call update_game_times."""
        from unittest.mock import patch

        task = self._make_task(tmp_path)

        with patch("video_grouper.models.MatchInfo") as mock_mi:
            await task.process_response("No, not yet at 01:35:00")
            mock_mi.update_game_times.assert_not_called()
