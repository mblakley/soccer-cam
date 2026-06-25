"""Real-media tests for the reactive corruption-recovery orchestration.

These drive :func:`recover_pipeline_input` end-to-end on REAL mp4 clips: a
byte-complete-but-corrupt segment can only be localized + repaired by a true
decode, which a mocked ``av.open`` can't reproduce. So this module overrides
conftest's autouse mocks (same pattern as tests/test_audio_padding.py) and runs
real PyAV + real filesystem.

The recovery is the REACTIVE half of the proactive->reactive rework: it runs only
after a pipeline decode step has already failed on a game, rebuilds the trimmed
``-raw.mp4`` the pipeline reads with the corrupt region keyframe-cut, flags the
loss, warns the user, and invalidates the manifest so the re-run starts fresh.
"""

import os
from unittest.mock import AsyncMock, Mock

import pytest

from video_grouper.models import DirectoryState
from video_grouper.pipeline.manifest import PipelineManifest
from video_grouper.task_processors.corrupt_recovery import recover_pipeline_input
from video_grouper.utils.ffmpeg_utils import combine_videos, decode_probe, trim_video


# --- Override conftest's autouse mocks for this module (use real PyAV/IO) ---
@pytest.fixture(autouse=True)
def mock_ffmpeg():
    yield


@pytest.fixture(autouse=True)
def mock_file_system():
    yield


@pytest.fixture(autouse=True)
def mock_httpx():
    yield


def _write_decodable_clip(path, video_seconds, fps=10, rate=16000):
    """Write a real mp4 with random (non-flat) frames so its bitstream breaks
    realistically when bytes are zeroed mid-file."""
    import av
    import numpy as np

    with av.open(str(path), "w", format="mp4") as container:
        vstream = container.add_stream("mpeg4", rate=fps)
        vstream.width = 64
        vstream.height = 64
        vstream.pix_fmt = "yuv420p"
        astream = container.add_stream("aac", rate=rate)

        rng = np.random.default_rng(0)
        for i in range(int(round(video_seconds * fps))):
            img = (rng.random((64, 64, 3)) * 255).astype(np.uint8)
            frame = av.VideoFrame.from_ndarray(img, format="rgb24").reformat(
                format="yuv420p"
            )
            frame.pts = i
            for pkt in vstream.encode(frame):
                container.mux(pkt)
        for pkt in vstream.encode(None):
            container.mux(pkt)

        total_samples = int(round(video_seconds * rate))
        written = 0
        while written < total_samples:
            n = min(1024, total_samples - written)
            arr = np.zeros((2, n), dtype="float32")
            aframe = av.AudioFrame.from_ndarray(arr, format="fltp", layout="stereo")
            aframe.sample_rate = rate
            for pkt in astream.encode(aframe):
                container.mux(pkt)
            written += n
        for pkt in astream.encode(None):
            container.mux(pkt)


def _corrupt_mid_file(path, frac_start=0.80, frac_end=0.92):
    data = bytearray(path.read_bytes())
    n = len(data)
    for i in range(int(n * frac_start), int(n * frac_end)):
        data[i] = 0
    path.write_bytes(bytes(data))


def _write_match_info(group_dir):
    (group_dir / "match_info.ini").write_text(
        "[MATCH]\n"
        "my_team_name = Flash\n"
        "opponent_team_name = Test\n"
        "location = Home\n"
        "start_time_offset = 00:00:00\n"
        "total_duration = 00:00:00\n"
    )


async def _build_corrupt_pipeline_input(group_dir):
    """Set up a game whose trimmed ``-raw.mp4`` carries source corruption.

    Two top-level segments (one corrupt), a combined.mp4 and a trimmed
    ``-raw.mp4`` produced by the same stream-copy path the real pipeline uses (so
    the corruption rides straight through). Returns the input_path the pipeline
    would read.
    """
    good = group_dir / "seg00.mp4"
    bad = group_dir / "seg01.mp4"
    _write_decodable_clip(good, video_seconds=20.0)
    _write_decodable_clip(bad, video_seconds=20.0)
    _corrupt_mid_file(bad)

    combined = group_dir / "combined.mp4"
    # Stream-copy combine with NO corrupt_starts: corruption rides through, exactly
    # as the production combine (which never decodes) leaves it.
    assert await combine_videos([str(good), str(bad)], str(combined))

    subdir = group_dir / "sub"
    subdir.mkdir()
    raw = subdir / "flash-test-home-06-08-2024-raw.mp4"
    assert await trim_video(str(combined), str(raw), "00:00:00", None)
    return str(raw)


def _make_config(trim_end=False):
    cfg = Mock()
    cfg.processing.trim_end_enabled = trim_end
    return cfg


@pytest.mark.asyncio
async def test_recover_repairs_input_flags_loss_and_notifies(tmp_path):
    """Full reactive recovery: localize -> keyframe-cut re-combine -> re-trim,
    leaving a clean ``-raw.mp4``, a video_loss flag, an NTFY warning, and a wiped
    manifest so the pipeline re-runs fresh."""
    group_dir = tmp_path / "2024.06.08-10.00.00"
    group_dir.mkdir()
    _write_match_info(group_dir)
    raw = await _build_corrupt_pipeline_input(group_dir)

    # Precondition: the pipeline input really is corrupt (a decode step would fail).
    assert await decode_probe(raw) is not None

    # A stale manifest exists; recovery must wipe it so the re-run starts fresh.
    manifest_path = PipelineManifest.path_for(str(group_dir))
    PipelineManifest.load_or_init(
        str(group_dir), raw, str(group_dir / "out.mp4")
    ).save()
    assert os.path.exists(manifest_path)

    ntfy_api = Mock(send_notification=AsyncMock())
    ntfy_processor = Mock(ntfy_service=Mock(ntfy_api=ntfy_api))

    outcome = await recover_pipeline_input(
        str(group_dir),
        str(tmp_path),
        raw,
        config=_make_config(),
        ntfy_processor=ntfy_processor,
    )

    assert outcome.repaired is True
    assert outcome.lost_seconds > 0

    # The pipeline input now decodes clean end-to-end (the dead span was cut).
    assert await decode_probe(raw) is None

    # The game is durably flagged — not shipped as if perfect.
    marker = DirectoryState(str(group_dir)).get_video_loss()
    assert marker is not None and marker["lost_seconds"] > 0

    # The camera manager was warned about the lost footage.
    ntfy_api.send_notification.assert_awaited_once()
    assert "lost" in ntfy_api.send_notification.await_args.kwargs["title"].lower()

    # The manifest was invalidated so the re-run re-decodes the repaired input.
    assert not os.path.exists(manifest_path)


@pytest.mark.asyncio
async def test_recover_no_source_corruption_is_not_repaired(tmp_path):
    """If the source segments are clean (the decode failure wasn't a cuttable
    source corruption), recovery does NOT repair, flag, or notify — the caller
    fails the game terminally rather than looping."""
    group_dir = tmp_path / "2024.06.08-11.00.00"
    group_dir.mkdir()
    _write_match_info(group_dir)

    good_a = group_dir / "seg00.mp4"
    good_b = group_dir / "seg01.mp4"
    _write_decodable_clip(good_a, video_seconds=6.0)
    _write_decodable_clip(good_b, video_seconds=6.0)
    subdir = group_dir / "sub"
    subdir.mkdir()
    raw = subdir / "flash-test-home-06-08-2024-raw.mp4"
    combined = group_dir / "combined.mp4"
    assert await combine_videos([str(good_a), str(good_b)], str(combined))
    assert await trim_video(str(combined), str(raw), "00:00:00", None)

    ntfy_api = Mock(send_notification=AsyncMock())
    ntfy_processor = Mock(ntfy_service=Mock(ntfy_api=ntfy_api))

    outcome = await recover_pipeline_input(
        str(group_dir),
        str(tmp_path),
        str(raw),
        config=_make_config(),
        ntfy_processor=ntfy_processor,
    )

    assert outcome.repaired is False
    assert outcome.reason is not None
    assert DirectoryState(str(group_dir)).get_video_loss() is None
    ntfy_api.send_notification.assert_not_awaited()
