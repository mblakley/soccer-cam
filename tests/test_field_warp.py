"""Unit tests for training.data_prep.field_warp.

No real video needed -- builds warps from synthetic gradients and verifies the
forward/inverse round-trip, the no-upscale guarantee, ball-size uniformity,
dimensions/compression reporting, and edge cases.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from training.data_prep.field_warp import (
    DEFAULT_TARGET_WIDTH,
    FieldWarp,
    build_field_warp,
    unwarp_points,
    warp_frame,
    warp_points,
)

# Production-shaped synthetic camera.
SRC_W = 7680
SRC_H = 2160
Y_TOP = 700
Y_BOT = 1450


def _gradient(
    y_top: int = Y_TOP,
    y_bot: int = Y_BOT,
    far_size: float = 8.5,
    near_size: float = 33.0,
    n: int = 400,
    seed: int = 0,
) -> tuple[np.ndarray, np.ndarray]:
    """Synthetic monotone ball-size-vs-row gradient (far small -> near large).

    Returns (rows, sizes). Adds a little noise so the band-median fit and the
    monotone-enforcement path are exercised, not a perfectly clean line.
    """
    rng = np.random.default_rng(seed)
    rows = rng.uniform(y_top, y_bot, size=n)
    frac = (rows - y_top) / (y_bot - y_top)
    sizes = far_size + frac * (near_size - far_size)
    sizes = sizes + rng.normal(0.0, 0.4, size=n)
    sizes = np.clip(sizes, 1.0, None)
    return rows, sizes


def _build() -> FieldWarp:
    rows, sizes = _gradient()
    return build_field_warp(
        rows, sizes, SRC_W, SRC_H, target_width=DEFAULT_TARGET_WIDTH
    )


# --------------------------------------------------------------------------- #
# Round-trip accuracy -- the most important test.
# --------------------------------------------------------------------------- #


def test_roundtrip_grid_within_2px() -> None:
    """Forward-then-inverse recovers source points within sub-2px tolerance."""
    warp = _build()

    # A grid of source points spanning the band interior and the full width.
    xs = np.linspace(50, SRC_W - 50, 9)
    ys = np.linspace(warp.y_top + 1, warp.y_bot - 1, 13)
    grid = np.array([(x, y) for y in ys for x in xs], dtype=np.float64)

    warped_pts = warp_points(grid, warp)
    recovered = unwarp_points(warped_pts, warp)

    err = np.abs(recovered - grid)
    assert err[:, 0].max() < 2.0, f"x error too big: {err[:, 0].max()}"
    assert err[:, 1].max() < 2.0, f"y error too big: {err[:, 1].max()}"


def test_roundtrip_consistent_with_warp_frame_dims() -> None:
    """Points that warp forward land inside the actual warped image bounds."""
    warp = _build()
    frame = np.zeros((SRC_H, SRC_W, 3), dtype=np.uint8)
    warped = warp_frame(frame, warp)
    h, w = warped.shape[:2]

    xs = np.linspace(0, SRC_W - 1, 20)
    ys = np.linspace(warp.y_top, warp.y_bot, 20)
    grid = np.array([(x, y) for y in ys for x in xs], dtype=np.float64)
    wp = warp_points(grid, warp)

    assert wp[:, 0].min() >= -1.0
    assert wp[:, 0].max() <= w + 1.0
    assert wp[:, 1].min() >= -1.0
    assert wp[:, 1].max() <= h + 1.0


def test_unwarp_then_warp_roundtrip() -> None:
    """Inverse-then-forward also round-trips (both directions are inverses)."""
    warp = _build()
    # Points in warped/resized space.
    wxs = np.linspace(1, warp.target_width - 1, 7)
    wys = np.linspace(1, warp.final_h - 1, 7)
    wpts = np.array([(x, y) for y in wys for x in wxs], dtype=np.float64)

    src = unwarp_points(wpts, warp)
    back = warp_points(src, warp)
    err = np.abs(back - wpts)
    assert err.max() < 2.0, f"warped-space round-trip error too big: {err.max()}"


# --------------------------------------------------------------------------- #
# No upscale -- far field is an information ceiling.
# --------------------------------------------------------------------------- #


def test_no_upscale_all_scales_le_1() -> None:
    warp = _build()
    assert np.all(warp.scale > 0.0)
    assert np.all(warp.scale <= 1.0 + 1e-9)


def test_far_rows_preserved_at_native() -> None:
    """The smallest-ball (far) end keeps scale == 1 (never upscaled)."""
    warp = _build()
    # Far end is the small-size end; with a monotone increasing size(row),
    # the minimum size is at the top of the band -> scale == 1 there.
    assert warp.scale.max() == pytest.approx(1.0, abs=1e-9)
    # At least the very far row is exactly native.
    assert warp.scale[0] == pytest.approx(1.0, abs=1e-9)


def test_target_size_is_far_native() -> None:
    rows, sizes = _gradient()
    warp = build_field_warp(rows, sizes, SRC_W, SRC_H)
    # target_size should sit near the far (min) measured size.
    assert warp.target_size <= float(np.median(sizes))
    assert warp.target_size > 0


# --------------------------------------------------------------------------- #
# Uniformity -- a ball at far vs near comes out ~same pixel size.
# --------------------------------------------------------------------------- #


def test_ball_uniformity_far_vs_near() -> None:
    """A drawn 'ball' at far vs near source rows is ~uniform after warping.

    Draw vertical bars whose height equals the local ball size at a far row and
    a near row, apply the vertical anisotropic warp (the part that normalizes
    apparent size), and measure the bar heights. They should come out close,
    even though the source sizes differ by >1.5x.

    Uniformity is a property of the *vertical* warp; the subsequent horizontal
    resize scales both bars equally (and, at the production TW, shrinks them to
    a few px where integer quantization dominates). So this measures on the
    full-resolution vertically-warped image via the warp's own remap maps --
    the same maps :func:`warp_frame` uses before its horizontal resize.
    """
    rows, sizes = _gradient()
    warp = build_field_warp(rows, sizes, SRC_W, SRC_H)

    # Choose a far row (small ball) and a near row (large ball) well inside band.
    far_y = warp.y_top + int(0.1 * warp.band_height)
    near_y = warp.y_top + int(0.9 * warp.band_height)

    # Local ball size at each row from the fitted scale (target / scale = size).
    far_size = warp.target_size / warp.scale[far_y - warp.y_top]
    near_size = warp.target_size / warp.scale[near_y - warp.y_top]
    assert near_size > far_size * 1.5  # confirm a real gradient exists

    frame = np.zeros((SRC_H, SRC_W, 3), dtype=np.uint8)

    def draw_bar(cy: float, size: float, cx: int) -> None:
        half = int(round(size / 2))
        y0 = int(round(cy)) - half
        y1 = int(round(cy)) + half + 1
        frame[y0:y1, cx - 4 : cx + 5] = 255

    draw_bar(far_y, far_size, SRC_W // 4)
    draw_bar(near_y, near_size, 3 * SRC_W // 4)

    # Vertical warp only (no horizontal resize) -- full out_h resolution.
    band = frame[warp.y_top : warp.y_bot + 1]
    warped = cv2.remap(
        band,
        warp.map_x,
        warp.map_y,
        interpolation=cv2.INTER_AREA,
        borderMode=cv2.BORDER_REPLICATE,
    )
    gray = warped[:, :, 0]

    def bar_height(cx_src: int) -> int:
        col = gray[:, cx_src - 4 : cx_src + 5].max(axis=1)
        lit = np.where(col > 40)[0]
        return int(lit.max() - lit.min() + 1) if lit.size else 0

    far_h = bar_height(SRC_W // 4)
    near_h = bar_height(3 * SRC_W // 4)

    assert far_h > 0 and near_h > 0
    # The far bar is near native (scale~1), the near bar is compressed ~3-4x.
    # After normalization the two should land within ~25% of each other,
    # whereas the source sizes differed by >1.5x.
    ratio = max(far_h, near_h) / min(far_h, near_h)
    assert ratio < 1.25, f"warped ball sizes not uniform: far={far_h} near={near_h}"


# --------------------------------------------------------------------------- #
# Dimensions / compression / reporting.
# --------------------------------------------------------------------------- #


def test_out_h_less_than_band_height() -> None:
    warp = _build()
    assert warp.out_h < warp.band_height
    assert warp.vertical_compression > 1.0


def test_warp_frame_output_dims() -> None:
    warp = _build()
    frame = np.zeros((SRC_H, SRC_W, 3), dtype=np.uint8)
    warped = warp_frame(frame, warp)
    assert warped.shape[0] == warp.final_h
    assert warped.shape[1] == warp.target_width
    assert warped.shape[2] == 3
    assert warp.output_shape == (warp.final_h, warp.target_width)


def test_target_width_handling_changes_dims() -> None:
    rows, sizes = _gradient()
    w512 = build_field_warp(rows, sizes, SRC_W, SRC_H, target_width=512)
    w2048 = build_field_warp(rows, sizes, SRC_W, SRC_H, target_width=2048)
    assert w512.target_width == 512
    assert w2048.target_width == 2048
    # Wider target -> taller final image (aspect preserved).
    assert w2048.final_h > w512.final_h


def test_speedup_reporting_positive() -> None:
    warp = _build()
    assert warp.output_megapixels > 0
    assert warp.tiled_megapixels() == pytest.approx(21 * 640 * 640 / 1e6)
    # Single compact warp should push far fewer pixels than 21 tiles.
    assert warp.speedup_vs_tiled() > 1.0


# --------------------------------------------------------------------------- #
# Edge cases.
# --------------------------------------------------------------------------- #


def test_point_at_y_top_and_y_bot_roundtrip() -> None:
    """The exact band edges round-trip within one warped-pixel's source span.

    At the production TW the band's near (bottom) rows are compressed hardest,
    so one warped row covers several source rows there. The round-trip error at
    the exact ``y_bot`` boundary is therefore bounded by that local span (a few
    px), not the sub-px interior accuracy. We assert against that real bound.
    """
    warp = _build()
    pts = np.array(
        [[100.0, float(warp.y_top)], [100.0, float(warp.y_bot)]], dtype=np.float64
    )
    rec = unwarp_points(warp_points(pts, warp), warp)

    # Source rows spanned by one final (resized) warped row at each edge.
    span = warp.band_height / warp.final_h
    assert abs(rec[0, 1] - warp.y_top) < max(2.0, span)
    assert abs(rec[1, 1] - warp.y_bot) < max(2.0, span)


def test_point_outside_band_handled_sanely() -> None:
    """Points above/below the band map without error and clamp to band rows."""
    warp = _build()
    above = np.array([[100.0, float(warp.y_top - 200)]], dtype=np.float64)
    below = np.array([[100.0, float(warp.y_bot + 200)]], dtype=np.float64)

    wa = warp_points(above, warp)
    wb = warp_points(below, warp)
    assert np.all(np.isfinite(wa))
    assert np.all(np.isfinite(wb))
    # np.interp clamps out-of-range inputs to the endpoints: above -> warped 0,
    # below -> warped final_h-ish. Unwarping those returns band edges.
    ra = unwarp_points(wa, warp)
    rb = unwarp_points(wb, warp)
    assert warp.y_top - 1 <= ra[0, 1] <= warp.y_bot + 1
    assert warp.y_top - 1 <= rb[0, 1] <= warp.y_bot + 1


def test_warp_frame_rejects_wrong_size() -> None:
    warp = _build()
    bad = np.zeros((100, 100, 3), dtype=np.uint8)
    with pytest.raises(ValueError, match="does not match warp"):
        warp_frame(bad, warp)


def test_build_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        build_field_warp(np.array([]), np.array([]), SRC_W, SRC_H)
    with pytest.raises(ValueError):
        build_field_warp(np.array([1.0, 2.0]), np.array([5.0]), SRC_W, SRC_H)
    with pytest.raises(ValueError):
        build_field_warp(np.array([1.0]), np.array([-1.0]), SRC_W, SRC_H)
    with pytest.raises(ValueError):
        build_field_warp(np.array([1.0]), np.array([5.0]), 0, SRC_H)


def test_single_band_grayscale_frame() -> None:
    """A 2D (grayscale) frame warps too (cv2.remap/resize keep ndim)."""
    rows, sizes = _gradient()
    warp = build_field_warp(rows, sizes, SRC_W, SRC_H, target_width=640)
    frame = np.zeros((SRC_H, SRC_W), dtype=np.uint8)
    warped = warp_frame(frame, warp)
    assert warped.shape == (warp.final_h, warp.target_width)


def test_inv_lut_monotone_and_bounded() -> None:
    warp = _build()
    assert warp.inv_lut.shape == (warp.out_h,)
    # Non-decreasing and within the band.
    assert np.all(np.diff(warp.inv_lut) >= -1e-9)
    assert warp.inv_lut.min() >= warp.y_top - 1e-6
    assert warp.inv_lut.max() <= warp.y_bot + 1e-6
