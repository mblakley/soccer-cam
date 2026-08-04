"""Regression: a quota-parked processor must not block the auto-upgrade.

On 2026-08-04 the upload queue was parked on YouTube's daily "Video Uploads
per day" limit. Nothing was in flight and nothing could start -- the worker
loop was asleep -- yet quiescence reported "youtube=2, youtube in progress"
and the upgrade deferred.

It deferred on EVERY hourly check, not occasionally, because the update
check and StateAuditor's re-queue run on aligned ~60s cadences: the check
landed inside the same ~2s churn window every time. Meanwhile QUEUE_STATUS
sampled queued=0/in_progress=None at every 5-minute mark. The net effect was
that v0.5.12 -- the release that stops the retry storm -- could not install
because of the storm.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def mock_file_system():
    yield None


@pytest.fixture(autouse=True)
def mock_ffmpeg():
    yield None


def _app(youtube_queued=0, youtube_busy=None, quota_blocked=False):
    from video_grouper.video_grouper_app import VideoGrouperApp

    app = VideoGrouperApp.__new__(VideoGrouperApp)

    def _proc(qsize=0, busy=None, blocked=False):
        p = MagicMock()
        p.get_queue_size = MagicMock(return_value=qsize)
        p.get_in_progress_summary = MagicMock(return_value=busy)
        p.is_quota_blocked = MagicMock(return_value=blocked)
        p._in_progress_item = None
        return p

    app.video_processor = _proc()
    app.upload_processor = _proc(youtube_queued, youtube_busy, quota_blocked)
    app.ntfy_processor = None
    app.download_processors = {"cam": _proc()}
    app.clip_request_processor = None
    app.highlight_reel_processor = None
    app.ttt_job_processor = None
    app.clip_processor = None
    app.pipeline_processor = None
    return app


class TestQuotaParkedIsNotBusy:
    @pytest.mark.asyncio
    async def test_parked_uploads_do_not_block_the_upgrade(self):
        """The exact production state: 2 queued, one momentarily in hand,
        but the processor is parked on quota and cannot act."""
        app = _app(
            youtube_queued=2,
            youtube_busy="YoutubeUploadTask(2026.07.08-18.20.31)",
            quota_blocked=True,
        )
        is_idle, reason = await app._update_quiescence_check()
        assert is_idle is True, (
            f"quota-parked uploads reported the pipeline busy ({reason}); the "
            f"upgrade then defers for as long as the external limit lasts"
        )
        assert reason is None

    @pytest.mark.asyncio
    async def test_real_upload_still_blocks_the_upgrade(self):
        """The guard must keep working for genuine in-flight uploads --
        stopping the service mid-upload is what killed one at 2%."""
        app = _app(
            youtube_queued=0,
            youtube_busy="YoutubeUploadTask(2026.07.06-17.50.05)",
            quota_blocked=False,
        )
        is_idle, reason = await app._update_quiescence_check()
        assert is_idle is False
        assert "youtube" in reason

    @pytest.mark.asyncio
    async def test_queued_work_still_blocks_when_not_parked(self):
        app = _app(youtube_queued=3, quota_blocked=False)
        is_idle, reason = await app._update_quiescence_check()
        assert is_idle is False
        assert "youtube=3" in reason


class TestQuotaBlockedFlag:
    def _proc(self, minutes=30):
        from video_grouper.task_processors.base_queue_processor import QueueProcessor
        from video_grouper.task_processors.queue_type import QueueType
        from video_grouper.utils import config as C

        class _Concrete(QueueProcessor):
            @property
            def queue_type(self):
                return QueueType.UPLOAD

            async def process_item(self, item):
                return None

        cfg = C.Config(
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
        cfg.youtube.quota_retry_minutes = minutes
        return _Concrete("/tmp/x", cfg)

    def test_defaults_to_not_blocked(self):
        assert self._proc().is_quota_blocked() is False

    def test_blocked_while_parked_then_expires(self):
        p = self._proc()
        p._quota_blocked_until = time.time() + 60
        assert p.is_quota_blocked() is True
        p._quota_blocked_until = time.time() - 1
        assert p.is_quota_blocked() is False, "a lapsed park must not linger"

    def test_success_clears_the_park(self):
        """Set on quota error, cleared the moment anything succeeds."""
        import inspect

        from video_grouper.task_processors import base_queue_processor

        src = inspect.getsource(base_queue_processor.QueueProcessor)
        assert "self._quota_blocked_until = time.time() + wait_seconds" in src
        assert "self._quota_blocked_until = 0.0" in src
