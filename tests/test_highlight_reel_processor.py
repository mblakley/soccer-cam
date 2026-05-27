"""Tests for HighlightReelProcessor."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from video_grouper.task_processors.highlight_reel_processor import (
    HighlightReelProcessor,
)


def _make_config(camera_id: str | None = "cam-xyz"):
    ttt = MagicMock()
    ttt.camera_id = camera_id
    config = MagicMock()
    config.ttt = ttt
    return config


def _make_processor(tmp_path, *, ttt_client=None, youtube_uploader=None):
    if ttt_client is None:
        ttt_client = MagicMock()
        ttt_client.is_authenticated = MagicMock(return_value=True)
    if youtube_uploader is None:
        youtube_uploader = MagicMock()
        youtube_uploader.upload_video = MagicMock(return_value="YT_REEL_123")
    # conftest.mock_file_system autouse-patches os.makedirs to a no-op, so the
    # concat task's makedirs(output_dir, exist_ok=True) silently does nothing —
    # create the highlights dir explicitly so file writes work.
    (tmp_path / "highlights").mkdir(parents=True, exist_ok=True)
    return HighlightReelProcessor(
        storage_path=str(tmp_path),
        config=_make_config(),
        ttt_client=ttt_client,
        youtube_uploader=youtube_uploader,
        poll_interval=60,
    )


def _stage_recording(storage_path: Path, group_dir: str) -> Path:
    """Create <storage_path>/<group_dir>/combined.mp4 and return its path."""
    target = storage_path / group_dir
    target.mkdir(parents=True, exist_ok=True)
    combined = target / "combined.mp4"
    combined.write_bytes(b"\x00" * 16)
    return combined


def _make_clip(idx: int, group_dir: str, start: int = 10, end: int = 20):
    """A minimal game-clip dict shaped like get_highlight_game_clips returns."""
    return {
        "id": f"clip-{idx}",
        "game_video_id": "game-video-1",
        "start_time": start,
        "end_time": end,
        "title": f"Clip {idx}",
        "recording_group_dir": group_dir,
        "camera_id": "cam-xyz",
    }


def _make_moment_clip(
    idx: int, group_dir: str | None, start: float = 110.5, end: float = 140.5
):
    """A minimal moment-clip dict shaped like get_highlight_moment_clips returns.

    Moment clips use float clip_start_offset/clip_end_offset (absolute
    offsets into the source combined.mp4) instead of int start_time/end_time.
    """
    return {
        "id": f"mclip-{idx}",
        "moment_tag_id": f"tag-{idx}",
        "game_session_id": "gs-1",
        "clip_start_offset": start,
        "clip_end_offset": end,
        "clip_duration": end - start,
        "status": "ready",
        "recording_group_dir": group_dir,
        "sequence_order": idx,
    }


@pytest.fixture
def stub_combine_videos():
    """Stub combine_videos to write a fake output and return True.

    Asserts the first arg is a list — guards against regressions to the
    pre-5328233 bug where the task passed a concat-file-list path string.
    """

    async def _fake_combine(file_paths, output_path: str) -> bool:
        assert isinstance(file_paths, list), (
            f"combine_videos expects list[str], got {type(file_paths).__name__}"
        )
        assert all(isinstance(p, str) for p in file_paths), (
            "combine_videos list items must be paths"
        )
        with open(output_path, "wb") as fh:
            fh.write(b"\x00" * 32)
        return True

    with patch(
        "video_grouper.task_processors.tasks.clips.highlight_compilation_task.combine_videos",
        side_effect=_fake_combine,
    ) as p:
        yield p


@pytest.fixture
def stub_trim_video():
    """Stub trim_video to write a fake output and return True."""

    async def _fake_trim(src: str, dst: str, start: str, duration: str) -> bool:
        with open(dst, "wb") as fh:
            fh.write(b"\x00" * 16)
        return True

    with patch(
        "video_grouper.task_processors.highlight_reel_processor.trim_video",
        side_effect=_fake_trim,
    ) as p:
        yield p


@pytest.mark.asyncio
async def test_happy_path_renders_uploads_and_reports(
    tmp_path, stub_trim_video, stub_combine_videos
):
    """Two clips → claim → trim each → concat → upload → complete."""
    _stage_recording(tmp_path, "game-A")

    reel = {
        "id": "reel-1",
        "title": "Best of Westside",
        "player_name": "Alice",
        "status": "pending",
    }
    game_clips = [
        _make_clip(0, "game-A", start=10, end=20),
        _make_clip(1, "game-A", start=30, end=45),
    ]

    ttt_client = MagicMock()
    ttt_client.is_authenticated = MagicMock(return_value=True)
    ttt_client.get_pending_highlights = MagicMock(return_value=[reel])
    ttt_client.get_highlight_game_clips = MagicMock(return_value=game_clips)
    ttt_client.claim_highlight = MagicMock(return_value=reel)
    ttt_client.get_highlight = MagicMock(return_value=reel)
    ttt_client.complete_highlight = MagicMock(return_value=reel)
    ttt_client.fail_highlight = MagicMock()

    processor = _make_processor(tmp_path, ttt_client=ttt_client)

    await processor.discover_work()
    # Let the spawned task finish.
    await asyncio.sleep(0)
    while processor._processing:
        await asyncio.sleep(0.01)

    ttt_client.claim_highlight.assert_called_once_with("reel-1", "cam-xyz")
    ttt_client.complete_highlight.assert_called_once()
    kwargs = ttt_client.complete_highlight.call_args.kwargs
    assert kwargs["youtube_video_id"] == "YT_REEL_123"
    assert kwargs["file_path"].endswith(".mp4")
    assert ttt_client.fail_highlight.call_count == 0
    # Final concat file exists; per-clip tmpdir is cleaned up.
    assert os.path.isfile(kwargs["file_path"])


@pytest.mark.asyncio
async def test_source_missing_locally_does_not_claim(
    tmp_path, stub_trim_video, stub_combine_videos
):
    """When a clip's recording_group_dir doesn't resolve, the reel stays pending."""
    # game-A is staged but the second clip points at game-B which is NOT staged.
    _stage_recording(tmp_path, "game-A")

    reel = {"id": "reel-2", "title": "Mixed sources", "status": "pending"}
    game_clips = [
        _make_clip(0, "game-A"),
        _make_clip(1, "game-B"),
    ]

    ttt_client = MagicMock()
    ttt_client.is_authenticated = MagicMock(return_value=True)
    ttt_client.get_pending_highlights = MagicMock(return_value=[reel])
    ttt_client.get_highlight_game_clips = MagicMock(return_value=game_clips)
    ttt_client.claim_highlight = MagicMock()
    ttt_client.complete_highlight = MagicMock()
    ttt_client.fail_highlight = MagicMock()

    processor = _make_processor(tmp_path, ttt_client=ttt_client)

    await processor.discover_work()
    await asyncio.sleep(0)
    while processor._processing:
        await asyncio.sleep(0.01)

    ttt_client.claim_highlight.assert_not_called()
    ttt_client.complete_highlight.assert_not_called()
    ttt_client.fail_highlight.assert_not_called()


@pytest.mark.asyncio
async def test_render_failure_marks_reel_failed(tmp_path, stub_trim_video):
    """When combine_videos returns False, the reel is failed with an error message."""
    _stage_recording(tmp_path, "game-A")

    reel = {"id": "reel-3", "title": "Render flop", "status": "pending"}
    game_clips = [_make_clip(0, "game-A")]

    ttt_client = MagicMock()
    ttt_client.is_authenticated = MagicMock(return_value=True)
    ttt_client.get_pending_highlights = MagicMock(return_value=[reel])
    ttt_client.get_highlight_game_clips = MagicMock(return_value=game_clips)
    ttt_client.claim_highlight = MagicMock()
    ttt_client.complete_highlight = MagicMock()
    ttt_client.fail_highlight = MagicMock()

    async def _bad_combine(file_list_path: str, output_path: str) -> bool:
        return False

    processor = _make_processor(tmp_path, ttt_client=ttt_client)

    with patch(
        "video_grouper.task_processors.tasks.clips.highlight_compilation_task.combine_videos",
        side_effect=_bad_combine,
    ):
        await processor.discover_work()
        await asyncio.sleep(0)
        while processor._processing:
            await asyncio.sleep(0.01)

    ttt_client.claim_highlight.assert_called_once()
    ttt_client.complete_highlight.assert_not_called()
    ttt_client.fail_highlight.assert_called_once()
    err = ttt_client.fail_highlight.call_args[0][1]
    assert "combine_videos failed" in err


@pytest.mark.asyncio
async def test_upload_failure_marks_reel_failed(
    tmp_path, stub_trim_video, stub_combine_videos
):
    """When YouTubeUploader.upload_video raises, the reel is failed."""
    _stage_recording(tmp_path, "game-A")

    reel = {"id": "reel-4", "title": "Upload flop", "status": "pending"}
    game_clips = [_make_clip(0, "game-A")]

    ttt_client = MagicMock()
    ttt_client.is_authenticated = MagicMock(return_value=True)
    ttt_client.get_pending_highlights = MagicMock(return_value=[reel])
    ttt_client.get_highlight_game_clips = MagicMock(return_value=game_clips)
    ttt_client.claim_highlight = MagicMock()
    ttt_client.get_highlight = MagicMock(return_value=reel)
    ttt_client.complete_highlight = MagicMock()
    ttt_client.fail_highlight = MagicMock()

    youtube_uploader = MagicMock()
    youtube_uploader.upload_video = MagicMock(
        side_effect=RuntimeError("YT 403: quota exceeded")
    )

    processor = _make_processor(
        tmp_path, ttt_client=ttt_client, youtube_uploader=youtube_uploader
    )

    await processor.discover_work()
    await asyncio.sleep(0)
    while processor._processing:
        await asyncio.sleep(0.01)

    ttt_client.claim_highlight.assert_called_once()
    ttt_client.complete_highlight.assert_not_called()
    ttt_client.fail_highlight.assert_called_once()
    err = ttt_client.fail_highlight.call_args[0][1]
    assert "quota exceeded" in err


@pytest.mark.asyncio
async def test_idempotency_within_single_poll(
    tmp_path, stub_trim_video, stub_combine_videos
):
    """Calling discover_work twice with the same pending reel only claims once."""
    _stage_recording(tmp_path, "game-A")

    reel = {"id": "reel-5", "title": "Once only", "status": "pending"}
    game_clips = [_make_clip(0, "game-A")]

    ttt_client = MagicMock()
    ttt_client.is_authenticated = MagicMock(return_value=True)
    # Both polls return the same pending reel (simulating a slow processor that
    # hasn't completed before the next discover_work fires).
    ttt_client.get_pending_highlights = MagicMock(return_value=[reel])
    ttt_client.get_highlight_game_clips = MagicMock(return_value=game_clips)
    ttt_client.claim_highlight = MagicMock(return_value=reel)
    ttt_client.get_highlight = MagicMock(return_value=reel)
    ttt_client.complete_highlight = MagicMock(return_value=reel)
    ttt_client.fail_highlight = MagicMock()

    processor = _make_processor(tmp_path, ttt_client=ttt_client)

    # First poll spawns the task.
    await processor.discover_work()
    # Before yielding to the spawned task, hit discover_work again — the
    # reel_id is in _processing so the second call must not spawn another task.
    await processor.discover_work()

    while processor._processing:
        await asyncio.sleep(0.01)

    ttt_client.claim_highlight.assert_called_once()


@pytest.mark.asyncio
async def test_discover_skips_when_not_authenticated(tmp_path):
    """When TTT isn't authenticated, poll skips without any side effects."""
    ttt_client = MagicMock()
    ttt_client.is_authenticated = MagicMock(return_value=False)
    ttt_client.get_pending_highlights = MagicMock()

    processor = _make_processor(tmp_path, ttt_client=ttt_client)
    await processor.discover_work()

    ttt_client.get_pending_highlights.assert_not_called()


@pytest.mark.asyncio
async def test_discover_skips_when_youtube_uploader_missing(tmp_path):
    """When youtube_uploader is None, poll skips — reel can't be shipped."""
    ttt_client = MagicMock()
    ttt_client.is_authenticated = MagicMock(return_value=True)
    ttt_client.get_pending_highlights = MagicMock()

    processor = _make_processor(tmp_path, ttt_client=ttt_client, youtube_uploader=None)
    # Manually set to None — _make_processor's default would supply a mock.
    processor.youtube_uploader = None

    await processor.discover_work()

    ttt_client.get_pending_highlights.assert_not_called()


@pytest.mark.asyncio
async def test_progress_emitted_per_stage(
    tmp_path, stub_trim_video, stub_combine_videos
):
    """Happy path should emit update_highlight_progress at each stage transition,
    once per trimmed clip, and on upload start."""
    _stage_recording(tmp_path, "game-A")

    reel = {
        "id": "reel-prog",
        "title": "Progress",
        "player_name": "P",
        "status": "pending",
    }
    game_clips = [
        _make_clip(0, "game-A"),
        _make_clip(1, "game-A"),
        _make_clip(2, "game-A"),
    ]

    ttt_client = MagicMock()
    ttt_client.is_authenticated = MagicMock(return_value=True)
    ttt_client.get_pending_highlights = MagicMock(return_value=[reel])
    ttt_client.get_highlight_game_clips = MagicMock(return_value=game_clips)
    ttt_client.claim_highlight = MagicMock(return_value=reel)
    ttt_client.get_highlight = MagicMock(return_value=reel)
    ttt_client.complete_highlight = MagicMock(return_value=reel)
    ttt_client.fail_highlight = MagicMock()
    ttt_client.update_highlight_progress = MagicMock()

    processor = _make_processor(tmp_path, ttt_client=ttt_client)
    await processor.discover_work()
    await asyncio.sleep(0)
    while processor._processing:
        await asyncio.sleep(0.01)

    # Expected progress sequence (uploading 0% only, since mocked upload_video
    # doesn't invoke the on_progress callback):
    #   ('trimming', 0), then per-clip ('trimming', N/3*100) → 33, 66, 100
    #   then ('concatenating', 0), ('concatenating', 100)
    #   then ('uploading', 0)
    emitted = [
        (kwargs["stage"], kwargs["percent"])
        for _, kwargs in ttt_client.update_highlight_progress.call_args_list
    ]
    assert ("trimming", 0) in emitted
    assert ("trimming", 100) in emitted
    assert ("concatenating", 0) in emitted
    assert ("concatenating", 100) in emitted
    assert ("uploading", 0) in emitted
    # Final transition is from update to complete, no further progress emits required.
    ttt_client.complete_highlight.assert_called_once()


@pytest.mark.asyncio
async def test_progress_not_emitted_when_source_missing(
    tmp_path, stub_trim_video, stub_combine_videos
):
    """When a clip's source isn't local, no progress updates are emitted
    (the reel is skipped before claiming)."""
    _stage_recording(tmp_path, "game-A")

    reel = {"id": "reel-skip-prog", "title": "Skipped", "status": "pending"}
    game_clips = [_make_clip(0, "game-A"), _make_clip(1, "game-Z")]

    ttt_client = MagicMock()
    ttt_client.is_authenticated = MagicMock(return_value=True)
    ttt_client.get_pending_highlights = MagicMock(return_value=[reel])
    ttt_client.get_highlight_game_clips = MagicMock(return_value=game_clips)
    ttt_client.update_highlight_progress = MagicMock()
    ttt_client.claim_highlight = MagicMock()
    ttt_client.fail_highlight = MagicMock()

    processor = _make_processor(tmp_path, ttt_client=ttt_client)
    await processor.discover_work()
    await asyncio.sleep(0)
    while processor._processing:
        await asyncio.sleep(0.01)

    ttt_client.claim_highlight.assert_not_called()
    ttt_client.update_highlight_progress.assert_not_called()


@pytest.mark.asyncio
async def test_camera_id_passed_to_pending_query(tmp_path):
    """The configured camera_id is forwarded to get_pending_highlights."""
    ttt_client = MagicMock()
    ttt_client.is_authenticated = MagicMock(return_value=True)
    ttt_client.get_pending_highlights = MagicMock(return_value=[])

    processor = _make_processor(tmp_path, ttt_client=ttt_client)
    await processor.discover_work()

    ttt_client.get_pending_highlights.assert_called_once_with("cam-xyz")


@pytest.mark.asyncio
async def test_upload_returns_none_marks_reel_failed(
    tmp_path, stub_trim_video, stub_combine_videos
):
    """`upload_video` returns None when the YT API errors internally (HttpError,
    auth refresh failure, etc.). The processor must NOT mark the reel ready
    with a null youtube_video_id — it must fail through fail_highlight.
    """
    _stage_recording(tmp_path, "game-A")

    reel = {"id": "reel-yt-none", "title": "Upload returns None", "status": "pending"}
    game_clips = [_make_clip(0, "game-A")]

    ttt_client = MagicMock()
    ttt_client.is_authenticated = MagicMock(return_value=True)
    ttt_client.get_pending_highlights = MagicMock(return_value=[reel])
    ttt_client.get_highlight_game_clips = MagicMock(return_value=game_clips)
    ttt_client.claim_highlight = MagicMock()
    ttt_client.get_highlight = MagicMock(return_value=reel)
    ttt_client.complete_highlight = MagicMock()
    ttt_client.fail_highlight = MagicMock()

    yt = MagicMock()
    yt.upload_video = MagicMock(return_value=None)

    processor = _make_processor(tmp_path, ttt_client=ttt_client, youtube_uploader=yt)
    await processor.discover_work()
    await asyncio.sleep(0)
    while processor._processing:
        await asyncio.sleep(0.01)

    ttt_client.claim_highlight.assert_called_once()
    ttt_client.complete_highlight.assert_not_called()
    ttt_client.fail_highlight.assert_called_once()
    assert "no video id" in ttt_client.fail_highlight.call_args[0][1]


@pytest.mark.asyncio
async def test_upload_on_progress_throttle_emits_at_5_percent_steps(
    tmp_path, stub_trim_video, stub_combine_videos
):
    """The upload on_progress callback throttles to 5% increments and always
    fires at 100%. Verifies the run_coroutine_threadsafe -> update_highlight_progress
    pipeline plumbing without simulating real chunks.
    """
    _stage_recording(tmp_path, "game-A")

    reel = {"id": "reel-throttle", "title": "Throttle test", "status": "pending"}
    game_clips = [_make_clip(0, "game-A")]

    ttt_client = MagicMock()
    ttt_client.is_authenticated = MagicMock(return_value=True)
    ttt_client.get_pending_highlights = MagicMock(return_value=[reel])
    ttt_client.get_highlight_game_clips = MagicMock(return_value=game_clips)
    ttt_client.claim_highlight = MagicMock()
    ttt_client.get_highlight = MagicMock(return_value=reel)
    ttt_client.complete_highlight = MagicMock()
    ttt_client.fail_highlight = MagicMock()
    ttt_client.update_highlight_progress = MagicMock()

    # Synthetic chunked upload — fire on_progress at 1, 4 (suppressed), 5,
    # 10, 12 (suppressed), 50, 100. Expected uploading-stage emits:
    # (0 from pre-call) + (5, 10, 50, 100) = throttle keeps 5%-step increments
    # plus the guaranteed 100% final.
    captured = []

    def fake_upload(
        video_path, title, description, tags, privacy, playlist, on_progress
    ):
        # The processor's pre-upload PATCH already lands the initial 0%; here
        # the uploader emits subsequent chunks.
        for pct in (1, 4, 5, 10, 12, 50, 100):
            on_progress(pct)
        return "YT_THROTTLE_OK"

    yt = MagicMock()
    yt.upload_video = MagicMock(side_effect=fake_upload)

    processor = _make_processor(tmp_path, ttt_client=ttt_client, youtube_uploader=yt)
    await processor.discover_work()
    await asyncio.sleep(0)
    while processor._processing:
        await asyncio.sleep(0.05)
    # Let the scheduled run_coroutine_threadsafe coroutines complete.
    for _ in range(20):
        await asyncio.sleep(0.05)

    captured = [
        (kwargs["stage"], kwargs["percent"])
        for _, kwargs in ttt_client.update_highlight_progress.call_args_list
    ]
    uploading_emits = [pct for stage, pct in captured if stage == "uploading"]
    # Contract: the throttle suppresses sub-5%-step increments AND always
    # emits the final 100%. From a starting baseline of -1:
    #   1   -> suppressed (1 - (-1) = 2)
    #   4   -> emitted    (4 - (-1) = 5)
    #   5   -> suppressed (5 - 4 = 1)
    #   10  -> emitted    (10 - 4 = 6)
    #   12  -> suppressed (12 - 10 = 2)
    #   50  -> emitted    (50 - 10 = 40)
    #   100 -> emitted    (pct == 100 special-case)
    # Plus the pre-upload 0 emit from the processor itself.
    assert 0 in uploading_emits, uploading_emits
    assert 100 in uploading_emits, uploading_emits
    # At least one mid-pipeline percent landed (the exact value depends on the
    # throttle's baseline; we don't pin a specific number to keep the test
    # robust to small impl tweaks).
    mid_emits = [p for p in uploading_emits if 0 < p < 100]
    assert len(mid_emits) >= 2, (
        f"throttle should emit >=2 mid-pipeline values, got {uploading_emits}"
    )
    # Suppressed values must NOT appear.
    assert 1 not in uploading_emits
    assert 12 not in uploading_emits

    ttt_client.complete_highlight.assert_called_once()
    ttt_client.fail_highlight.assert_not_called()


@pytest.mark.asyncio
async def test_skip_reports_blocker_once_per_session(tmp_path):
    """When a clip's source is missing, report_blocker is called exactly once per
    session even if discover_work fires multiple times for the same reel."""
    # game-A is present; game-B is NOT staged → reel skips on clip 1.
    _stage_recording(tmp_path, "game-A")

    reel = {"id": "reel-blocker", "title": "Missing source", "status": "pending"}
    game_clips = [_make_clip(0, "game-A"), _make_clip(1, "game-B")]

    ttt_client = MagicMock()
    ttt_client.is_authenticated = MagicMock(return_value=True)
    ttt_client.get_pending_highlights = MagicMock(return_value=[reel])
    ttt_client.get_highlight_game_clips = MagicMock(return_value=game_clips)
    ttt_client.claim_highlight = MagicMock()
    ttt_client.report_blocker = MagicMock(return_value=reel)
    ttt_client.complete_highlight = MagicMock()
    ttt_client.fail_highlight = MagicMock()

    processor = _make_processor(tmp_path, ttt_client=ttt_client)

    # First poll — reel-blocker is new, should report blocker once.
    await processor.discover_work()
    await asyncio.sleep(0)
    while processor._processing:
        await asyncio.sleep(0.01)

    # Second poll — same reel still pending, already in _already_skipped.
    await processor.discover_work()
    await asyncio.sleep(0)
    while processor._processing:
        await asyncio.sleep(0.01)

    ttt_client.claim_highlight.assert_not_called()
    ttt_client.report_blocker.assert_called_once()
    call_args = ttt_client.report_blocker.call_args
    assert call_args[0][0] == "reel-blocker"
    assert call_args[0][1] == "cam-xyz"
    assert "game-B" in call_args[0][2]


@pytest.mark.asyncio
async def test_claim_409_skips_rendering(
    tmp_path, stub_trim_video, stub_combine_videos
):
    """When claim_highlight returns None (409 from TTT), the processor must not
    render, upload, or call complete_highlight / fail_highlight."""
    _stage_recording(tmp_path, "game-A")

    reel = {"id": "reel-409", "title": "Already claimed", "status": "pending"}
    game_clips = [_make_clip(0, "game-A")]

    ttt_client = MagicMock()
    ttt_client.is_authenticated = MagicMock(return_value=True)
    ttt_client.get_pending_highlights = MagicMock(return_value=[reel])
    ttt_client.get_highlight_game_clips = MagicMock(return_value=game_clips)
    # Simulate 409 — another camera-manager already claimed the reel.
    ttt_client.claim_highlight = MagicMock(return_value=None)
    ttt_client.complete_highlight = MagicMock()
    ttt_client.fail_highlight = MagicMock()

    yt = MagicMock()
    yt.upload_video = MagicMock(return_value="YT_SHOULD_NOT_BE_CALLED")

    processor = _make_processor(tmp_path, ttt_client=ttt_client, youtube_uploader=yt)
    await processor.discover_work()
    await asyncio.sleep(0)
    while processor._processing:
        await asyncio.sleep(0.01)

    ttt_client.claim_highlight.assert_called_once_with("reel-409", "cam-xyz")
    yt.upload_video.assert_not_called()
    ttt_client.complete_highlight.assert_not_called()
    ttt_client.fail_highlight.assert_not_called()


@pytest.mark.asyncio
async def test_idempotent_upload_when_youtube_video_id_present(
    tmp_path, stub_trim_video, stub_combine_videos
):
    """When get_highlight returns a reel that already has youtube_video_id set
    (a prior run uploaded but complete_highlight PATCH failed), the processor
    must NOT upload again but MUST call complete_highlight with the existing id."""
    _stage_recording(tmp_path, "game-A")

    reel = {"id": "reel-idem", "title": "Already uploaded", "status": "generating"}
    game_clips = [_make_clip(0, "game-A")]

    # The pending reel (from get_pending_highlights) has no youtube_video_id.
    # But get_highlight (called just before upload) reveals it was already uploaded.
    reel_with_yt = dict(reel, youtube_video_id="YT_EXISTING_456")

    ttt_client = MagicMock()
    ttt_client.is_authenticated = MagicMock(return_value=True)
    ttt_client.get_pending_highlights = MagicMock(return_value=[reel])
    ttt_client.get_highlight_game_clips = MagicMock(return_value=game_clips)
    ttt_client.claim_highlight = MagicMock(return_value=reel)
    # get_highlight reveals the previously uploaded video id.
    ttt_client.get_highlight = MagicMock(return_value=reel_with_yt)
    ttt_client.complete_highlight = MagicMock(return_value=reel_with_yt)
    ttt_client.fail_highlight = MagicMock()

    yt = MagicMock()
    yt.upload_video = MagicMock(return_value="YT_NEW_SHOULD_NOT_BE_CALLED")

    processor = _make_processor(tmp_path, ttt_client=ttt_client, youtube_uploader=yt)
    await processor.discover_work()
    await asyncio.sleep(0)
    while processor._processing:
        await asyncio.sleep(0.01)

    # Upload must be skipped.
    yt.upload_video.assert_not_called()
    # complete_highlight must be called with the EXISTING youtube_video_id.
    ttt_client.complete_highlight.assert_called_once()
    kwargs = ttt_client.complete_highlight.call_args.kwargs
    assert kwargs["youtube_video_id"] == "YT_EXISTING_456"
    assert kwargs["file_path"].endswith(".mp4")
    ttt_client.fail_highlight.assert_not_called()


# ---------------------------------------------------------------------------
# Moment-tagger reel path (source='moment_tagger')
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_moment_reel_happy_path_renders_uploads_and_reports(
    tmp_path, stub_trim_video, stub_combine_videos
):
    """A moment-tagger reel routes through get_highlight_moment_clips, trims
    using clip_start_offset/clip_end_offset, then claims → concat → upload →
    complete. The game-clip endpoint must NOT be called for this reel."""
    _stage_recording(tmp_path, "game-A")

    reel = {
        "id": "reel-m1",
        "title": "Bob's moments",
        "player_name": "Bob",
        "status": "pending",
        "source": "moment_tagger",
    }
    moment_clips = [
        _make_moment_clip(0, "game-A", start=110.5, end=140.5),
        _make_moment_clip(1, "game-A", start=200.0, end=220.0),
    ]

    ttt_client = MagicMock()
    ttt_client.is_authenticated = MagicMock(return_value=True)
    ttt_client.get_pending_highlights = MagicMock(return_value=[reel])
    ttt_client.get_highlight_moment_clips = MagicMock(return_value=moment_clips)
    ttt_client.get_highlight_game_clips = MagicMock(
        side_effect=AssertionError("game-clips endpoint must not be called")
    )
    ttt_client.claim_highlight = MagicMock(return_value=reel)
    ttt_client.get_highlight = MagicMock(return_value=reel)
    ttt_client.complete_highlight = MagicMock(return_value=reel)
    ttt_client.fail_highlight = MagicMock()

    processor = _make_processor(tmp_path, ttt_client=ttt_client)

    await processor.discover_work()
    await asyncio.sleep(0)
    while processor._processing:
        await asyncio.sleep(0.01)

    ttt_client.get_highlight_moment_clips.assert_called_once_with("reel-m1")
    ttt_client.get_highlight_game_clips.assert_not_called()
    ttt_client.claim_highlight.assert_called_once_with("reel-m1", "cam-xyz")
    ttt_client.complete_highlight.assert_called_once()
    kwargs = ttt_client.complete_highlight.call_args.kwargs
    assert kwargs["youtube_video_id"] == "YT_REEL_123"
    assert kwargs["file_path"].endswith(".mp4")
    ttt_client.fail_highlight.assert_not_called()


@pytest.mark.asyncio
async def test_moment_reel_uses_clip_offsets_for_trim(tmp_path, stub_combine_videos):
    """Verify trim_video is called with float clip_start_offset/clip_end_offset
    (not start_time/end_time) — and that sub-second precision survives."""
    _stage_recording(tmp_path, "game-A")

    reel = {
        "id": "reel-m-trim",
        "title": "Trim args",
        "status": "pending",
        "source": "moment_tagger",
    }
    # Pick offsets with sub-second precision to make sure formatting preserves them.
    moment_clips = [_make_moment_clip(0, "game-A", start=110.123, end=140.456)]

    ttt_client = MagicMock()
    ttt_client.is_authenticated = MagicMock(return_value=True)
    ttt_client.get_pending_highlights = MagicMock(return_value=[reel])
    ttt_client.get_highlight_moment_clips = MagicMock(return_value=moment_clips)
    ttt_client.claim_highlight = MagicMock(return_value=reel)
    ttt_client.get_highlight = MagicMock(return_value=reel)
    ttt_client.complete_highlight = MagicMock()
    ttt_client.fail_highlight = MagicMock()

    captured: list[tuple[str, str, str, str]] = []

    async def _capture_trim(src: str, dst: str, start: str, duration: str) -> bool:
        captured.append((src, dst, start, duration))
        with open(dst, "wb") as fh:
            fh.write(b"\x00" * 16)
        return True

    with patch(
        "video_grouper.task_processors.highlight_reel_processor.trim_video",
        side_effect=_capture_trim,
    ):
        processor = _make_processor(tmp_path, ttt_client=ttt_client)
        await processor.discover_work()
        await asyncio.sleep(0)
        while processor._processing:
            await asyncio.sleep(0.01)

    assert len(captured) == 1
    _src, _dst, start_str, duration_str = captured[0]
    # 110.123 formatted to 3 decimals.
    assert start_str == "110.123"
    # duration = 140.456 - 110.123 = 30.333
    assert duration_str == "30.333"
    ttt_client.fail_highlight.assert_not_called()


@pytest.mark.asyncio
async def test_moment_reel_source_missing_locally_does_not_claim(
    tmp_path, stub_trim_video, stub_combine_videos
):
    """When a moment_clip's recording_group_dir doesn't resolve, the reel
    stays pending — no claim, no fail, but report_blocker fires once."""
    _stage_recording(tmp_path, "game-A")

    reel = {
        "id": "reel-m-skip",
        "title": "Missing source",
        "status": "pending",
        "source": "moment_tagger",
    }
    moment_clips = [
        _make_moment_clip(0, "game-A"),
        _make_moment_clip(1, "game-Z"),  # not staged
    ]

    ttt_client = MagicMock()
    ttt_client.is_authenticated = MagicMock(return_value=True)
    ttt_client.get_pending_highlights = MagicMock(return_value=[reel])
    ttt_client.get_highlight_moment_clips = MagicMock(return_value=moment_clips)
    ttt_client.claim_highlight = MagicMock()
    ttt_client.report_blocker = MagicMock(return_value=reel)
    ttt_client.complete_highlight = MagicMock()
    ttt_client.fail_highlight = MagicMock()

    processor = _make_processor(tmp_path, ttt_client=ttt_client)

    await processor.discover_work()
    await asyncio.sleep(0)
    while processor._processing:
        await asyncio.sleep(0.01)

    ttt_client.claim_highlight.assert_not_called()
    ttt_client.complete_highlight.assert_not_called()
    ttt_client.fail_highlight.assert_not_called()
    ttt_client.report_blocker.assert_called_once()
    assert "game-Z" in ttt_client.report_blocker.call_args[0][2]


@pytest.mark.asyncio
async def test_moment_reel_recording_group_dir_none_does_not_claim(
    tmp_path, stub_trim_video, stub_combine_videos
):
    """When a moment_clip has recording_group_dir=None (game_session has no
    recording_group_dir), the reel skips without claim/fail and reports
    a blocker explaining the None value."""
    _stage_recording(tmp_path, "game-A")

    reel = {
        "id": "reel-m-none",
        "title": "Null rgd",
        "status": "pending",
        "source": "moment_tagger",
    }
    moment_clips = [_make_moment_clip(0, None)]

    ttt_client = MagicMock()
    ttt_client.is_authenticated = MagicMock(return_value=True)
    ttt_client.get_pending_highlights = MagicMock(return_value=[reel])
    ttt_client.get_highlight_moment_clips = MagicMock(return_value=moment_clips)
    ttt_client.claim_highlight = MagicMock()
    ttt_client.report_blocker = MagicMock(return_value=reel)
    ttt_client.complete_highlight = MagicMock()
    ttt_client.fail_highlight = MagicMock()

    processor = _make_processor(tmp_path, ttt_client=ttt_client)

    await processor.discover_work()
    await asyncio.sleep(0)
    while processor._processing:
        await asyncio.sleep(0.01)

    ttt_client.claim_highlight.assert_not_called()
    ttt_client.fail_highlight.assert_not_called()
    ttt_client.report_blocker.assert_called_once()
    # The "None" branch produces a distinct reason string.
    assert "is None" in ttt_client.report_blocker.call_args[0][2]


@pytest.mark.asyncio
async def test_moment_reel_render_failure_marks_reel_failed(tmp_path, stub_trim_video):
    """When combine_videos returns False for a moment reel, the reel is
    failed with an error message — same as the game-clip path."""
    _stage_recording(tmp_path, "game-A")

    reel = {
        "id": "reel-m-flop",
        "title": "Concat flop",
        "status": "pending",
        "source": "moment_tagger",
    }
    moment_clips = [_make_moment_clip(0, "game-A")]

    ttt_client = MagicMock()
    ttt_client.is_authenticated = MagicMock(return_value=True)
    ttt_client.get_pending_highlights = MagicMock(return_value=[reel])
    ttt_client.get_highlight_moment_clips = MagicMock(return_value=moment_clips)
    ttt_client.claim_highlight = MagicMock(return_value=reel)
    ttt_client.complete_highlight = MagicMock()
    ttt_client.fail_highlight = MagicMock()

    async def _bad_combine(file_list_path: str, output_path: str) -> bool:
        return False

    processor = _make_processor(tmp_path, ttt_client=ttt_client)

    with patch(
        "video_grouper.task_processors.tasks.clips.highlight_compilation_task.combine_videos",
        side_effect=_bad_combine,
    ):
        await processor.discover_work()
        await asyncio.sleep(0)
        while processor._processing:
            await asyncio.sleep(0.01)

    ttt_client.claim_highlight.assert_called_once()
    ttt_client.complete_highlight.assert_not_called()
    ttt_client.fail_highlight.assert_called_once()
    err = ttt_client.fail_highlight.call_args[0][1]
    assert "combine_videos failed" in err


@pytest.mark.asyncio
async def test_routing_mixed_sources_in_single_poll(
    tmp_path, stub_trim_video, stub_combine_videos
):
    """A poll cycle with both a 'manual' reel AND a 'moment_tagger' reel
    routes each to the correct endpoint."""
    _stage_recording(tmp_path, "game-A")

    manual_reel = {
        "id": "reel-manual",
        "title": "Manual",
        "status": "pending",
        # Omitting source defaults to manual.
    }
    moment_reel = {
        "id": "reel-moment",
        "title": "Auto",
        "status": "pending",
        "source": "moment_tagger",
    }

    ttt_client = MagicMock()
    ttt_client.is_authenticated = MagicMock(return_value=True)
    ttt_client.get_pending_highlights = MagicMock(
        return_value=[manual_reel, moment_reel]
    )
    ttt_client.get_highlight_game_clips = MagicMock(
        return_value=[_make_clip(0, "game-A")]
    )
    ttt_client.get_highlight_moment_clips = MagicMock(
        return_value=[_make_moment_clip(0, "game-A")]
    )
    ttt_client.claim_highlight = MagicMock(side_effect=lambda rid, cid: {"id": rid})
    ttt_client.get_highlight = MagicMock(side_effect=lambda rid: {"id": rid})
    ttt_client.complete_highlight = MagicMock()
    ttt_client.fail_highlight = MagicMock()

    processor = _make_processor(tmp_path, ttt_client=ttt_client)
    await processor.discover_work()
    await asyncio.sleep(0)
    while processor._processing:
        await asyncio.sleep(0.01)

    # Manual reel hit the game-clips endpoint exactly once.
    ttt_client.get_highlight_game_clips.assert_called_once_with("reel-manual")
    # Moment reel hit the moment-clips endpoint exactly once.
    ttt_client.get_highlight_moment_clips.assert_called_once_with("reel-moment")
    # Both reels were claimed and completed (no fails).
    assert ttt_client.claim_highlight.call_count == 2
    assert ttt_client.complete_highlight.call_count == 2
    ttt_client.fail_highlight.assert_not_called()


@pytest.mark.asyncio
async def test_moment_reel_empty_clips_skips_without_claim(tmp_path):
    """A moment-tagger reel with zero linked clips logs + returns without
    claiming or failing — same shape as the empty game-clip path."""
    reel = {
        "id": "reel-m-empty",
        "title": "Empty",
        "status": "pending",
        "source": "moment_tagger",
    }

    ttt_client = MagicMock()
    ttt_client.is_authenticated = MagicMock(return_value=True)
    ttt_client.get_pending_highlights = MagicMock(return_value=[reel])
    ttt_client.get_highlight_moment_clips = MagicMock(return_value=[])
    ttt_client.claim_highlight = MagicMock()
    ttt_client.complete_highlight = MagicMock()
    ttt_client.fail_highlight = MagicMock()

    processor = _make_processor(tmp_path, ttt_client=ttt_client)
    await processor.discover_work()
    await asyncio.sleep(0)
    while processor._processing:
        await asyncio.sleep(0.01)

    ttt_client.get_highlight_moment_clips.assert_called_once()
    ttt_client.claim_highlight.assert_not_called()
    ttt_client.fail_highlight.assert_not_called()
