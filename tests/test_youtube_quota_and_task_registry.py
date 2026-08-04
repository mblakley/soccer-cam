"""Regressions from the 2026-08-04 overnight batch.

Three defects that together stalled a five-game batch and buried the log
in 1,800 pointless API calls:

1. The daily upload quota is reported as HTTP 429, but only 400/403 were
   parsed for a reason -- so YouTubeQuotaError never fired and the queue's
   quota-deferral path never ran.
2. That deferral, when it did run, slept until "next midnight Pacific".
   The limit that actually bites is the project's "Video Uploads per day"
   metric, which was still rejecting uploads 3h39m AFTER midnight PT. A
   wake-fail-resleep would have become a 24h+ stall.
3. GameEndTask never implemented deserialize, so it stayed abstract and
   TaskRegistry could not construct the probe instance it uses to read
   task_type -- the class was silently absent from the registry and a
   persisted end-of-game task could not survive a restart.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest


def _minimal_config():
    """Config() itself raises -- nine sections are required."""
    from video_grouper.utils import config as C

    return C.Config(
        STORAGE=C.StorageConfig(path="."),
        RECORDING=C.RecordingConfig(),
        PROCESSING=C.ProcessingConfig(),
        LOGGING=C.LoggingConfig(),
        APP=C.AppConfig(),
        TEAMSNAP=C.TeamSnapConfig(),
        PLAYMETRICS=C.PlayMetricsConfig(),
        NTFY=C.NtfyConfig(),
        YOUTUBE=C.YouTubeConfig(),
    )


def _upload_src():
    from video_grouper.utils.youtube_upload import YouTubeUploader

    return inspect.getsource(YouTubeUploader.upload_video)


class TestQuotaErrorDetection:
    """A 429 must be recognised as a quota condition."""

    def test_429_is_parsed_for_a_reason(self):
        src = _upload_src()
        assert "400, 403, 429" in src, (
            "429 is the status the API returns for 'Video Uploads per day'; "
            "if it is not parsed, YouTubeQuotaError never fires and the "
            "uploader retries every 60s forever"
        )

    def test_bare_429_still_classified_without_a_parseable_body(self):
        """The observed 429 body stringifies its details oddly; a 429 from
        this API is a quota response regardless."""
        assert "status == 429" in _upload_src()


class TestQuotaBackoffIsBounded:
    """The deferral must poll, not predict the reset."""

    def test_does_not_sleep_until_midnight_pacific(self):
        from video_grouper.task_processors import base_queue_processor

        src = inspect.getsource(base_queue_processor.QueueProcessor)
        assert "_quota_retry_seconds" in src
        assert "US/Pacific" not in src, (
            "the midnight-Pacific model is contradicted by observation: "
            "uploads were still rejected 3h39m after midnight PT"
        )

    def _proc(self, minutes):
        from video_grouper.task_processors.base_queue_processor import QueueProcessor
        from video_grouper.task_processors.queue_type import QueueType

        class _Concrete(QueueProcessor):
            @property
            def queue_type(self):
                return QueueType.UPLOAD

            async def process_item(self, item):
                return None

        cfg = _minimal_config()
        cfg.youtube.quota_retry_minutes = minutes
        return _Concrete("/tmp/x", cfg)

    def test_interval_is_configurable(self):
        assert self._proc(45)._quota_retry_seconds == 45 * 60

    def test_nonsense_interval_is_floored_not_zero(self):
        """A 0 would restore the hot-retry loop this fix exists to stop."""
        assert self._proc(0)._quota_retry_seconds >= 60


class TestGameEndTaskRegisters:
    """GameEndTask must be constructible, hence registrable."""

    def test_not_abstract(self):
        from video_grouper.task_processors.tasks.ntfy.game_end_task import GameEndTask

        assert GameEndTask.__abstractmethods__ == frozenset(), (
            f"GameEndTask is abstract ({sorted(GameEndTask.__abstractmethods__)}); "
            f"TaskRegistry cannot build its probe instance and the class is "
            f"silently left out of the registry"
        )

    def test_round_trips_through_serialize(self):
        from video_grouper.task_processors.tasks.ntfy.game_end_task import GameEndTask

        task = GameEndTask(
            group_dir="D:/games/2026.07.08-18.20.31",
            config=_minimal_config(),
            ntfy_service=MagicMock(),
            combined_video_path="D:/games/2026.07.08-18.20.31/combined.mp4",
            start_time_offset="00:52",
            time_offset="01:30",
            time_seconds=90,
        )
        restored = GameEndTask.deserialize(task.serialize())
        assert restored.group_dir == task.group_dir
        assert restored.combined_video_path == task.combined_video_path, (
            "combined_video_path lost in round-trip; serialize() nests fields "
            "under metadata and deserialize must read them from there"
        )
        assert restored.start_time_offset == "00:52"


class TestNtfyDeserializersConstruct:
    """Config() raises (nine required sections), so both deserializers threw
    before they could return a task."""

    @pytest.mark.parametrize(
        "mod,cls_name",
        [
            ("game_end_task", "GameEndTask"),
            ("game_start_task", "GameStartTask"),
        ],
    )
    def test_deserialize_does_not_call_bare_config(self, mod, cls_name):
        import importlib

        m = importlib.import_module(f"video_grouper.task_processors.tasks.ntfy.{mod}")
        src = inspect.getsource(getattr(m, cls_name).deserialize)
        assert "Config()" not in src, (
            f"{cls_name}.deserialize calls Config(), which raises "
            f"ValidationError for nine missing sections"
        )

    def test_game_start_round_trips_without_raising(self):
        from video_grouper.task_processors.tasks.ntfy.game_start_task import (
            GameStartTask,
        )

        task = GameStartTask(
            group_dir="D:/games/g",
            config=_minimal_config(),
            ntfy_service=MagicMock(),
            combined_video_path="D:/games/g/combined.mp4",
        )
        GameStartTask.deserialize(task.serialize())
