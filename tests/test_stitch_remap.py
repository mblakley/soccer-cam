"""Unit tests for video_grouper.utils.stitch_remap (and the step that uses it)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from video_grouper.utils.stitch_remap import (
    StitchProfile,
    apply_shift_to_frame_nv12,
    apply_shift_to_frame_rgb,
    build_dx_lookup,
    chroma_aligned_dx,
    load_profile,
    shift_no_wrap,
    write_profile,
)


@pytest.fixture(autouse=True)
def mock_ffmpeg():
    """Override conftest's autouse PyAV mock for this module.

    ``test_add_output_stream_applies_spec_to_a_real_container`` needs the real
    ``av.open`` to prove PyAV accepts every attribute the spec sets; nothing
    here encodes a frame, so real PyAV is cheap. Same override pattern as
    tests/test_audio_padding.py.
    """
    yield


SAMPLE_PROFILE = StitchProfile(
    source_width=7680,
    source_height=2160,
    seam_x=3840,
    dx_anchors=[(0, -10), (477, -20), (657, -35), (1500, 0), (2160, 0)],
)


def test_profile_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "profile.json"
    write_profile(SAMPLE_PROFILE, p)
    loaded = load_profile(p)
    assert loaded == SAMPLE_PROFILE


def test_load_profile_missing(tmp_path: Path) -> None:
    assert load_profile(tmp_path / "missing.json") is None


def test_load_profile_invalid_json(tmp_path: Path) -> None:
    p = tmp_path / "broken.json"
    p.write_text("not json")
    assert load_profile(p) is None


def test_load_profile_missing_key(tmp_path: Path) -> None:
    p = tmp_path / "partial.json"
    p.write_text(json.dumps({"source_width": 7680, "source_height": 2160}))
    assert load_profile(p) is None


def test_build_dx_lookup_identity_resolution() -> None:
    lookup = build_dx_lookup(SAMPLE_PROFILE, 7680, 2160)
    assert lookup.shape == (2160,)
    # Anchors must reproduce exactly
    assert lookup[0] == -10
    assert lookup[477] == -20
    assert lookup[657] == -35
    assert lookup[1500] == 0
    assert lookup[2159] == 0


def test_build_dx_lookup_halves_at_half_resolution() -> None:
    """dx values scale with width; y anchors scale with height."""
    lookup = build_dx_lookup(SAMPLE_PROFILE, 3840, 1080)
    assert lookup.shape == (1080,)
    # Anchor at y=657 (source) → y≈328 (half). dx at that y should be ~-18 (half of -35).
    assert abs(lookup[328] - (-18)) <= 1
    # End of frame is 0 in source; still 0 at half res
    assert lookup[1079] == 0


def test_apply_shift_nv12_skips_zero_rows() -> None:
    """A row with dx=0 must be left untouched."""
    h, w = 100, 100
    seam_x = 50
    y = np.arange(h * w, dtype=np.uint8).reshape(h, w)
    uv = np.arange((h // 2) * w, dtype=np.uint8).reshape(h // 2, w)
    dx = np.zeros(h, dtype=np.int32)

    y_orig = y.copy()
    uv_orig = uv.copy()
    apply_shift_to_frame_nv12(y, uv, dx, seam_x)
    np.testing.assert_array_equal(y, y_orig)
    np.testing.assert_array_equal(uv, uv_orig)


def test_apply_shift_nv12_shifts_right_half_only() -> None:
    """Left half (x < seam_x) must be unchanged; right half gets shifted."""
    h, w = 10, 20
    seam_x = 10
    y = np.zeros((h, w), dtype=np.uint8)
    y[:, :seam_x] = 1  # left half
    y[:, seam_x:] = np.arange(w - seam_x, dtype=np.uint8)  # 0..9 in right half

    uv = np.zeros((h // 2, w), dtype=np.uint8)
    dx = np.full(h, -2, dtype=np.int32)  # shift right half by -2

    apply_shift_to_frame_nv12(y, uv, dx, seam_x)

    # Left half unchanged
    assert (y[:, :seam_x] == 1).all()
    # Right half shifted left by 2, outer edge replicated (NOT wrapped: the old
    # np.roll put the seam's 0,1 back at the panorama's far right edge)
    np.testing.assert_array_equal(
        y[0, seam_x:], np.array([2, 3, 4, 5, 6, 7, 8, 9, 9, 9])
    )


def test_apply_shift_rgb_shifts_right_half_only() -> None:
    h, w = 8, 12
    seam_x = 6
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    frame[:, :seam_x] = [1, 2, 3]
    # Put distinct pixels across the right half so we can check the roll
    for x in range(w - seam_x):
        frame[:, seam_x + x] = [x, x + 10, x + 20]
    dx = np.full(h, -3, dtype=np.int32)
    out = apply_shift_to_frame_rgb(frame, dx, seam_x)

    # Left half unchanged
    assert (out[:, :seam_x] == frame[:, :seam_x]).all()
    # Right-half pixel at x=seam_x originally held (0, 10, 20); after shifting -3 the
    # pixel now at x=seam_x came from x=seam_x+3, i.e. (3, 13, 23)
    np.testing.assert_array_equal(out[0, seam_x], [3, 13, 23])


# --- Defect 2: the shift must not wrap content into the seam ----------------


def test_shift_no_wrap_positive_replicates_head() -> None:
    seg = np.arange(8, dtype=np.uint8)
    out = shift_no_wrap(seg, 3)
    # np.roll would have given [5,6,7,0,1,2,3,4] — the tail landing at the head.
    np.testing.assert_array_equal(out, [0, 0, 0, 0, 1, 2, 3, 4])


def test_shift_no_wrap_negative_replicates_tail() -> None:
    seg = np.arange(8, dtype=np.uint8)
    out = shift_no_wrap(seg, -3)
    np.testing.assert_array_equal(out, [3, 4, 5, 6, 7, 7, 7, 7])


def test_shift_no_wrap_zero_is_identity() -> None:
    seg = np.arange(8, dtype=np.uint8)
    np.testing.assert_array_equal(shift_no_wrap(seg, 0), seg)


def test_shift_no_wrap_beyond_segment_width() -> None:
    """|dx| >= width: everything shifts out, the surviving edge fills the row."""
    seg = np.arange(4, dtype=np.uint8)
    np.testing.assert_array_equal(shift_no_wrap(seg, 9), [0, 0, 0, 0])
    np.testing.assert_array_equal(shift_no_wrap(seg, -9), [3, 3, 3, 3])
    np.testing.assert_array_equal(shift_no_wrap(np.empty(0, dtype=np.uint8), 3), [])


def test_apply_shift_rgb_never_wraps_far_edge_into_seam() -> None:
    """dx>0 must not roll the panorama's far right edge into the seam columns."""
    h, w = 4, 16
    seam_x = 8
    dx = 3
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    frame[:, :seam_x] = [1, 1, 1]
    frame[:, seam_x:] = [2, 2, 2]
    # A marker only at the far right edge of the panorama — exactly what np.roll
    # would have wrapped into the seam.
    frame[:, w - dx :] = [255, 0, 0]

    out = apply_shift_to_frame_rgb(frame, np.full(h, dx, dtype=np.int32), seam_x)

    seam_cols = out[:, seam_x : seam_x + dx]
    assert not (seam_cols == [255, 0, 0]).all(axis=-1).any(), (
        "far-edge content wrapped into the seam columns"
    )
    # Replicate: the seam columns hold a copy of what was the first right-half pixel
    assert (seam_cols == [2, 2, 2]).all()


def test_apply_shift_nv12_never_wraps_far_edge_into_seam() -> None:
    h, w = 4, 16
    seam_x = 8
    dx = 3
    y = np.full((h, w), 2, dtype=np.uint8)
    y[:, :seam_x] = 1
    y[:, w - dx :] = 255  # far-edge marker
    uv = np.zeros((h // 2, w), dtype=np.uint8)

    apply_shift_to_frame_nv12(y, uv, np.full(h, dx, dtype=np.int32), seam_x)

    # dx=3 rounds to 2 for chroma alignment, so 2 seam columns are vacated.
    assert not (y[:, seam_x : seam_x + 2] == 255).any()
    assert (y[:, seam_x : seam_x + 2] == 2).all()


# --- Defect 3: luma and chroma must agree ----------------------------------


def test_chroma_aligned_dx_rounds_toward_zero_symmetrically() -> None:
    assert chroma_aligned_dx(0) == 0
    assert chroma_aligned_dx(2) == 2
    assert chroma_aligned_dx(-2) == -2
    # Odd values round toward zero, and the sign flip is symmetric — the old
    # `(dx // 2) * 2` gave -36 for -35 and +34 for +35, a leftward bias.
    assert chroma_aligned_dx(35) == 34
    assert chroma_aligned_dx(-35) == -34
    assert chroma_aligned_dx(1) == 0
    assert chroma_aligned_dx(-1) == 0
    for dx in range(-40, 41):
        assert chroma_aligned_dx(dx) == -chroma_aligned_dx(-dx)
        assert chroma_aligned_dx(dx) % 2 == 0
        assert abs(chroma_aligned_dx(dx)) <= abs(dx)


def _edge_column_luma(row: np.ndarray, marker: int) -> int:
    """First column of `row` holding `marker`."""
    hits = np.nonzero(row == marker)[0]
    assert hits.size, "marker not found"
    return int(hits[0])


def test_apply_shift_nv12_luma_and_chroma_land_on_the_same_column() -> None:
    """An odd dx must not leave luma and chroma disagreeing at the seam."""
    h, w = 4, 32
    seam_x = 8
    edge_x = 16  # a vertical edge at luma column 16 (chroma sample 8)

    for dx in (3, -3, 5, -5):
        y = np.zeros((h, w), dtype=np.uint8)
        y[:, edge_x:] = 200
        uv = np.zeros((h // 2, w), dtype=np.uint8)
        # Byte index in the interleaved UV row == luma column (chroma sample c
        # sits at bytes 2c, 2c+1 and covers luma columns 2c, 2c+1).
        uv[:, edge_x::2] = 200  # U of every chroma sample right of the edge
        uv[:, edge_x + 1 :: 2] = 100  # its V

        apply_shift_to_frame_nv12(y, uv, np.full(h, dx, dtype=np.int32), seam_x)

        luma_edge = _edge_column_luma(y[0], 200)
        chroma_edge = _edge_column_luma(uv[0], 200)
        assert luma_edge == chroma_edge, (
            f"dx={dx}: luma edge at {luma_edge}, chroma edge at {chroma_edge}"
        )
        # ...and both landed on the chroma-aligned shift, not the raw dx.
        assert luma_edge == edge_x + chroma_aligned_dx(dx)


def test_apply_shift_nv12_replicates_whole_chroma_pairs() -> None:
    """The vacated chroma strip must hold (U, V) pairs, not one smeared byte."""
    h, w = 2, 16
    seam_x = 4
    y = np.zeros((h, w), dtype=np.uint8)
    uv = np.zeros((h // 2, w), dtype=np.uint8)
    uv[0, ::2] = 20  # U
    uv[0, 1::2] = 90  # V
    uv[0, -2:] = [21, 91]  # distinct final pair

    apply_shift_to_frame_nv12(y, uv, np.full(h, -4, dtype=np.int32), seam_x)

    # 4 luma px = 2 chroma samples vacated at the outer edge; both are copies of
    # the final (U, V) pair, so U stays U and V stays V.
    np.testing.assert_array_equal(uv[0, -4:], [21, 91, 21, 91])


# --- Defect 1: the correction must not silently downgrade the encode -------


@dataclass
class _FakeCodecContext:
    name: str = "hevc"
    pix_fmt: str = "yuv420p"
    color_range: int = 1
    colorspace: int = 5
    color_primaries: int = 1
    color_trc: int = 1


@dataclass
class _FakeVideoStream:
    """Duck-typed stand-in for av.video.stream.VideoStream."""

    codec_context: Any
    width: int = 7680
    height: int = 2160
    average_rate: Any = Fraction(20, 1)
    bit_rate: int | None = 20_480_000
    time_base: Any = Fraction(1, 10240)


def _fake_encoder_lookup(name: str) -> tuple[str, frozenset[str]]:
    return name, frozenset({"yuv420p", "yuv420p10le"})


def test_output_stream_spec_carries_source_encode_settings() -> None:
    """The camera's 7680x2160 HEVC @ 20480 kbps must survive the correction."""
    from video_grouper.pipeline.steps.stitch_correct import _output_stream_spec

    stream = _FakeVideoStream(codec_context=_FakeCodecContext())
    spec = _output_stream_spec(stream, encoder_lookup=_fake_encoder_lookup)

    assert spec.codec_name == "hevc"  # not the hardcoded "h264"
    assert spec.bit_rate == 20_480_000  # not PyAV's ~1 Mbps default
    assert spec.pix_fmt == "yuv420p"
    assert spec.rate == Fraction(20, 1)
    assert spec.time_base == Fraction(1, 10240)
    assert spec.width == 7680
    assert spec.height == 2160
    # Colour metadata carried so the copy is interpreted as the source was
    assert spec.color_range == 1
    assert spec.colorspace == 5
    assert spec.color_primaries == 1
    assert spec.color_trc == 1


def test_output_stream_spec_carries_10bit_pix_fmt() -> None:
    from video_grouper.pipeline.steps.stitch_correct import _output_stream_spec

    stream = _FakeVideoStream(codec_context=_FakeCodecContext(pix_fmt="yuv420p10le"))
    spec = _output_stream_spec(stream, encoder_lookup=_fake_encoder_lookup)
    assert spec.pix_fmt == "yuv420p10le"


def test_output_stream_spec_downgrades_pix_fmt_the_encoder_cannot_take() -> None:
    from video_grouper.pipeline.steps.stitch_correct import _output_stream_spec

    stream = _FakeVideoStream(codec_context=_FakeCodecContext(pix_fmt="yuv444p12le"))
    spec = _output_stream_spec(stream, encoder_lookup=_fake_encoder_lookup)
    assert spec.pix_fmt == "yuv420p"


def test_output_stream_spec_falls_back_to_container_bitrate() -> None:
    from video_grouper.pipeline.steps.stitch_correct import _output_stream_spec

    stream = _FakeVideoStream(codec_context=_FakeCodecContext(), bit_rate=None)
    spec = _output_stream_spec(
        stream, container_bit_rate=20_600_000, encoder_lookup=_fake_encoder_lookup
    )
    assert spec.bit_rate == 20_600_000


def test_output_stream_spec_bitrate_fallback_matches_the_camera() -> None:
    """No declared bitrate anywhere: derive it from the camera's own bpp."""
    from video_grouper.pipeline.steps.stitch_correct import _output_stream_spec

    stream = _FakeVideoStream(codec_context=_FakeCodecContext(), bit_rate=0)
    spec = _output_stream_spec(
        stream, container_bit_rate=0, encoder_lookup=_fake_encoder_lookup
    )
    # 7680x2160 @ 20 fps is exactly the encode the constant was derived from
    assert spec.bit_rate == 20_480_000
    # Half the frame area at the same rate ⇒ half the budget
    half = _FakeVideoStream(
        codec_context=_FakeCodecContext(), bit_rate=0, width=3840, height=2160
    )
    half_spec = _output_stream_spec(
        half, container_bit_rate=None, encoder_lookup=_fake_encoder_lookup
    )
    assert half_spec.bit_rate == 10_240_000


def test_resolve_encoder_keeps_hevc_and_falls_back_for_unknown() -> None:
    """Against real PyAV: no encode, just codec lookup."""
    from video_grouper.pipeline.steps.stitch_correct import _resolve_encoder

    name, pix_fmts = _resolve_encoder("hevc")
    assert name == "hevc"
    assert "yuv420p" in pix_fmts

    name, _ = _resolve_encoder("no_such_codec")
    assert name == "h264"


def test_add_output_stream_applies_spec_to_a_real_container(tmp_path: Path) -> None:
    """PyAV must accept every attribute the spec sets (no frames encoded)."""
    import av

    from video_grouper.pipeline.steps.stitch_correct import (
        _add_output_stream,
        _output_stream_spec,
    )

    stream = _FakeVideoStream(
        codec_context=_FakeCodecContext(), width=64, height=48, bit_rate=1_234_000
    )
    spec = _output_stream_spec(stream, encoder_lookup=_fake_encoder_lookup)

    container = av.open(str(tmp_path / "out.mp4"), mode="w")
    try:
        out_stream = _add_output_stream(container, spec)
        # The encoder for "hevc" is libx265 — same codec id, so the corrected
        # copy is still H.265 rather than the old hardcoded H.264.
        assert out_stream.codec_context.codec.id == av.Codec("hevc", "r").id
        assert out_stream.codec_context.bit_rate == 1_234_000
        assert out_stream.codec_context.pix_fmt == "yuv420p"
        assert out_stream.codec_context.time_base == Fraction(1, 10240)
        assert out_stream.width == 64
        assert out_stream.height == 48
    finally:
        container.close()
