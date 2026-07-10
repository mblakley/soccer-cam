"""Heatmap candidate detector (product): peaks, tiling, coordinate mapping."""

from __future__ import annotations

import numpy as np

from video_grouper.inference.ball_detector import (
    _pad8,
    extract_peaks,
    infer_band,
)
from video_grouper.inference.iso_warp import (
    CropIsoWarp,
    band_mask,
    expand_polygon,
    far_margin_polygon,
    field_band_from_polygon,
    native_iso_warp,
)


class _StubSession:
    """onnxruntime-session stand-in: heatmap = mean of the 3 input frames."""

    calls: int = 0

    def get_inputs(self):
        class _In:
            name = "frames"

        return [_In()]

    def run(self, _outs, feeds):
        x = feeds["frames"]  # (1, 3, H, W)
        self.calls = getattr(self, "calls", 0) + 1
        return [x.mean(axis=1, keepdims=True)]


def test_extract_peaks_finds_local_maxima_score_descending():
    hm = np.zeros((64, 96), np.float32)
    hm[10, 20] = 0.9
    hm[40, 70] = 0.5
    hm[41, 71] = 0.45  # suppressed: within the NMS radius of (40, 70)
    peaks = extract_peaks(hm, top_k=8, threshold=0.1, min_distance=3)
    assert [(x, y) for x, y, _s in peaks] == [(20.0, 10.0), (70.0, 40.0)]
    assert peaks[0][2] > peaks[1][2]


def test_extract_peaks_threshold_and_topk():
    hm = np.zeros((32, 32), np.float32)
    hm[5, 5] = 0.05  # below floor
    hm[20, 20] = 0.6
    hm[10, 25] = 0.3
    assert len(extract_peaks(hm, top_k=8, threshold=0.1)) == 2
    assert len(extract_peaks(hm, top_k=1, threshold=0.1)) == 1


def test_pad8_pads_to_multiples_of_8():
    a = np.zeros((3, 30, 100), np.float32)
    padded, h, w = _pad8(a)
    assert (h, w) == (30, 100)
    assert padded.shape == (3, 32, 104)


def test_infer_band_tiles_and_stitches():
    sess = _StubSession()
    stack = np.random.default_rng(0).random((3, 40, 700), dtype=np.float32)
    hm = infer_band(sess, stack, tile_w=256, overlap=64)
    assert hm.shape == (40, 700)
    assert sess.calls > 1  # actually tiled
    np.testing.assert_allclose(hm, stack.mean(axis=0), atol=1e-6)


def test_band_coordinate_round_trip():
    """Band coords -> source px uses (scale, y_top) exactly like warp.points."""
    poly = np.array(
        [
            [100, 1000],
            [500, 1010],
            [960, 1015],
            [1420, 1010],
            [1820, 1000],
            [1600, 300],
            [1280, 295],
            [960, 290],
            [640, 295],
            [320, 300],
        ],
        float,
    )
    far = far_margin_polygon(poly, 100.0)
    assert far[5:, 1].max() <= 200.0
    warp = native_iso_warp(far, 1920, 1080, target_width=960)
    assert isinstance(warp, CropIsoWarp)
    yt, yb = field_band_from_polygon(far)
    assert (warp.y_top, warp.y_bot) == (yt, yb)
    src_pt = np.array([[700.0, 800.0]])
    bx, by = warp.points(src_pt)[0]
    # the detector maps peaks back with x / scale, y / scale + y_top
    assert bx / warp.scale == 700.0
    assert by / warp.scale + warp.y_top == 800.0
    mask = band_mask(warp, far)
    assert mask.shape == warp.shape
    assert mask.max() == 255


def test_expand_polygon_widens_boundaries_for_oob():
    """The end-line/dome margin: a behind-goal point outside the raw polygon lands
    INSIDE the expanded polygon (so the detector can see out-of-play exits)."""
    import cv2

    poly = np.array(
        [
            [100, 1000],
            [500, 1010],
            [960, 1015],
            [1420, 1010],
            [1820, 1000],
            [1600, 300],
            [1280, 295],
            [960, 290],
            [640, 295],
            [320, 300],
        ],
        float,
    )
    # a point just outside the right end line (behind the goal)
    behind_goal = (1880.0, 950.0)
    raw = cv2.pointPolygonTest(
        poly.astype(np.float32).reshape(-1, 1, 2), behind_goal, False
    )
    assert raw < 0  # outside today
    exp = expand_polygon(poly, 150.0)
    now = cv2.pointPolygonTest(
        exp.astype(np.float32).reshape(-1, 1, 2), behind_goal, False
    )
    assert now >= 0  # inside after the margin
    # margin 0 is a no-op (legacy)
    np.testing.assert_allclose(expand_polygon(poly, 0.0), poly)
