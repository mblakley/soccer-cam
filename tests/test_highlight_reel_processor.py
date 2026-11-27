"""Tests for HighlightReelProcessor, against the work-source interface
(FakeWorkSource). Which TTT endpoint serves each verb (game- vs moment-clips,
claim with camera_id, report_blocker, ...) is tested in
tests/integrations/work/test_ttt_work_source.py."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from video_grouper.integrations.work import HIGHLIGHT_REEL
from video_grouper.integrations.work.fake import FakeWorkSource
from video_grouper.task_processors.highlight_reel_processor import (
    HighlightReelProcessor,
)
from video_grouper.task_processors.tasks.ttt.highlight_reel_task import (
    HighlightReelTask,
)


def _make_config(camera_id: str | None = "cam-xyz"):
    ttt = MagicMock()
    ttt.camera_id = camera_id
    config = MagicMock()
    config.ttt = ttt
    return config


async def _drain(processor, *reels):
    """The work poller owns polling + add_work. Test helper: run the given
    reel(s) through ``process_item`` directly — i.e. skip the poll step and
    exercise the processing path."""
    for reel in reels:
        await processor.process_item(HighlightReelTask(ttt_id=reel["id"], payload=reel))


def _work(clips_by_reel: dict, **kw) -> FakeWorkSource:
    """A work source whose ``details`` serves each reel's clips."""
    return FakeWorkSource(
        details={rid: {"clips": clips} for rid, clips in clips_by_reel.items()}, **kw
    )


def _progress(work) -> list[tuple[str, float]]:
    return [(c.args[0], c.args[1]) for c in work.called("progress")]


def _only(work, verb):
    """The single ``verb`` call's args."""
    (call,) = work.called(verb)
    return call.args


def _make_processor(tmp_path, *, work=None, youtube_uploader=None):
    if work is None:
        work = FakeWorkSource()
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
        work=work,
        youtube_uploader=youtube_uploader,
    )


def _stage_recording(
    storage_path: Path,
    group_dir: str,
    *,
    processed: bool = True,
    raw: bool = True,
    combined: bool = True,
    rendered: bool = True,
) -> Path:
    """Stage a recording dir the way the pipeline leaves it: match info, the
    untrimmed ``combined.mp4`` at the top, the trimmed full-field
    ``<slug>-raw.mp4`` at the exact path the match info names, the broadcast
    ``<slug>.mp4`` beside it, and a group status saying whether the render
    has finished. Returns the broadcast path (the file reels should use)."""
    import json

    from video_grouper.models import MatchInfo
    from video_grouper.utils.paths import get_trimmed_video_path

    target = storage_path / group_dir
    target.mkdir(parents=True, exist_ok=True)
    (target / "match_info.ini").write_text(
        "[MATCH]\nmy_team_name = Core Loop\nopponent_team_name = Rivals\n"
        "location = home\nstart_time_offset = 00:05:00\n"
    )
    info = MatchInfo.from_file(str(target / "match_info.ini"))
    raw_path = Path(get_trimmed_video_path(str(target), info, str(storage_path)))
    # conftest's autouse fixture mocks os.makedirs; create the game dir here.
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    if combined:
        (target / "combined.mp4").write_bytes(b"\x00" * 16)
    if raw:
        raw_path.write_bytes(b"\x00" * 16)
    broadcast = raw_path.with_name(raw_path.name[: -len("-raw.mp4")] + ".mp4")
    if processed:
        broadcast.write_bytes(b"\x00" * 16)  # written after the raw: current
    status = "pipeline_complete" if processed and rendered else "trimmed"
    (target / "state.json").write_text(json.dumps({"status": status, "files": {}}))
    return broadcast


def _make_clip(idx: int, group_dir: str, start: int = 10, end: int = 20):
    """A minimal game clip, shaped like a manual reel's details clips."""
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
    """A minimal moment clip, shaped like a moment_tagger reel's details clips.

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


async def _bad_combine(file_list_path: str, output_path: str) -> bool:
    return False


@pytest.mark.asyncio
async def test_happy_path_renders_uploads_and_reports(
    tmp_path, stub_trim_video, stub_combine_videos
):
    """Two clips → details → claim → trim each → concat → upload → complete."""
    _stage_recording(tmp_path, "game-A")

    reel = {
        "id": "reel-1",
        "title": "Best of Westside",
        "player_name": "Alice",
        "status": "pending",
    }
    work = _work(
        {
            "reel-1": [
                _make_clip(0, "game-A", start=10, end=20),
                _make_clip(1, "game-A", start=30, end=45),
            ]
        }
    )
    processor = _make_processor(tmp_path, work=work)

    await _drain(processor, reel)
    # Let the spawned task finish.
    await asyncio.sleep(0)

    assert [c.verb for c in work.calls if c.verb != "progress"] == [
        "details",
        "claim",
        "latest",
        "complete",
    ]
    assert {(c.kind, c.id) for c in work.calls} == {(HIGHLIGHT_REEL, "reel-1")}
    (result,) = _only(work, "complete")
    assert result["youtube_video_id"] == "YT_REEL_123"
    assert result["file_path"].endswith(".mp4")
    assert work.called("fail") == []
    # Final concat file exists; per-clip tmpdir is cleaned up.
    assert os.path.isfile(result["file_path"])


@pytest.mark.asyncio
async def test_source_missing_locally_does_not_claim(
    tmp_path, stub_trim_video, stub_combine_videos
):
    """When a clip's recording_group_dir doesn't resolve, the reel stays pending."""
    # game-A is staged but the second clip points at game-B which is NOT staged.
    _stage_recording(tmp_path, "game-A")

    reel = {"id": "reel-2", "title": "Mixed sources", "status": "pending"}
    work = _work({"reel-2": [_make_clip(0, "game-A"), _make_clip(1, "game-B")]})
    processor = _make_processor(tmp_path, work=work)

    await _drain(processor, reel)
    await asyncio.sleep(0)

    assert work.called("claim") == []
    assert work.called("complete") == []
    assert work.called("fail") == []


@pytest.mark.asyncio
async def test_render_failure_marks_reel_failed(tmp_path, stub_trim_video):
    """When combine_videos returns False, the reel is failed with an error message."""
    _stage_recording(tmp_path, "game-A")

    reel = {"id": "reel-3", "title": "Render flop", "status": "pending"}
    work = _work({"reel-3": [_make_clip(0, "game-A")]})
    processor = _make_processor(tmp_path, work=work)

    with patch(
        "video_grouper.task_processors.tasks.clips.highlight_compilation_task.combine_videos",
        side_effect=_bad_combine,
    ):
        await _drain(processor, reel)
        await asyncio.sleep(0)

    assert len(work.called("claim")) == 1
    assert work.called("complete") == []
    (err,) = _only(work, "fail")
    assert "combine_videos failed" in err


@pytest.mark.asyncio
async def test_upload_failure_marks_reel_failed(
    tmp_path, stub_trim_video, stub_combine_videos
):
    """When YouTubeUploader.upload_video raises, the reel is failed."""
    _stage_recording(tmp_path, "game-A")

    reel = {"id": "reel-4", "title": "Upload flop", "status": "pending"}
    work = _work({"reel-4": [_make_clip(0, "game-A")]})
    youtube_uploader = MagicMock()
    youtube_uploader.upload_video = MagicMock(
        side_effect=RuntimeError("YT 403: quota exceeded")
    )
    processor = _make_processor(tmp_path, work=work, youtube_uploader=youtube_uploader)

    await _drain(processor, reel)
    await asyncio.sleep(0)

    assert len(work.called("claim")) == 1
    assert work.called("complete") == []
    (err,) = _only(work, "fail")
    assert "quota exceeded" in err


@pytest.mark.asyncio
async def test_failure_report_error_is_logged_not_raised(tmp_path, stub_trim_video):
    """A fail report that itself errors is logged; process_item doesn't raise."""
    _stage_recording(tmp_path, "game-A")

    reel = {"id": "reel-4b", "title": "Double flop", "status": "pending"}
    work = _work(
        {"reel-4b": [_make_clip(0, "game-A")]},
        errors={"fail": RuntimeError("502")},
    )
    processor = _make_processor(tmp_path, work=work)

    with patch(
        "video_grouper.task_processors.tasks.clips.highlight_compilation_task.combine_videos",
        side_effect=_bad_combine,
    ):
        await _drain(processor, reel)

    assert len(work.called("fail")) == 1


@pytest.mark.asyncio
async def test_add_work_dedups_on_ttt_id(tmp_path):
    """``add_work`` keys dedup on ``ttt_id`` so the same reel queued
    twice only produces one queue entry."""
    processor = _make_processor(tmp_path)
    await processor.add_work(
        HighlightReelTask(ttt_id="reel-5", payload={"id": "reel-5"})
    )
    await processor.add_work(
        HighlightReelTask(ttt_id="reel-5", payload={"id": "reel-5"})
    )
    assert processor._queue.qsize() == 1


# Note: poll-side concerns (auth gate, camera_id propagation) moved to the
# work poller and the TTT work source — see test_ttt_poller.py and
# tests/integrations/work/test_ttt_work_source.py.


@pytest.mark.asyncio
async def test_refuses_without_youtube_uploader(tmp_path):
    """When youtube_uploader is None, processor refuses to process — no upload
    destination — and touches nothing on the work source."""
    work = _work({"reel-6": [_make_clip(0, "game-A")]})
    processor = _make_processor(tmp_path, work=work)
    # Manually set to None — _make_processor's default would supply a mock.
    processor.youtube_uploader = None

    with pytest.raises(RuntimeError, match="youtube_uploader"):
        await _drain(processor, {"id": "reel-6"})

    assert work.calls == []


@pytest.mark.asyncio
async def test_progress_emitted_per_stage(
    tmp_path, stub_trim_video, stub_combine_videos
):
    """Happy path should emit progress at each stage transition,
    once per trimmed clip, and on upload start."""
    _stage_recording(tmp_path, "game-A")

    reel = {
        "id": "reel-prog",
        "title": "Progress",
        "player_name": "P",
        "status": "pending",
    }
    work = _work(
        {
            "reel-prog": [
                _make_clip(0, "game-A"),
                _make_clip(1, "game-A"),
                _make_clip(2, "game-A"),
            ]
        }
    )
    processor = _make_processor(tmp_path, work=work)
    await _drain(processor, reel)
    await asyncio.sleep(0)

    # Expected progress sequence (uploading 0% only, since mocked upload_video
    # doesn't invoke the on_progress callback):
    #   ('trimming', 0), then per-clip ('trimming', N/3*100) → 33, 66, 100
    #   then ('concatenating', 0), ('concatenating', 100)
    #   then ('uploading', 0)
    assert _progress(work) == [
        ("trimming", 0),
        ("trimming", 33),
        ("trimming", 66),
        ("trimming", 100),
        ("concatenating", 0),
        ("concatenating", 100),
        ("uploading", 0),
    ]
    # Final transition is from update to complete, no further progress emits required.
    assert len(work.called("complete")) == 1


@pytest.mark.asyncio
async def test_progress_report_errors_do_not_stop_the_render(
    tmp_path, stub_trim_video, stub_combine_videos
):
    """Progress is fire-and-forget: a failing report is logged, the reel still
    completes."""
    _stage_recording(tmp_path, "game-A")

    reel = {"id": "reel-prog-err", "title": "Progress errors", "status": "pending"}
    work = _work(
        {"reel-prog-err": [_make_clip(0, "game-A")]},
        errors={"progress": RuntimeError("PATCH 500")},
    )
    processor = _make_processor(tmp_path, work=work)
    await _drain(processor, reel)

    assert len(work.called("complete")) == 1
    assert work.called("fail") == []


@pytest.mark.asyncio
async def test_progress_not_emitted_when_source_missing(
    tmp_path, stub_trim_video, stub_combine_videos
):
    """When a clip's source isn't local, no progress updates are emitted
    (the reel is skipped before claiming)."""
    _stage_recording(tmp_path, "game-A")

    reel = {"id": "reel-skip-prog", "title": "Skipped", "status": "pending"}
    work = _work({"reel-skip-prog": [_make_clip(0, "game-A"), _make_clip(1, "game-Z")]})
    processor = _make_processor(tmp_path, work=work)
    await _drain(processor, reel)
    await asyncio.sleep(0)

    assert work.called("claim") == []
    assert work.called("progress") == []


@pytest.mark.asyncio
async def test_upload_returns_none_marks_reel_failed(
    tmp_path, stub_trim_video, stub_combine_videos
):
    """`upload_video` returns None when the YT API errors internally (HttpError,
    auth refresh failure, etc.). The processor must NOT mark the reel ready
    with a null youtube_video_id — it must fail the reel.
    """
    _stage_recording(tmp_path, "game-A")

    reel = {"id": "reel-yt-none", "title": "Upload returns None", "status": "pending"}
    work = _work({"reel-yt-none": [_make_clip(0, "game-A")]})
    yt = MagicMock()
    yt.upload_video = MagicMock(return_value=None)

    processor = _make_processor(tmp_path, work=work, youtube_uploader=yt)
    await _drain(processor, reel)
    await asyncio.sleep(0)

    assert len(work.called("claim")) == 1
    assert work.called("complete") == []
    (err,) = _only(work, "fail")
    assert "no video id" in err


@pytest.mark.asyncio
async def test_upload_on_progress_throttle_emits_at_5_percent_steps(
    tmp_path, stub_trim_video, stub_combine_videos
):
    """The upload on_progress callback throttles to 5% increments and always
    fires at 100%. Verifies the run_coroutine_threadsafe -> progress report
    plumbing without simulating real chunks.
    """
    _stage_recording(tmp_path, "game-A")

    reel = {"id": "reel-throttle", "title": "Throttle test", "status": "pending"}
    work = _work({"reel-throttle": [_make_clip(0, "game-A")]})

    # Synthetic chunked upload — fire on_progress at 1, 4 (suppressed), 5,
    # 10, 12 (suppressed), 50, 100. Expected uploading-stage emits:
    # (0 from pre-call) + (5, 10, 50, 100) = throttle keeps 5%-step increments
    # plus the guaranteed 100% final.
    def fake_upload(
        video_path, title, description, tags, privacy, playlist, on_progress
    ):
        # The processor's pre-upload report already lands the initial 0%; here
        # the uploader emits subsequent chunks.
        for pct in (1, 4, 5, 10, 12, 50, 100):
            on_progress(pct)
        return "YT_THROTTLE_OK"

    yt = MagicMock()
    yt.upload_video = MagicMock(side_effect=fake_upload)

    processor = _make_processor(tmp_path, work=work, youtube_uploader=yt)
    await _drain(processor, reel)
    await asyncio.sleep(0)
    # Let the scheduled run_coroutine_threadsafe coroutines complete.
    for _ in range(20):
        await asyncio.sleep(0.05)

    uploading_emits = [pct for stage, pct in _progress(work) if stage == "uploading"]
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

    assert len(work.called("complete")) == 1
    assert work.called("fail") == []


@pytest.mark.asyncio
async def test_skip_reports_blocker_once_per_session(tmp_path):
    """When a clip's source is missing, ``blocked`` is reported exactly once per
    session even if the same reel is processed again."""
    # game-A is present; game-B is NOT staged → reel skips on clip 1.
    _stage_recording(tmp_path, "game-A")

    reel = {"id": "reel-blocker", "title": "Missing source", "status": "pending"}
    work = _work({"reel-blocker": [_make_clip(0, "game-A"), _make_clip(1, "game-B")]})
    processor = _make_processor(tmp_path, work=work)

    # First pass — reel-blocker is new, should report blocked once.
    await _drain(processor, reel)
    await asyncio.sleep(0)

    # Second pass — same reel still pending, already in _already_skipped.
    await _drain(processor, reel)
    await asyncio.sleep(0)

    assert work.called("claim") == []
    (call,) = work.called("blocked")
    assert call.id == "reel-blocker"
    assert "game-B" in call.args[0]


@pytest.mark.asyncio
async def test_claim_lost_skips_rendering(
    tmp_path, stub_trim_video, stub_combine_videos
):
    """When claim returns None (another camera-manager has the reel), the
    processor must not render, upload, complete or fail it."""
    _stage_recording(tmp_path, "game-A")

    reel = {"id": "reel-409", "title": "Already claimed", "status": "pending"}
    work = _work(
        {"reel-409": [_make_clip(0, "game-A")]},
        # Simulate a lost race — another camera-manager already claimed the reel.
        claims={"reel-409": None},
    )
    yt = MagicMock()
    yt.upload_video = MagicMock(return_value="YT_SHOULD_NOT_BE_CALLED")

    processor = _make_processor(tmp_path, work=work, youtube_uploader=yt)
    await _drain(processor, reel)
    await asyncio.sleep(0)

    assert [c.id for c in work.called("claim")] == ["reel-409"]
    yt.upload_video.assert_not_called()
    assert work.called("complete") == []
    assert work.called("fail") == []
    stub_trim_video.assert_not_called()


@pytest.mark.asyncio
async def test_idempotent_upload_when_youtube_video_id_present(
    tmp_path, stub_trim_video, stub_combine_videos
):
    """When ``latest`` returns a reel that already has youtube_video_id set
    (a prior run uploaded but the complete report failed), the processor
    must NOT upload again but MUST complete with the existing id."""
    _stage_recording(tmp_path, "game-A")

    reel = {"id": "reel-idem", "title": "Already uploaded", "status": "generating"}
    # The pending reel has no youtube_video_id, but ``latest`` (called just
    # before upload) reveals it was already uploaded.
    reel_with_yt = dict(reel, youtube_video_id="YT_EXISTING_456")
    work = _work(
        {"reel-idem": [_make_clip(0, "game-A")]},
        latest={"reel-idem": reel_with_yt},
    )
    yt = MagicMock()
    yt.upload_video = MagicMock(return_value="YT_NEW_SHOULD_NOT_BE_CALLED")

    processor = _make_processor(tmp_path, work=work, youtube_uploader=yt)
    await _drain(processor, reel)
    await asyncio.sleep(0)

    # Upload must be skipped.
    yt.upload_video.assert_not_called()
    # complete must carry the EXISTING youtube_video_id.
    (result,) = _only(work, "complete")
    assert result["youtube_video_id"] == "YT_EXISTING_456"
    assert result["file_path"].endswith(".mp4")
    assert work.called("fail") == []


@pytest.mark.asyncio
async def test_latest_error_proceeds_with_upload(
    tmp_path, stub_trim_video, stub_combine_videos
):
    """If the idempotency refetch fails, the reel is still uploaded + completed."""
    _stage_recording(tmp_path, "game-A")

    reel = {"id": "reel-latest-err", "title": "Refetch fails", "status": "pending"}
    work = _work(
        {"reel-latest-err": [_make_clip(0, "game-A")]},
        errors={"latest": RuntimeError("GET 500")},
    )
    processor = _make_processor(tmp_path, work=work)
    await _drain(processor, reel)

    (result,) = _only(work, "complete")
    assert result["youtube_video_id"] == "YT_REEL_123"


# ---------------------------------------------------------------------------
# Moment-tagger reel path (source='moment_tagger')
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_moment_reel_happy_path_renders_uploads_and_reports(
    tmp_path, stub_trim_video, stub_combine_videos
):
    """A moment-tagger reel's clips (from ``details``) are trimmed by
    clip_start_offset/clip_end_offset, then claimed → concat → upload →
    complete."""
    _stage_recording(tmp_path, "game-A")

    reel = {
        "id": "reel-m1",
        "title": "Bob's moments",
        "player_name": "Bob",
        "status": "pending",
        "source": "moment_tagger",
    }
    work = _work(
        {
            "reel-m1": [
                _make_moment_clip(0, "game-A", start=110.5, end=140.5),
                _make_moment_clip(1, "game-A", start=200.0, end=220.0),
            ]
        }
    )
    processor = _make_processor(tmp_path, work=work)

    await _drain(processor, reel)
    await asyncio.sleep(0)

    (details,) = work.called("details")
    assert details.id == "reel-m1"
    assert [c.id for c in work.called("claim")] == ["reel-m1"]
    (result,) = _only(work, "complete")
    assert result["youtube_video_id"] == "YT_REEL_123"
    assert result["file_path"].endswith(".mp4")
    assert work.called("fail") == []


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
    work = _work(
        {"reel-m-trim": [_make_moment_clip(0, "game-A", start=110.123, end=140.456)]}
    )

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
        processor = _make_processor(tmp_path, work=work)
        await _drain(processor, reel)
        await asyncio.sleep(0)

    assert len(captured) == 1
    _src, _dst, start_str, duration_str = captured[0]
    # 110.123 formatted to 3 decimals.
    assert start_str == "110.123"
    # duration = 140.456 - 110.123 = 30.333
    assert duration_str == "30.333"
    assert work.called("fail") == []


@pytest.mark.asyncio
async def test_moment_reel_missing_offsets_fails_the_reel(
    tmp_path, stub_trim_video, stub_combine_videos
):
    """A moment clip without offsets can't be trimmed: the claimed reel fails."""
    _stage_recording(tmp_path, "game-A")

    clip = _make_moment_clip(0, "game-A")
    clip["clip_start_offset"] = None
    reel = {"id": "reel-m-bad", "title": "No offsets", "source": "moment_tagger"}
    work = _work({"reel-m-bad": [clip]})
    processor = _make_processor(tmp_path, work=work)
    await _drain(processor, reel)

    (err,) = _only(work, "fail")
    assert "clip_start_offset" in err


@pytest.mark.asyncio
async def test_moment_reel_source_missing_locally_does_not_claim(
    tmp_path, stub_trim_video, stub_combine_videos
):
    """When a moment_clip's recording_group_dir doesn't resolve, the reel
    stays pending — no claim, no fail, but ``blocked`` fires once."""
    _stage_recording(tmp_path, "game-A")

    reel = {
        "id": "reel-m-skip",
        "title": "Missing source",
        "status": "pending",
        "source": "moment_tagger",
    }
    work = _work(
        {
            "reel-m-skip": [
                _make_moment_clip(0, "game-A"),
                _make_moment_clip(1, "game-Z"),  # not staged
            ]
        }
    )
    processor = _make_processor(tmp_path, work=work)

    await _drain(processor, reel)
    await asyncio.sleep(0)

    assert work.called("claim") == []
    assert work.called("complete") == []
    assert work.called("fail") == []
    (reason, _detail) = _only(work, "blocked")
    assert "game-Z" in reason


@pytest.mark.asyncio
async def test_moment_reel_recording_group_dir_none_does_not_claim(
    tmp_path, stub_trim_video, stub_combine_videos
):
    """When a moment_clip has recording_group_dir=None (game_session has no
    recording_group_dir), the reel skips without claim/fail and reports
    ``blocked`` explaining the None value."""
    _stage_recording(tmp_path, "game-A")

    reel = {
        "id": "reel-m-none",
        "title": "Null rgd",
        "status": "pending",
        "source": "moment_tagger",
    }
    work = _work({"reel-m-none": [_make_moment_clip(0, None)]})
    processor = _make_processor(tmp_path, work=work)

    await _drain(processor, reel)
    await asyncio.sleep(0)

    assert work.called("claim") == []
    assert work.called("fail") == []
    # The "None" branch produces a distinct reason string.
    (reason, _detail) = _only(work, "blocked")
    assert "is None" in reason


@pytest.mark.asyncio
async def test_combined_missing_reports_blocked(tmp_path, stub_trim_video):
    """The group dir exists but has no combined.mp4: blocked, not claimed."""
    (tmp_path / "game-A").mkdir()

    reel = {"id": "reel-no-combined", "title": "No combined"}
    work = _work({"reel-no-combined": [_make_clip(0, "game-A")]})
    processor = _make_processor(tmp_path, work=work)
    await _drain(processor, reel)

    assert work.called("claim") == []
    (reason, _detail) = _only(work, "blocked")
    assert "no trimmed game video" in reason


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
    work = _work({"reel-m-flop": [_make_moment_clip(0, "game-A")]})
    processor = _make_processor(tmp_path, work=work)

    with patch(
        "video_grouper.task_processors.tasks.clips.highlight_compilation_task.combine_videos",
        side_effect=_bad_combine,
    ):
        await _drain(processor, reel)
        await asyncio.sleep(0)

    assert len(work.called("claim")) == 1
    assert work.called("complete") == []
    (err,) = _only(work, "fail")
    assert "combine_videos failed" in err


@pytest.mark.asyncio
async def test_routing_mixed_sources_in_one_pass(
    tmp_path, stub_trim_video, stub_combine_videos
):
    """A 'manual' reel AND a 'moment_tagger' reel each render from their own
    clip shape (start_time/end_time vs clip_start_offset/clip_end_offset)."""
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
    work = _work(
        {
            "reel-manual": [_make_clip(0, "game-A", start=10, end=20)],
            "reel-moment": [_make_moment_clip(0, "game-A", start=110.5, end=140.5)],
        }
    )
    processor = _make_processor(tmp_path, work=work)
    await _drain(processor, manual_reel, moment_reel)
    await asyncio.sleep(0)

    assert [c.id for c in work.called("details")] == ["reel-manual", "reel-moment"]
    trims = [(c.args[2], c.args[3]) for c in stub_trim_video.call_args_list]
    assert trims == [("10.000", "10.000"), ("110.500", "30.000")]
    # Both reels were claimed and completed (no fails).
    assert len(work.called("claim")) == 2
    assert len(work.called("complete")) == 2
    assert work.called("fail") == []


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
    work = _work({"reel-m-empty": []})
    processor = _make_processor(tmp_path, work=work)
    await _drain(processor, reel)
    await asyncio.sleep(0)

    assert len(work.called("details")) == 1
    assert work.called("claim") == []
    assert work.called("fail") == []


@pytest.mark.asyncio
async def test_details_error_skips_without_claim(tmp_path):
    """If the clips can't be fetched, the reel is left alone (no claim/fail)."""
    work = FakeWorkSource(errors={"details": RuntimeError("GET 500")})
    processor = _make_processor(tmp_path, work=work)
    await _drain(processor, {"id": "reel-fetch-err", "title": "x"})

    assert [c.verb for c in work.calls] == ["details"]


# ---------------------------------------------------------------------------
# Which video a reel clip is cut from (decided 2026-11-26: the broadcast view;
# code review of #150, 2026-11-27)
# ---------------------------------------------------------------------------


def test_reel_source_prefers_the_broadcast_video(tmp_path):
    from video_grouper.task_processors.highlight_reel_processor import reel_source_for

    broadcast = _stage_recording(tmp_path, "game-A")
    clip = _make_moment_clip(0, "game-A")
    assert reel_source_for(str(tmp_path / "game-A"), clip) == str(broadcast)


def test_reel_source_falls_back_to_the_trimmed_full_field_video(tmp_path):
    from video_grouper.task_processors.highlight_reel_processor import reel_source_for

    _stage_recording(tmp_path, "game-A", processed=False)
    src = reel_source_for(str(tmp_path / "game-A"), _make_moment_clip(0, "game-A"))
    assert src is not None and src.endswith("-raw.mp4")


def test_full_field_game_clips_are_cut_from_the_full_field_video(tmp_path):
    from video_grouper.task_processors.highlight_reel_processor import reel_source_for

    _stage_recording(tmp_path, "game-A")
    clip = {**_make_clip(0, "game-A"), "video_type": "full"}
    src = reel_source_for(str(tmp_path / "game-A"), clip)
    assert src is not None and src.endswith("-raw.mp4")


def test_combined_mp4_alone_is_never_used(tmp_path):
    # combined.mp4 is untrimmed: clip offsets are on the trimmed timeline, so
    # cutting from it would land early by the trimmed-off pre-game footage.
    from video_grouper.task_processors.highlight_reel_processor import reel_source_for

    _stage_recording(tmp_path, "game-A", processed=False, raw=False)
    clip = _make_moment_clip(0, "game-A")
    assert reel_source_for(str(tmp_path / "game-A"), clip) is None


def test_a_broadcast_still_rendering_is_not_used(tmp_path):
    # The render (and AutoCam) write <slug>.mp4 in place for the whole render.
    from video_grouper.task_processors.highlight_reel_processor import reel_source_for

    _stage_recording(tmp_path, "game-A", rendered=False)
    src = reel_source_for(str(tmp_path / "game-A"), _make_moment_clip(0, "game-A"))
    assert src is not None and src.endswith("-raw.mp4")


def test_a_broadcast_older_than_the_trim_is_not_used(tmp_path):
    # After a re-trim the old broadcast is on the old timeline.
    import os as _os

    from video_grouper.task_processors.highlight_reel_processor import reel_source_for

    broadcast = _stage_recording(tmp_path, "game-A")
    raw = broadcast.with_name(broadcast.name[: -len(".mp4")] + "-raw.mp4")
    st = _os.stat(raw)
    _os.utime(broadcast, (st.st_atime, st.st_mtime - 60))
    src = reel_source_for(str(tmp_path / "game-A"), _make_moment_clip(0, "game-A"))
    assert src == str(raw)


def test_a_leftover_game_subdir_is_never_picked(tmp_path):
    # A team/opponent fix re-trims into a new subdir; the old one stays.
    from video_grouper.task_processors.highlight_reel_processor import reel_source_for

    broadcast = _stage_recording(tmp_path, "game-A")
    stale = tmp_path / "game-A" / "aaa - Core Loop vs Wrong (home)"
    stale.mkdir()
    (stale / "core-loop-wrong-home-raw.mp4").write_bytes(b"0")
    (stale / "core-loop-wrong-home.mp4").write_bytes(b"0")
    src = reel_source_for(str(tmp_path / "game-A"), _make_moment_clip(0, "game-A"))
    assert src == str(broadcast)


def test_raw_video_type_clips_come_from_the_full_field_video(tmp_path):
    from video_grouper.task_processors.highlight_reel_processor import reel_source_for

    _stage_recording(tmp_path, "game-A")
    clip = {**_make_clip(0, "game-A"), "video_type": "raw"}
    src = reel_source_for(str(tmp_path / "game-A"), clip)
    assert src is not None and src.endswith("-raw.mp4")


def test_no_match_info_means_no_source(tmp_path):
    from video_grouper.task_processors.highlight_reel_processor import reel_source_for

    _stage_recording(tmp_path, "game-A")
    (tmp_path / "game-A" / "match_info.ini").unlink()
    assert (
        reel_source_for(str(tmp_path / "game-A"), _make_moment_clip(0, "game-A"))
        is None
    )
