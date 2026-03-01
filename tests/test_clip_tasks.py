"""Tests for clip extraction and highlight compilation tasks."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from video_grouper.task_processors.tasks.clips.clip_extraction_task import (
    ClipExtractionTask,
)
from video_grouper.task_processors.tasks.clips.highlight_compilation_task import (
    HighlightCompilationTask,
)
from video_grouper.task_processors.queue_type import QueueType


# ---------------------------------------------------------------------------
# ClipExtractionTask
# ---------------------------------------------------------------------------


class TestClipExtractionTask:
    def _make_task(self, **overrides):
        defaults = {
            "tag_id": "aaa-111",
            "clip_id": "bbb-222",
            "game_session_id": "ccc-333",
            "group_dir": "/storage/2026-01-15",
            "trimmed_video_path": "/storage/2026-01-15/trimmed.mp4",
            "clip_start": 85.0,
            "clip_end": 115.0,
            "clip_output_path": "/storage/2026-01-15/clips/clip_aaa-111_85s.mp4",
        }
        defaults.update(overrides)
        return ClipExtractionTask(**defaults)

    def test_queue_type(self):
        assert ClipExtractionTask.queue_type() == QueueType.CLIPS

    def test_task_type(self):
        task = self._make_task()
        assert task.task_type == "clip_extraction"

    def test_serialize_deserialize(self):
        task = self._make_task()
        data = task.serialize()
        restored = ClipExtractionTask.deserialize(data)
        assert restored.tag_id == task.tag_id
        assert restored.clip_start == task.clip_start
        assert restored.clip_output_path == task.clip_output_path

    def test_get_item_path(self):
        task = self._make_task()
        assert task.get_item_path() == task.clip_output_path

    @pytest.mark.asyncio
    async def test_execute_calls_trim_video(self):
        task = self._make_task()
        with patch(
            "video_grouper.task_processors.tasks.clips.clip_extraction_task.trim_video",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_trim:
            result = await task.execute()
        assert result is True
        mock_trim.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_returns_false_on_failure(self):
        task = self._make_task()
        with patch(
            "video_grouper.task_processors.tasks.clips.clip_extraction_task.trim_video",
            new_callable=AsyncMock,
            return_value=False,
        ):
            result = await task.execute()
        assert result is False


# ---------------------------------------------------------------------------
# HighlightCompilationTask
# ---------------------------------------------------------------------------


class TestHighlightCompilationTask:
    def _make_task(self, **overrides):
        defaults = {
            "highlight_id": "hl-1",
            "title": "Game Highlights",
            "player_name": "Alice",
            "clip_local_paths": ("/clips/c1.mp4", "/clips/c2.mp4"),
            "output_dir": "/storage/highlights",
        }
        defaults.update(overrides)
        return HighlightCompilationTask(**defaults)

    def test_queue_type(self):
        assert HighlightCompilationTask.queue_type() == QueueType.CLIPS

    def test_task_type(self):
        task = self._make_task()
        assert task.task_type == "highlight_compilation"

    def test_serialize_deserialize(self):
        task = self._make_task()
        data = task.serialize()
        restored = HighlightCompilationTask.deserialize(data)
        assert restored.highlight_id == task.highlight_id
        assert restored.clip_local_paths == task.clip_local_paths

    def test_output_path(self):
        task = self._make_task(title="Season Best")
        assert task.output_path.endswith("Season Best.mp4")

    @pytest.mark.asyncio
    async def test_execute_calls_combine_videos(self):
        task = self._make_task()
        with (
            patch(
                "video_grouper.task_processors.tasks.clips.highlight_compilation_task.combine_videos",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_combine,
            patch("builtins.open", MagicMock()),
            patch("os.remove", MagicMock()),
        ):
            result = await task.execute()
        assert result is True
        mock_combine.assert_called_once()
