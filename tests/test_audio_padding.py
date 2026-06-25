"""Real-media tests for audio/video sync handling in the combine step.

Unlike the rest of the suite (which mocks PyAV), these encode tiny real mp4
clips so the silence padding is actually generated and muxed — the whole point
of the fix is that the output bytes line up, which a mocked ``av.open`` can't
prove. The module-level fixtures below intentionally OVERRIDE conftest's
autouse ``mock_ffmpeg`` / ``mock_file_system`` / ``mock_httpx`` so real PyAV and
real filesystem calls run here.
"""

import os

import pytest

from video_grouper.utils.ffmpeg_utils import (
    combine_videos,
    decode_probe,
    detect_audio_video_gaps,
    detect_video_decode_corruption,
)


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


def _write_clip(path, video_seconds, audio_seconds, fps=10, rate=16000):
    """Write a tiny real mp4 with independent video and audio durations.

    Models a camera segment whose audio is shorter than its video (the
    mic-dropout case). Video is mpeg4 (always available); audio is AAC.
    """
    import av
    import numpy as np

    with av.open(str(path), "w", format="mp4") as container:
        vstream = container.add_stream("mpeg4", rate=fps)
        vstream.width = 64
        vstream.height = 64
        vstream.pix_fmt = "yuv420p"
        astream = container.add_stream("aac", rate=rate)

        for i in range(int(round(video_seconds * fps))):
            img = np.zeros((64, 64, 3), dtype=np.uint8)
            frame = av.VideoFrame.from_ndarray(img, format="rgb24").reformat(
                format="yuv420p"
            )
            frame.pts = i  # explicit PTS so the muxed video duration is correct
            for pkt in vstream.encode(frame):
                container.mux(pkt)
        for pkt in vstream.encode(None):
            container.mux(pkt)

        total_samples = int(round(audio_seconds * rate))
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


def _decode_durations(path):
    """Ground-truth (video_seconds, audio_seconds) by decoding, not metadata."""
    import av

    with av.open(str(path)) as container:
        vstream = next(s for s in container.streams if s.type == "video")
        fps = float(vstream.average_rate or 10)
        frames = sum(1 for _ in container.decode(video=0))
    samples = 0
    rate = None
    with av.open(str(path)) as container:
        astream = next((s for s in container.streams if s.type == "audio"), None)
        if astream is not None:
            rate = astream.rate
            for frame in container.decode(audio=0):
                samples += frame.samples
    return frames / fps, (samples / rate if rate else 0.0)


def test_detect_audio_video_gaps_flags_short_audio(tmp_path):
    """A segment whose audio is materially short is flagged; a matched one isn't."""
    short = tmp_path / "short.mp4"
    full = tmp_path / "full.mp4"
    _write_clip(short, video_seconds=5.0, audio_seconds=1.0)
    _write_clip(full, video_seconds=5.0, audio_seconds=5.0)

    gaps = detect_audio_video_gaps([str(short), str(full)], threshold_seconds=2.5)

    assert len(gaps) == 1
    assert os.path.basename(gaps[0]["path"]) == "short.mp4"
    assert gaps[0]["kind"] == "short"
    assert gaps[0]["gap_seconds"] == pytest.approx(4.0, abs=0.7)


def test_detect_audio_video_gaps_flags_long_audio(tmp_path):
    """A segment whose audio overruns its video is flagged as 'long'."""
    long_clip = tmp_path / "long.mp4"
    full = tmp_path / "full.mp4"
    _write_clip(long_clip, video_seconds=3.0, audio_seconds=7.0)
    _write_clip(full, video_seconds=5.0, audio_seconds=5.0)

    gaps = detect_audio_video_gaps([str(long_clip), str(full)], threshold_seconds=2.5)

    assert len(gaps) == 1
    assert os.path.basename(gaps[0]["path"]) == "long.mp4"
    assert gaps[0]["kind"] == "long"
    assert gaps[0]["gap_seconds"] == pytest.approx(4.0, abs=0.7)


@pytest.mark.asyncio
async def test_combine_pads_short_audio_segment(tmp_path):
    """Combining a short-audio segment pads it so audio stays aligned to video.

    Without the fix the combined audio would be ~2s shorter than the video (the
    un-padded shortfall), drifting every later segment out of sync.
    """
    aligned = tmp_path / "a.mp4"
    short = tmp_path / "b.mp4"
    out = tmp_path / "combined.mp4"
    _write_clip(aligned, video_seconds=3.0, audio_seconds=3.0)
    _write_clip(short, video_seconds=3.0, audio_seconds=1.0)

    ok = await combine_videos([str(aligned), str(short)], str(out))
    assert ok is True

    video_seconds, audio_seconds = _decode_durations(out)
    # Audio must be padded up to (near) the full ~6s of video, not the ~4s it
    # would be if the short segment's missing audio were left as a gap.
    assert audio_seconds == pytest.approx(video_seconds, abs=1.0)
    assert audio_seconds > 4.5


@pytest.mark.asyncio
async def test_combine_trims_long_audio_segment(tmp_path):
    """Combining a segment whose audio overruns its video trims the excess.

    Without trimming the combined audio would be ~8s (3 + 5) against ~6s of
    video, pushing the second half of the game ahead of its picture.
    """
    aligned = tmp_path / "a.mp4"
    long_clip = tmp_path / "b.mp4"
    out = tmp_path / "combined.mp4"
    _write_clip(aligned, video_seconds=3.0, audio_seconds=3.0)
    _write_clip(long_clip, video_seconds=3.0, audio_seconds=5.0)

    ok = await combine_videos([str(aligned), str(long_clip)], str(out))
    assert ok is True

    video_seconds, audio_seconds = _decode_durations(out)
    # The 5s audio of the second clip is trimmed to ~its 3s video, so combined
    # audio tracks the ~6s of video rather than running out to ~8s.
    assert audio_seconds == pytest.approx(video_seconds, abs=1.0)
    assert audio_seconds < 7.0


# --- Decode-corruption (byte-complete-but-corrupt) tests ---------------------
#
# These model the 06.08 case: a segment downloaded at EXACTLY the camera-
# reported size (PR #88's completeness check passes) yet decodes fine for a
# while and then hits an undecodable HEVC region (InvalidDataError from
# avcodec_send_packet). A size check can't see it and a stream-copy mux can't
# either (a pure mux never decodes). Only forcing a decode trips on it. Real
# media is required: a mocked av.open can't actually fail mid-stream.


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
    """Zero a byte range deep in the mdat so the file decodes for a while and
    then hits an undecodable region — the byte-complete-but-corrupt case."""
    data = bytearray(path.read_bytes())
    n = len(data)
    for i in range(int(n * frac_start), int(n * frac_end)):
        data[i] = 0
    path.write_bytes(bytes(data))


@pytest.mark.asyncio
async def test_decode_probe_clean_vs_corrupt(tmp_path):
    """decode_probe returns None for a clean clip and a corrupt-start second for
    one that decodes partway then fails."""
    clean = tmp_path / "clean.mp4"
    corrupt = tmp_path / "corrupt.mp4"
    _write_decodable_clip(clean, video_seconds=20.0)
    _write_decodable_clip(corrupt, video_seconds=20.0)
    _corrupt_mid_file(corrupt)

    assert await decode_probe(str(clean)) is None

    corrupt_start = await decode_probe(str(corrupt))
    assert corrupt_start is not None
    # The corruption sits in the back of the file: it must decode cleanly for a
    # while first (proving this is the mid-file case, not a wholly-broken file)
    # and the cut point must land before the segment's full 20s.
    assert 1.0 < corrupt_start < 20.0


def test_detect_video_decode_corruption_flags_only_corrupt(tmp_path):
    """The detector flags the corrupt segment with its lost extent, not the
    clean one."""
    clean = tmp_path / "clean.mp4"
    corrupt = tmp_path / "corrupt.mp4"
    _write_decodable_clip(clean, video_seconds=20.0)
    _write_decodable_clip(corrupt, video_seconds=20.0)
    _corrupt_mid_file(corrupt)

    found = detect_video_decode_corruption([str(clean), str(corrupt)])

    assert len(found) == 1
    entry = found[0]
    assert os.path.basename(entry["path"]) == "corrupt.mp4"
    assert entry["lost_seconds"] > 0
    assert entry["corrupt_start_seconds"] < entry["video_seconds"]


@pytest.mark.asyncio
async def test_combine_degrades_corrupt_segment(tmp_path):
    """Combining a corrupt segment cuts the dead video region instead of muxing
    garbage: the combined output decodes clean end-to-end, is shorter than the
    naive sum by ~the lost span, and audio stays aligned to the kept video."""
    good = tmp_path / "a.mp4"
    bad = tmp_path / "b.mp4"
    out = tmp_path / "combined.mp4"
    _write_decodable_clip(good, video_seconds=20.0)
    _write_decodable_clip(bad, video_seconds=20.0)
    _corrupt_mid_file(bad)

    corruption = detect_video_decode_corruption([str(good), str(bad)])
    assert len(corruption) == 1
    corrupt_starts = {c["path"]: c["corrupt_start_seconds"] for c in corruption}

    ok = await combine_videos(
        [str(good), str(bad)], str(out), corrupt_starts=corrupt_starts
    )
    assert ok is True

    # The whole combined output must now decode cleanly — the garbage tail of
    # the bad segment was cut, not muxed (this is what a naive stream-copy got
    # wrong: it would silently ship the corrupt span).
    assert await decode_probe(str(out)) is None

    video_seconds, audio_seconds = _decode_durations(out)
    # ~20s good + the kept (pre-corruption) part of bad, so well under the
    # naive 40s sum, and audio stays aligned to the kept video.
    assert video_seconds < 38.0
    assert audio_seconds == pytest.approx(video_seconds, abs=1.5)


@pytest.mark.asyncio
async def test_combine_clean_segments_unaffected(tmp_path):
    """A combine with no corruption still produces a clean, full-length output
    (the degrade path is inert when corrupt_starts is empty)."""
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    out = tmp_path / "combined.mp4"
    _write_decodable_clip(a, video_seconds=6.0)
    _write_decodable_clip(b, video_seconds=6.0)

    assert detect_video_decode_corruption([str(a), str(b)]) == []

    ok = await combine_videos([str(a), str(b)], str(out))
    assert ok is True
    assert await decode_probe(str(out)) is None
    video_seconds, _ = _decode_durations(out)
    assert video_seconds == pytest.approx(12.0, abs=1.5)
