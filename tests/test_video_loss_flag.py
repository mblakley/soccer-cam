"""Tests that combine durably flags video lost to camera-recording corruption.

A byte-complete-but-corrupt segment (PR #88's size check passes, yet a region is
undecodable) is unrecoverable — re-downloading returns identical bytes — so the
combine cuts the dead span and the game still ships. The requirement these tests
guard is that it is NOT shipped as if perfect: combine persists a ``video_loss``
marker on state.json so the dashboard / auditor / camera manager can see it.

Real PyAV + real filesystem are required end-to-end (a mocked av.open can't fail
mid-stream, and the flag is real file I/O), so this module overrides conftest's
autouse mocks the same way tests/test_audio_padding.py does.
"""

import os

import pytest

from video_grouper.models import DirectoryState
from video_grouper.task_processors.tasks.video import CombineTask


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
async def test_combine_task_flags_corruption_end_to_end(tmp_path):
    """CombineTask probes, cuts the corrupt region, and flags the loss.

    Drives the real execute() path: it must detect the corrupt segment, populate
    self.video_corruption (which VideoProcessor turns into the NTFY warning),
    produce a combined.mp4 that decodes cleanly, and persist the video_loss flag.
    """
    group_dir = tmp_path / "flash__2024.06.08_vs_Test_home"
    group_dir.mkdir()
    good = group_dir / "00.00.00-00.05.00.mp4"
    bad = group_dir / "00.05.00-00.10.00.mp4"
    _write_decodable_clip(good, video_seconds=20.0)
    _write_decodable_clip(bad, video_seconds=20.0)
    _corrupt_mid_file(bad)

    task = CombineTask(group_dir=str(group_dir))
    # CombineTask resolves output via storage_path; point it at the group's parent.
    task.storage_path = str(tmp_path)

    ok = await task.execute()
    assert ok is True

    # The corruption was detected and recorded for the NTFY warning path.
    assert len(task.video_corruption) == 1
    assert os.path.basename(task.video_corruption[0]["path"]) == bad.name
    assert task.video_corruption[0]["lost_seconds"] > 0

    # The combined output exists and decodes cleanly end-to-end (no garbage tail).
    from video_grouper.utils.ffmpeg_utils import decode_probe

    out = task.get_output_path()
    assert os.path.exists(out)
    assert await decode_probe(out) is None

    # The game is flagged — not silently shipped as if perfect.
    marker = DirectoryState(str(group_dir)).get_video_loss()
    assert marker is not None
    assert marker["lost_seconds"] > 0
