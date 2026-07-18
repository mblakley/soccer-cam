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


def _textured_band(h=200, w=800, seed=3):
    """Structured band with enough texture for phase correlation to lock onto."""
    rng = np.random.default_rng(seed)
    img = (rng.random((h, w)) * 60 + 60).astype(np.float32)
    for x in range(50, w, 120):  # field-line-ish verticals
        img[:, x : x + 3] += 120
    for y in range(30, h, 60):
        img[y : y + 2, :] += 100
    return np.clip(img, 0, 255).astype(np.uint8)


def _shift_img(img, dx, dy):
    import cv2

    m = np.float32([[1, 0, dx], [0, 1, dy]])
    return cv2.warpAffine(
        img, m, (img.shape[1], img.shape[0]), flags=cv2.INTER_LINEAR, borderValue=0
    )


def test_band_stabilizer_first_frame_is_anchor():
    from video_grouper.inference.iso_warp import BandStabilizer

    stab = BandStabilizer()
    img = _textured_band()
    out = stab.align(img)
    assert stab.last == (0.0, 0.0)
    np.testing.assert_array_equal(out, img)


def test_band_stabilizer_estimates_and_undoes_known_shift():
    """A wind gust = translated content: estimate ~= the true shift, and align
    warps the content back onto the anchor (EXP-DIST-57)."""
    from video_grouper.inference.iso_warp import BandStabilizer

    img = _textured_band()
    dx, dy = 9.0, -6.0
    shifted = _shift_img(img, dx, dy)
    stab = BandStabilizer()
    stab.align(img)  # anchor
    aligned = stab.align(shifted)
    edx, edy = stab.last
    assert abs(edx - dx) < 1.0 and abs(edy - dy) < 1.0
    # the aligned frame is registered to the anchor: residual shift ~ 0
    # (pixel-diff would punish sub-pixel interpolation smoothing on this
    # noise texture — registration is the property that matters)
    probe = BandStabilizer()
    probe.align(img)
    probe.align(aligned)
    assert abs(probe.last[0]) < 0.75 and abs(probe.last[1]) < 0.75


def test_band_stabilizer_coordinate_mapping():
    """A raw-frame point maps into the aligned band as band_coords - last, and an
    aligned detection maps back as + last — the convention every call site uses."""
    from video_grouper.inference.iso_warp import BandStabilizer

    img = _textured_band()
    px, py = 400, 100
    img[py - 2 : py + 3, px - 2 : px + 3] = 255  # bright dot = "the ball"
    dx, dy = 12.0, 5.0
    shifted = _shift_img(img, dx, dy)  # ball now at (px+dx, py+dy) on the raw frame
    stab = BandStabilizer()
    stab.align(img)
    aligned = stab.align(shifted)
    edx, edy = stab.last
    # raw ball position corrected by the measured shift lands on the anchor position
    rx, ry = (px + dx) - edx, (py + dy) - edy
    assert abs(rx - px) < 1.0 and abs(ry - py) < 1.0
    # and the dot content itself is back at the anchor position
    win = aligned[py - 4 : py + 5, px - 4 : px + 5]
    assert win.max() >= 250


def test_band_stabilizer_flat_frame_keeps_previous_shift():
    """No usable correlation (flat frame) -> keep the last shift, don't snap to 0."""
    from video_grouper.inference.iso_warp import BandStabilizer

    img = _textured_band()
    stab = BandStabilizer()
    stab.align(img)
    stab.align(_shift_img(img, 8.0, 3.0))
    prev = stab.last
    assert abs(prev[0] - 8.0) < 1.0
    stab.align(np.full_like(img, 90))  # flat: every patch below min_response
    assert stab.last == prev


def test_dewarp_mask_gray_stabilized_pulls_content_back():
    """dewarp_mask_gray with a stabilizer masks AFTER alignment, so content that
    wind pushed toward the mask edge is pulled back before zeroing."""
    import cv2

    from video_grouper.inference.iso_warp import BandStabilizer, dewarp_mask_gray

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
    warp = native_iso_warp(far, 1920, 1080, target_width=None)
    mask = band_mask(warp, far)
    rng = np.random.default_rng(7)
    frame = (rng.random((1080, 1920, 3)) * 80 + 60).astype(np.uint8)
    for x in range(100, 1920, 160):
        frame[:, x : x + 4] = 220
    stab = BandStabilizer()
    ref = dewarp_mask_gray(frame, warp, mask, stab)
    shifted = _shift_img(frame, 10.0, 0.0)
    out = dewarp_mask_gray(shifted, warp, mask, stab)
    assert abs(stab.last[0] - 10.0) < 1.5
    # aligned output matches the reference in the mask interior
    inner = cv2.erode(mask, np.ones((25, 25), np.uint8)) > 0
    diff = np.abs(out.astype(np.float32) - ref.astype(np.float32))[inner]
    assert float(diff.mean()) < 6.0
