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
    _last_keyframe_pts_seconds,
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


def _h264_codec_available():
    """True if a usable H.264 encoder is present (real-GOP fixtures need it)."""
    import av

    for name in ("libx264", "h264", "h264_nvenc"):
        try:
            if av.codec.Codec(name, "w") is not None:
                return name
        except Exception:
            continue
    return None


def _write_gop_clip(path, video_seconds, fps=10, gop_size=12, rate=16000, codec=None):
    """Write a real mp4 with genuine GOP structure: an H.264 stream with a
    keyframe every ``gop_size`` frames and P/B frames in between.

    This is the fixture that exposes the keyframe-aware-cut defect. With the old
    mid-GOP cut, dropping packets at the raw corrupt second leaves dangling P/B
    frames whose GOP is incomplete, so the combined output explodes
    ``avcodec_send_packet`` on decode. Only ending on a GOP boundary (the last
    keyframe at/before the cut) keeps the output decodable — which is exactly
    what a flat all-I-frame mpeg4 fixture could never prove.
    """
    import av
    import numpy as np

    codec = codec or _h264_codec_available()
    with av.open(str(path), "w", format="mp4") as container:
        vstream = container.add_stream(codec, rate=fps)
        vstream.width = 64
        vstream.height = 64
        vstream.pix_fmt = "yuv420p"
        vstream.gop_size = gop_size
        # Deterministic, real keyframe interval; let the encoder also place
        # keyframes only at GOP boundaries (no scene-cut keyframes).
        vstream.options = {
            "g": str(gop_size),
            "keyint_min": str(gop_size),
            "sc_threshold": "0",
        }
        astream = container.add_stream("aac", rate=rate)

        rng = np.random.default_rng(0)
        n_frames = int(round(video_seconds * fps))
        for i in range(n_frames):
            # Moving, non-flat content so P/B frames carry real residual.
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


def _keyframe_pts_seconds(path):
    """Return the sorted list of keyframe pts (seconds) in the video stream."""
    import av

    kfs = []
    with av.open(str(path)) as container:
        vstream = next(s for s in container.streams if s.type == "video")
        tb = vstream.time_base
        for packet in container.demux(vstream):
            if packet.pts is not None and packet.is_keyframe:
                kfs.append(float(packet.pts * tb))
    return sorted(kfs)


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


# --- Keyframe-aware cut on REAL GOP structure --------------------------------
#
# These are the tests that would have CAUGHT the production defect. The earlier
# corrupt-segment tests use an all-I-frame mpeg4 fixture, so any cut point is a
# self-contained frame boundary and a naive mid-GOP cut "works" by accident. On
# real HEVC/H.264 with a GOP, cutting at the raw corrupt second lands mid-GOP and
# leaves dangling P/B frames whose access unit is incomplete — the combined
# output then explodes ``avcodec_send_packet`` on decode (the 06.08 failure).
# Only ending the muxed stream on a keyframe (GOP boundary) keeps it decodable.


def _full_decode_errors(path):
    """Decode EVERY frame of EVERY stream; return the count of decoded video
    frames and the first decode exception encountered (or None).

    This is the hard end-to-end gate: ``decode_probe`` only samples windows, but
    here we force the decoder over the entire muxed stream so a dangling-GOP
    failure anywhere cannot hide between sampled windows.
    """
    import av

    frames = 0
    with av.open(str(path)) as container:
        streams = [s for s in container.streams if s.type in ("video", "audio")]
        try:
            for packet in container.demux(streams):
                for _frame in packet.decode():
                    if packet.stream.type == "video":
                        frames += 1
        except (av.error.InvalidDataError, av.error.FFmpegError) as exc:
            return frames, exc
    return frames, None


@pytest.mark.asyncio
async def test_combine_corrupt_gop_cuts_on_keyframe_and_fully_decodes(tmp_path):
    """The miniature of the 06.08 case on a REAL GOP structure.

    A clean segment + an H.264 segment with a real keyframe interval that is
    corrupted MID-GOP. The combine must cut on a keyframe boundary so the output
    decodes end-to-end with zero error. A naive mid-GOP cut (the old behavior)
    fails this: it ships dangling P/B frames and the full decode raises.
    """
    codec = _h264_codec_available()
    if codec is None:
        pytest.skip("no H.264 encoder available for a real-GOP fixture")

    good = tmp_path / "seg00_good.mp4"
    bad = tmp_path / "seg01_bad.mp4"
    out = tmp_path / "combined.mp4"
    gop = 12
    fps = 10
    _write_gop_clip(good, video_seconds=8.0, fps=fps, gop_size=gop, codec=codec)
    _write_gop_clip(bad, video_seconds=20.0, fps=fps, gop_size=gop, codec=codec)
    # Corrupt the back of the bad segment so it decodes for a while then fails,
    # with the corruption deliberately landing mid-GOP.
    _corrupt_mid_file(bad, frac_start=0.80, frac_end=0.92)

    corruption = detect_video_decode_corruption([str(good), str(bad)])
    assert len(corruption) == 1
    entry = corruption[0]
    assert os.path.basename(entry["path"]) == "seg01_bad.mp4"
    # The reported cut must be a real keyframe boundary, and lost_seconds must
    # reflect that keyframe cut (>= the raw corrupt-second loss, never less).
    bad_keyframes = _keyframe_pts_seconds(bad)
    assert entry["cut_seconds"] is not None
    assert any(
        entry["cut_seconds"] == pytest.approx(kf, abs=1e-3) for kf in bad_keyframes
    ), f"cut {entry['cut_seconds']} not on a keyframe; kfs={bad_keyframes}"
    assert entry["cut_seconds"] <= entry["corrupt_start_seconds"] + 1e-6
    assert (
        entry["lost_seconds"] >= entry["video_seconds"] - entry["corrupt_start_seconds"]
    )

    # _last_keyframe_pts_seconds (the helper the combine uses to place the cut)
    # must agree it's a keyframe at/before the corrupt second.
    kf = _last_keyframe_pts_seconds(str(bad), entry["corrupt_start_seconds"])
    assert kf is not None
    assert any(kf == pytest.approx(k, abs=1e-3) for k in bad_keyframes)

    corrupt_starts = {c["path"]: c["corrupt_start_seconds"] for c in corruption}
    ok = await combine_videos(
        [str(good), str(bad)], str(out), corrupt_starts=corrupt_starts
    )
    assert ok is True

    # HARD GATE: the entire combined output must decode end-to-end with zero
    # InvalidDataError. This is what the old mid-GOP cut got wrong.
    frames, err = _full_decode_errors(out)
    assert err is None, (
        f"combined output failed full decode after {frames} frames: {err}"
    )
    assert frames > 0
    # Belt-and-suspenders: the sampled probe is also clean.
    assert await decode_probe(str(out)) is None

    # DECISIVE: the combine must cut on the keyframe BOUNDARY, not the raw
    # corrupt second. Measure the actual kept duration of the corrupt segment
    # from the output (output video minus the good segment) and assert it tracks
    # the keyframe cut, NOT corrupt_start. The keyframe cut here is 14.4s and the
    # raw corrupt second is 15.0s — well separated. Under the old mid-GOP cut the
    # kept duration would track corrupt_start (~15.0s); the keyframe-aware cut
    # keeps only up to the keyframe (~14.4-14.5s, inclusive of that I-frame).
    video_seconds, audio_seconds = _decode_durations(out)
    good_seconds, _ = _decode_durations(good)
    kept_bad_seconds = video_seconds - good_seconds
    dist_to_keyframe = abs(kept_bad_seconds - entry["cut_seconds"])
    dist_to_corrupt = abs(kept_bad_seconds - entry["corrupt_start_seconds"])
    assert dist_to_keyframe < dist_to_corrupt, (
        f"kept {kept_bad_seconds:.3f}s tracks the raw corrupt second "
        f"{entry['corrupt_start_seconds']:.3f}s (mid-GOP cut!), not the keyframe "
        f"boundary {entry['cut_seconds']:.3f}s"
    )
    # The keyframe-inclusive cut keeps up to and including the I-frame at
    # cut_seconds, so kept is within ~a couple frames of that keyframe.
    assert kept_bad_seconds == pytest.approx(entry["cut_seconds"], abs=0.35), (
        f"kept {kept_bad_seconds:.3f}s not aligned to keyframe cut "
        f"{entry['cut_seconds']:.3f}s"
    )

    # The dead region was removed: output video is well under the naive 28s sum,
    # and audio tracks the kept video.
    assert video_seconds < 27.0
    assert audio_seconds == pytest.approx(video_seconds, abs=1.5)


@pytest.mark.asyncio
async def test_combine_corruption_in_first_gop_drops_segment_video(tmp_path):
    """If corruption hits the very first GOP (no clean keyframe to end on), the
    segment's video is dropped entirely rather than emitting a broken stream —
    and the combined output still decodes clean.

    The first keyframe of a camera segment is at pts 0, so 'no keyframe
    at/before the cut' means the corruption is before that first keyframe — a
    corrupt_start strictly less than 0. ``_last_keyframe_pts_seconds`` returns
    None there, which is the combine's signal to drop the whole segment's video.
    """
    codec = _h264_codec_available()
    if codec is None:
        pytest.skip("no H.264 encoder available for a real-GOP fixture")

    good = tmp_path / "seg00_good.mp4"
    bad = tmp_path / "seg01_bad.mp4"
    out = tmp_path / "combined.mp4"
    gop = 12
    fps = 10
    _write_gop_clip(good, video_seconds=6.0, fps=fps, gop_size=gop, codec=codec)
    _write_gop_clip(bad, video_seconds=12.0, fps=fps, gop_size=gop, codec=codec)

    # No keyframe precedes a cut before pts 0 => the helper signals "drop".
    assert _last_keyframe_pts_seconds(str(bad), -0.5) is None

    # Drive the combine with that first-GOP-corruption case and confirm the
    # output is clean and contains only the good segment's video.
    ok = await combine_videos(
        [str(good), str(bad)], str(out), corrupt_starts={str(bad): -0.5}
    )
    assert ok is True
    frames, err = _full_decode_errors(out)
    assert err is None, f"output failed full decode after {frames} frames: {err}"
    video_seconds, _ = _decode_durations(out)
    # Only the ~6s good segment survives (bad segment's video fully dropped).
    assert video_seconds == pytest.approx(6.0, abs=1.5)
