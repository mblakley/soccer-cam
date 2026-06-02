"""Regression tests: YouTube uploads must not block the event loop.

Memory entry [sync_upload_blocks_tray]: ``upload_video`` is synchronous
(googleapiclient resumable upload). When called via plain ``await`` from
an ``async def`` it runs on the shared event loop, blocking it for the
upload duration (60-90 min for a full-field match). That starves the
auth server's uvicorn loop and the tray's status poller, which then
times out every 60s with ``update status poll unexpected error: timed
out`` -- observed live during the 2026-06-01 game.

These tests use a synchronous ``time.sleep`` inside the mocked uploader
and a concurrent heartbeat coroutine. If the production code awaits the
sync upload directly, the heartbeat is starved and tick count stays at
zero. If the upload is wrapped in ``asyncio.to_thread``, the event loop
keeps running and the heartbeat advances normally.
"""

import asyncio
import time
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def mock_file_system():
    """Override conftest mock so we can place real files in tmp_path."""
    yield None


@pytest.fixture(autouse=True)
def mock_ffmpeg():
    """Override conftest mock; these tests don't touch PyAV."""
    yield None


# Tunables. Block long enough that heartbeat skew is unambiguous, but
# not so long that CI feels it.
_BLOCK_SECONDS = 0.4
_HEARTBEAT_INTERVAL_S = 0.05
# Allow a few ticks slack for scheduler jitter and the thread spin-up cost.
_HEARTBEAT_MIN_TICKS = int(_BLOCK_SECONDS / _HEARTBEAT_INTERVAL_S) - 2


async def _run_heartbeat_while(coro):
    """Run `coro` to completion while ticking a counter on the event loop.

    Returns (coro_result, tick_count). A blocked event loop yields zero
    ticks during the call; a properly-offloaded sync workload yields
    roughly BLOCK_SECONDS / HEARTBEAT_INTERVAL_S ticks.
    """
    ticks = 0
    stop = asyncio.Event()

    async def beat():
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            try:
                await asyncio.wait_for(stop.wait(), timeout=_HEARTBEAT_INTERVAL_S)
            except TimeoutError:
                pass

    hb = asyncio.create_task(beat())
    try:
        result = await coro
    finally:
        stop.set()
        await hb
    return result, ticks


class TestClipProcessorUploadOffThread:
    """``ClipProcessor._upload_to_youtube`` wraps the sync uploader call
    in ``asyncio.to_thread`` so the event loop stays responsive during
    long uploads."""

    @pytest.mark.asyncio
    async def test_upload_releases_event_loop(self, tmp_path):
        from video_grouper.task_processors.clip_processor import ClipProcessor

        def blocking_upload(*args, **kwargs):
            time.sleep(_BLOCK_SECONDS)
            return "fake_video_id"

        config = MagicMock()
        config.youtube.privacy_status = "unlisted"

        uploader = MagicMock()
        uploader.upload_video = blocking_upload

        # Build a minimal ClipProcessor without running __init__ -- we
        # only need youtube_uploader and config for _upload_to_youtube.
        processor = ClipProcessor.__new__(ClipProcessor)
        processor.youtube_uploader = uploader
        processor.config = config

        video = tmp_path / "clip.mp4"
        video.write_bytes(b"x" * 1024)

        result, ticks = await _run_heartbeat_while(
            processor._upload_to_youtube(str(video), "title", "desc")
        )
        assert result == "fake_video_id"
        assert ticks >= _HEARTBEAT_MIN_TICKS, (
            f"event loop appears to be blocked during the upload: only "
            f"{ticks} heartbeat ticks during a {_BLOCK_SECONDS:.1f}s sleep "
            f"(expected at least {_HEARTBEAT_MIN_TICKS}). Did you remove "
            f"the asyncio.to_thread wrap?"
        )


class TestYoutubeUploadTaskOffThread:
    """The full ``YoutubeUploadTask.execute`` end-to-end path has too many
    side effects (OAuth, match-info loading, playlist coordination) to
    drive in a unit test without a brittle mock stack. The clip-processor
    case above is the behavioral regression test for the
    ``asyncio.to_thread`` pattern; here we add a static guard against
    the same wrap being reverted in ``youtube_upload_task``."""

    def test_upload_calls_routed_through_to_thread(self):
        """Static guard: if a refactor reverts the ``asyncio.to_thread``
        wrap around ``uploader.upload_video`` or ``get_or_create_playlist``,
        this test fails with a clear message.
        """
        import inspect
        import re

        from video_grouper.task_processors.tasks.upload import youtube_upload_task

        src = inspect.getsource(youtube_upload_task)

        # Direct call: `uploader.upload_video(...)`. Should be zero --
        # the only references should be `uploader.upload_video,` (as a
        # reference passed to asyncio.to_thread, no parens).
        direct_upload_calls = re.findall(r"uploader\.upload_video\s*\(", src)
        assert len(direct_upload_calls) == 0, (
            f"uploader.upload_video is called directly {len(direct_upload_calls)} "
            f"time(s); it must be wrapped in `await asyncio.to_thread(...)` "
            f"so the resumable upload doesn't block the shared event loop."
        )

        # Direct call: `uploader.get_or_create_playlist(...)`. Same rule.
        direct_playlist_calls = re.findall(
            r"uploader\.get_or_create_playlist\s*\(", src
        )
        assert len(direct_playlist_calls) == 0, (
            f"uploader.get_or_create_playlist is called directly "
            f"{len(direct_playlist_calls)} time(s); must be wrapped in "
            f"`await asyncio.to_thread(...)` -- it issues blocking HTTP."
        )

        # Reference passed to to_thread: `asyncio.to_thread(...,\n  uploader.upload_video,`.
        # Soccer-cam uploads processed + raw, so we expect exactly 2.
        to_thread_uploads = re.findall(
            r"asyncio\.to_thread\s*\(\s*uploader\.upload_video\s*,", src
        )
        assert len(to_thread_uploads) == 2, (
            f"Expected exactly 2 `asyncio.to_thread(uploader.upload_video, ...)` "
            f"call sites (processed + raw uploads), found {len(to_thread_uploads)}."
        )

        # Reference passed to to_thread for playlist creation.
        to_thread_playlists = re.findall(
            r"asyncio\.to_thread\s*\(\s*uploader\.get_or_create_playlist\s*,", src
        )
        assert len(to_thread_playlists) == 2, (
            f"Expected exactly 2 `asyncio.to_thread(uploader.get_or_create_playlist, ...)` "
            f"call sites (one per upload variant), found {len(to_thread_playlists)}."
        )
