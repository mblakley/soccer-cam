"""Unit tests for video_grouper.utils.stitch_remap."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from video_grouper.utils.stitch_remap import (
    StitchProfile,
    apply_shift_to_frame_nv12,
    apply_shift_to_frame_rgb,
    build_dx_lookup,
    load_profile,
    write_profile,
)


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
    """Left half (x < seam_x) must be unchanged; right half gets rolled."""
    h, w = 10, 20
    seam_x = 10
    y = np.zeros((h, w), dtype=np.uint8)
    y[:, :seam_x] = 1  # left half
    y[:, seam_x:] = np.arange(w - seam_x, dtype=np.uint8)  # 0..9 in right half

    uv = np.zeros((h // 2, w), dtype=np.uint8)
    dx = np.full(h, -2, dtype=np.int32)  # roll right half by -2

    apply_shift_to_frame_nv12(y, uv, dx, seam_x)

    # Left half unchanged
    assert (y[:, :seam_x] == 1).all()
    # Right half: np.roll(arange(10), -2) → [2,3,4,5,6,7,8,9,0,1]
    np.testing.assert_array_equal(
        y[0, seam_x:], np.array([2, 3, 4, 5, 6, 7, 8, 9, 0, 1])
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
    # Right-half pixel at x=seam_x originally held (0, 10, 20); after roll -3 the pixel
    # now at x=seam_x came from x=seam_x+3, i.e. (3, 13, 23)
    np.testing.assert_array_equal(out[0, seam_x], [3, 13, 23])
