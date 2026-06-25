"""Tests for the ``video_loss`` flag and the (reactive-only) decode cost of combine.

Two guarantees here:

1. ``DirectoryState.set_video_loss`` round-trips the marker that records a game
   lost footage to a camera SD-card recording error (set by the reactive recovery,
   not by combine — see tests/test_corrupt_recovery.py).
2. Combine pays ZERO decode cost: it is a fast stream-copy and must NOT decode the
   video to hunt for in-file corruption (that ~realtime probe is reserved for the
   reactive recovery, which only runs on a game already known to be corrupt). A
   clean game therefore never decodes during combine.

Real PyAV + real filesystem are required end-to-end (a mocked av.open can't fail
mid-stream, and the flag is real file I/O), so this module overrides conftest's
autouse mocks the same way tests/test_audio_padding.py does.
"""

import os

import pytest

from video_grouper.models import DirectoryState
from video_grouper.task_processors.tasks.video import CombineTask
from video_grouper.utils import ffmpeg_utils


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
    """Write a real mp4 with random frames (breaks realistically when corrupted)."""
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


def test_directory_state_video_loss_roundtrip(tmp_path):
    """set_video_loss persists and get_video_loss reads back the marker."""
    state = DirectoryState(str(tmp_path))
    assert state.get_video_loss() is None

    state.set_video_loss(13.0, "seg2.mp4 ~13s@287s")

    marker = state.get_video_loss()
    assert marker is not None
    assert marker["lost_seconds"] == pytest.approx(13.0)
    assert "seg2.mp4" in marker["detail"]


@pytest.mark.asyncio
async def test_combine_does_not_proactively_decode(tmp_path, monkeypatch):
    """Combine is a fast stream-copy: it must NOT decode the video to look for
    in-file corruption — even when a segment IS corrupt.

    This is the heart of the proactive->reactive rework: decoding every segment at
    combine time costs ~realtime and would punish every clean game. We spy on the
    decode-probe primitive (``_decode_probe_sync``, which every corruption-decode
    path goes through) and assert combine never calls it. The combine still
    succeeds via stream-copy; the corruption is left to be caught reactively by a
    downstream decode step.
    """
    group_dir = tmp_path / "flash__2024.06.08_vs_Test_home"
    group_dir.mkdir()
    good = group_dir / "00.00.00-00.05.00.mp4"
    bad = group_dir / "00.05.00-00.10.00.mp4"
    _write_decodable_clip(good, video_seconds=20.0)
    _write_decodable_clip(bad, video_seconds=20.0)
    _corrupt_mid_file(bad)

    probe_calls = {"n": 0}
    real_probe = ffmpeg_utils._decode_probe_sync

    def _spy(*args, **kwargs):
        probe_calls["n"] += 1
        return real_probe(*args, **kwargs)

    monkeypatch.setattr(ffmpeg_utils, "_decode_probe_sync", _spy)

    task = CombineTask(group_dir=str(group_dir))
    # CombineTask resolves output via storage_path; point it at the group's parent.
    task.storage_path = str(tmp_path)

    ok = await task.execute()
    assert ok is True

    # ZERO decode cost: combine never decode-probed a single segment.
    assert probe_calls["n"] == 0
    # Combine no longer surfaces a corruption list (that moved to recovery).
    assert not hasattr(task, "video_corruption")

    # The combined output still exists (stream-copied through, corruption and all).
    assert os.path.exists(task.get_output_path())

    # And combine did NOT flag video_loss — the game is not yet known corrupt.
    assert DirectoryState(str(group_dir)).get_video_loss() is None
