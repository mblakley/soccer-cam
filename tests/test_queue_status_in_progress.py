"""Regression tests: QUEUE_STATUS log line must distinguish ``queue=0 + busy``
from ``truly idle``.

On 2026-06-01 the periodic ``QUEUE_STATUS`` line read ``video=0`` for
~10 minutes while a TrimTask was in fact running silently through a
14 GB stream copy. The pending queue's ``qsize()`` was zero because the
task had been dequeued for processing, but the processor was busy --
the legacy log line had no way to express that. The fix:

  - :meth:`BaseQueueProcessor.get_in_progress_summary` reports the
    currently-processing item (or ``None``).
  - :meth:`VideoGrouperApp.get_queue_status_summary` returns
    ``{queued: int, in_progress: str | None}`` per processor.
  - :meth:`_log_queue_status` uses the richer summary.

These tests close the diagnostic-fidelity gap that made the silent
trim look like a wedged worker.
"""

from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def mock_file_system():
    yield None


@pytest.fixture(autouse=True)
def mock_ffmpeg():
    yield None


class _FakeTask:
    """Minimal stand-in for BaseTask with the surface the summary uses."""

    def __init__(self, item_path: str):
        self._item_path = item_path

    def get_item_path(self) -> str:
        return self._item_path


class TestGetInProgressSummary:
    """``QueueProcessor.get_in_progress_summary`` returns a short
    identifier for the running task, or ``None`` when idle."""

    def _make_processor(self):
        from video_grouper.task_processors.base_queue_processor import (
            QueueProcessor,
        )
        from video_grouper.task_processors.queue_type import QueueType

        # QueueProcessor is ABC; build a minimal concrete subclass so
        # we can __new__ an instance without running __init__.
        class _TestableProcessor(QueueProcessor):
            @property
            def queue_type(self):
                return QueueType.DOWNLOAD

            async def process_item(self, item):
                return None

        proc = _TestableProcessor.__new__(_TestableProcessor)
        proc._in_progress_item = None
        proc._queue = None
        return proc

    def test_idle_returns_none(self):
        proc = self._make_processor()
        assert proc.get_in_progress_summary() is None

    def test_busy_returns_class_and_path(self):
        proc = self._make_processor()
        task = _FakeTask("2026.06.01-18.25.38")
        # Subclass _FakeTask to verify the actual class name surfaces
        # in the summary string -- so a reader of QUEUE_STATUS can tell
        # TrimTask from CombineTask from UploadTask.
        proc._in_progress_item = task
        summary = proc.get_in_progress_summary()
        assert summary == "_FakeTask(2026.06.01-18.25.38)"

    def test_busy_with_broken_get_item_path_falls_back_to_repr(self):
        """A task whose ``get_item_path()`` raises shouldn't kill the
        logger. Fall back to ``repr`` so we still get *something*."""
        proc = self._make_processor()

        class BrokenTask:
            def get_item_path(self):
                raise RuntimeError("path resolution failed")

            def __repr__(self):
                return "BrokenTask<repr>"

        proc._in_progress_item = BrokenTask()
        summary = proc.get_in_progress_summary()
        assert summary == "BrokenTask(BrokenTask<repr>)"


class TestQueueStatusSummary:
    """``VideoGrouperApp.get_queue_status_summary`` aggregates per-
    processor ``{queued, in_progress}`` for the log line."""

    def _make_app(
        self,
        video_qsize=0,
        video_busy=None,
        youtube_qsize=0,
        youtube_busy=None,
        download_qsize=0,
        download_busy=None,
        ntfy_enabled=True,
        ntfy_qsize=0,
        ntfy_busy=None,
    ):
        """Build a VideoGrouperApp-shaped object via __new__ + attribute
        injection; the real __init__ has too many dependencies to drive
        in a unit test."""
        from video_grouper.video_grouper_app import VideoGrouperApp

        app = VideoGrouperApp.__new__(VideoGrouperApp)

        def _mock_processor(qsize, busy):
            proc = MagicMock()
            proc.get_queue_size = MagicMock(return_value=qsize)
            proc.get_in_progress_summary = MagicMock(return_value=busy)
            return proc

        app.video_processor = _mock_processor(video_qsize, video_busy)
        app.upload_processor = _mock_processor(youtube_qsize, youtube_busy)
        app.ntfy_processor = (
            _mock_processor(ntfy_qsize, ntfy_busy) if ntfy_enabled else None
        )
        app.download_processors = {
            "soccer-cam": _mock_processor(download_qsize, download_busy)
        }
        app.clip_request_processor = None
        app.highlight_reel_processor = None
        app.ttt_job_processor = None
        app.clip_processor = None
        return app

    def test_all_idle_shows_zero_queues_and_none_in_progress(self):
        app = self._make_app()
        s = app.get_queue_status_summary()
        for name in ("video", "youtube", "ntfy", "download"):
            assert s[name]["queued"] == 0, f"{name} queued"
            assert s[name]["in_progress"] is None, f"{name} in_progress"

    def test_video_busy_with_empty_queue_reports_in_progress(self):
        """The exact 2026-06-01 scenario: queue=0 but trim is running."""
        app = self._make_app(
            video_qsize=0,
            video_busy="TrimTask(2026.06.01-18.25.38)",
        )
        s = app.get_queue_status_summary()
        assert s["video"] == {
            "queued": 0,
            "in_progress": "TrimTask(2026.06.01-18.25.38)",
        }, "queue=0 + busy must be reported, not collapsed to plain int 0"

    def test_youtube_busy_reports_in_progress(self):
        app = self._make_app(youtube_busy="YoutubeUploadTask(2026.06.01-18.25.38)")
        s = app.get_queue_status_summary()
        assert s["youtube"]["in_progress"] == ("YoutubeUploadTask(2026.06.01-18.25.38)")

    def test_disabled_ntfy_is_omitted_from_summary(self):
        """When the ntfy processor is None (disabled in config), the
        summary should omit it rather than emit ``None`` -- the log
        line stays compact."""
        app = self._make_app(ntfy_enabled=False)
        s = app.get_queue_status_summary()
        assert "ntfy" not in s

    def test_download_in_progress_aggregates_across_cameras(self):
        from video_grouper.video_grouper_app import VideoGrouperApp

        app = VideoGrouperApp.__new__(VideoGrouperApp)

        def _proc(qsize, busy):
            p = MagicMock()
            p.get_queue_size = MagicMock(return_value=qsize)
            p.get_in_progress_summary = MagicMock(return_value=busy)
            return p

        app.download_processors = {
            "soccer-cam": _proc(0, "RecordingFile(seg1.mp4)"),
            "second-cam": _proc(2, None),
        }
        app.video_processor = _proc(0, None)
        app.upload_processor = _proc(0, None)
        app.ntfy_processor = None
        app.clip_request_processor = None
        app.highlight_reel_processor = None
        app.ttt_job_processor = None
        app.clip_processor = None

        s = app.get_queue_status_summary()
        assert s["download"]["queued"] == 2  # 0 + 2 across cameras
        assert s["download"]["in_progress"] == ["RecordingFile(seg1.mp4)"]


class TestLogQueueStatusFormat:
    """The QUEUE_STATUS log line must include the in_progress field so
    downstream log readers (the watcher we ran 2026-06-01, dashboards,
    Mark's eyeballs) can tell a busy processor from an idle one."""

    def test_log_line_contains_in_progress(self, caplog):
        import logging

        from video_grouper.video_grouper_app import VideoGrouperApp

        app = VideoGrouperApp.__new__(VideoGrouperApp)

        def _proc(qsize, busy):
            p = MagicMock()
            p.get_queue_size = MagicMock(return_value=qsize)
            p.get_in_progress_summary = MagicMock(return_value=busy)
            return p

        app.video_processor = _proc(0, "TrimTask(2026.06.01-18.25.38)")
        app.upload_processor = _proc(0, None)
        app.ntfy_processor = None
        app.download_processors = {}
        app.clip_request_processor = None
        app.highlight_reel_processor = None
        app.ttt_job_processor = None
        app.clip_processor = None

        with caplog.at_level(logging.INFO, logger="video_grouper.video_grouper_app"):
            app._log_queue_status()

        log_text = " ".join(r.getMessage() for r in caplog.records)
        assert "QUEUE_STATUS" in log_text
        assert "TrimTask(2026.06.01-18.25.38)" in log_text, (
            "in_progress task identity must surface in the log line"
        )


class TestGetQueueSizesBackwardCompat:
    """The legacy ``get_queue_sizes()`` int-shaped return must remain
    intact -- two call sites (auth_status_provider and the
    StopService-deferral check) filter on ``v >= 0`` / ``v > 0``."""

    def test_returns_ints(self):
        from video_grouper.video_grouper_app import VideoGrouperApp

        app = VideoGrouperApp.__new__(VideoGrouperApp)

        def _proc(qsize):
            p = MagicMock()
            p.get_queue_size = MagicMock(return_value=qsize)
            return p

        app.video_processor = _proc(0)
        app.upload_processor = _proc(3)
        app.ntfy_processor = _proc(1)
        app.download_processors = {"soccer-cam": _proc(2)}
        app.clip_request_processor = None
        app.ttt_job_processor = None
        app.clip_processor = None

        sizes = app.get_queue_sizes()
        assert sizes["video"] == 0
        assert sizes["youtube"] == 3
        assert sizes["ntfy"] == 1
        assert sizes["download"] == 2
        assert sizes["clip_request"] == -1
        assert sizes["ttt_jobs"] == -1
        # All values comparable with int operators -- preserves the
        # `v >= 0` filter in auth_status_provider and the `v > 0`
        # filter in the StopService deferral path.
        for v in sizes.values():
            assert isinstance(v, int)
