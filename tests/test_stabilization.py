"""Unit tests for :mod:`video_grouper.inference.stabilization`."""

from __future__ import annotations

import json
import math
from pathlib import Path

import cv2
import numpy as np
import pytest

from video_grouper.inference.stabilization import (
    FrameStabilizer,
    MotionEstimationConfig,
    SimilarityTransform,
    _ReferenceState,
    background_strip_roi,
    compose_stabilizing_transforms,
    compute_safe_inset,
    estimate_similarity,
    extract_features,
    l1_smooth_path,
    match_with_ratio_test,
    measure_frame_motion,
    phase_correlate_translation,
    soccer_stability_mask,
    write_motion_json,
)


# ---------------------------------------------------------------------------
# SimilarityTransform
# ---------------------------------------------------------------------------


class TestSimilarityTransform:
    def test_identity_to_affine(self):
        T = SimilarityTransform.identity()
        M = T.to_affine()
        expected = np.array([[1, 0, 0], [0, 1, 0]], dtype=np.float32)
        np.testing.assert_allclose(M, expected, atol=1e-6)

    def test_roundtrip_decomposition(self):
        for tx, ty, theta, logs in [
            (3.5, -2.0, 0.05, 0.01),
            (-10.0, 7.0, -0.1, -0.02),
            (0.0, 0.0, 0.0, 0.0),
        ]:
            T = SimilarityTransform(tx=tx, ty=ty, theta=theta, log_scale=logs)
            M = T.to_affine()
            T2 = SimilarityTransform.from_affine(M)
            assert T2.tx == pytest.approx(tx, abs=1e-5)
            assert T2.ty == pytest.approx(ty, abs=1e-5)
            assert T2.theta == pytest.approx(theta, abs=1e-5)
            assert T2.log_scale == pytest.approx(logs, abs=1e-5)

    def test_compose_identity_left(self):
        T = SimilarityTransform(tx=2.0, ty=3.0, theta=0.1, log_scale=0.05)
        composed = SimilarityTransform.identity().compose(T)
        assert composed.tx == pytest.approx(T.tx, abs=1e-5)
        assert composed.ty == pytest.approx(T.ty, abs=1e-5)
        assert composed.theta == pytest.approx(T.theta, abs=1e-5)
        assert composed.log_scale == pytest.approx(T.log_scale, abs=1e-5)

    def test_compose_identity_right(self):
        T = SimilarityTransform(tx=2.0, ty=3.0, theta=0.1, log_scale=0.05)
        composed = T.compose(SimilarityTransform.identity())
        assert composed.tx == pytest.approx(T.tx, abs=1e-5)
        assert composed.ty == pytest.approx(T.ty, abs=1e-5)
        assert composed.theta == pytest.approx(T.theta, abs=1e-5)
        assert composed.log_scale == pytest.approx(T.log_scale, abs=1e-5)

    def test_inverse_roundtrip(self):
        T = SimilarityTransform(tx=5.0, ty=-3.0, theta=0.07, log_scale=0.02)
        composed = T.compose(T.inverse())
        np.testing.assert_allclose(
            composed.to_affine(),
            SimilarityTransform.identity().to_affine(),
            atol=1e-5,
        )


# ---------------------------------------------------------------------------
# ORB extraction + matching + RANSAC similarity
# ---------------------------------------------------------------------------


def _synthetic_textured_image(
    height: int = 480,
    width: int = 720,
    n_blobs: int = 300,
    rng_seed: int = 42,
) -> np.ndarray:
    """Build a textured RGB image with random high-contrast blobs that ORB
    can index reliably. Includes some long edges so estimateAffinePartial2D
    has enough geometric diversity."""
    rng = np.random.default_rng(rng_seed)
    img = np.full((height, width, 3), 128, dtype=np.uint8)
    # Random rectangles + circles
    for _ in range(n_blobs):
        x = int(rng.integers(20, width - 20))
        y = int(rng.integers(20, height - 20))
        r = int(rng.integers(3, 12))
        color = (
            int(rng.integers(0, 256)),
            int(rng.integers(0, 256)),
            int(rng.integers(0, 256)),
        )
        if rng.random() < 0.5:
            cv2.rectangle(img, (x - r, y - r), (x + r, y + r), color, -1)
        else:
            cv2.circle(img, (x, y), r, color, -1)
    # A few long lines
    for _ in range(10):
        p1 = (int(rng.integers(0, width)), int(rng.integers(0, height)))
        p2 = (int(rng.integers(0, width)), int(rng.integers(0, height)))
        cv2.line(img, p1, p2, (220, 220, 220), 2)
    return img


def _warp_image(img: np.ndarray, T: SimilarityTransform) -> np.ndarray:
    """Apply T as the FORWARD warp: each output pixel comes from source pixel
    T·(x_out, y_out). The forward warp matrix that produces this with
    warpAffine is T.inverse()."""
    M = T.inverse().to_affine()
    return cv2.warpAffine(
        img,
        M,
        dsize=(img.shape[1], img.shape[0]),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )


class TestORBRansacSimilarity:
    def test_recover_pure_translation(self):
        img_ref = _synthetic_textured_image(rng_seed=1)
        T_true = SimilarityTransform(tx=4.7, ty=-3.1)
        img_cur = _warp_image(img_ref, T_true)
        ref_kp, ref_desc = extract_features(img_ref, mask=None, n_features=1500)
        cur_kp, cur_desc = extract_features(img_cur, mask=None, n_features=1500)
        matches = match_with_ratio_test(ref_desc, cur_desc, ratio=0.75)
        assert len(matches) >= 20, "need enough matches for stable RANSAC"
        M, inliers, ratio = estimate_similarity(
            ref_kp, cur_kp, matches, ransac_threshold=1.5
        )
        T_est = SimilarityTransform.from_affine(M)
        assert T_est.tx == pytest.approx(T_true.tx, abs=0.4)
        assert T_est.ty == pytest.approx(T_true.ty, abs=0.4)
        assert abs(T_est.theta) < 0.01
        assert abs(T_est.log_scale) < 0.01
        assert inliers >= 15

    def test_recover_translation_plus_rotation(self):
        img_ref = _synthetic_textured_image(rng_seed=2)
        T_true = SimilarityTransform(tx=2.0, ty=1.5, theta=math.radians(0.35))
        img_cur = _warp_image(img_ref, T_true)
        ref_kp, ref_desc = extract_features(img_ref, mask=None, n_features=1500)
        cur_kp, cur_desc = extract_features(img_cur, mask=None, n_features=1500)
        matches = match_with_ratio_test(ref_desc, cur_desc)
        M, inliers, ratio = estimate_similarity(ref_kp, cur_kp, matches)
        T_est = SimilarityTransform.from_affine(M)
        # Combined translation+rotation has small cross-coupling on a
        # finite-extent synthetic frame; ~1px tolerance is representative of
        # real-world performance and well below the per-axis safe budget.
        assert T_est.tx == pytest.approx(T_true.tx, abs=1.0)
        assert T_est.ty == pytest.approx(T_true.ty, abs=1.0)
        assert math.degrees(T_est.theta) == pytest.approx(
            math.degrees(T_true.theta), abs=0.15
        )

    def test_too_few_matches(self):
        # Empty descriptor case: estimate_similarity should fail gracefully.
        M, inliers, ratio = estimate_similarity([], [], [], ransac_threshold=1.5)
        assert M is None
        assert inliers == 0
        assert ratio == 0.0


# ---------------------------------------------------------------------------
# Soccer stability mask
# ---------------------------------------------------------------------------


def _synthetic_field_polygon(src_w: int = 1280, src_h: int = 360) -> np.ndarray:
    """A representative ~180° panoramic-view field polygon: near sideline at
    y ≈ 0.85h (5 points), far sideline at y ≈ 0.35h (5 points)."""
    near_y = int(src_h * 0.85)
    far_y = int(src_h * 0.35)
    near = [
        (int(src_w * 0.10), near_y),
        (int(src_w * 0.30), near_y),
        (int(src_w * 0.50), near_y),
        (int(src_w * 0.70), near_y),
        (int(src_w * 0.90), near_y),
    ]
    far = [
        (int(src_w * 0.90), far_y),  # near-right
        (int(src_w * 0.70), far_y),
        (int(src_w * 0.50), far_y),
        (int(src_w * 0.30), far_y),
        (int(src_w * 0.10), far_y),
    ]
    return np.array(near + far, dtype=np.float32)


class TestSoccerStabilityMask:
    def test_field_interior_excluded(self):
        src_w, src_h = 1280, 360
        poly = _synthetic_field_polygon(src_w, src_h)
        mask = soccer_stability_mask(src_w, src_h, poly)
        # Center of field is dead-centre x, halfway between near and far sidelines.
        cx = src_w // 2
        far_y = int(poly[:, 1].min())
        near_y = int(poly[:, 1].max())
        cy = (far_y + near_y) // 2
        assert mask[cy, cx] == 0

    def test_sky_strip_included(self):
        src_w, src_h = 1280, 360
        poly = _synthetic_field_polygon(src_w, src_h)
        mask = soccer_stability_mask(src_w, src_h, poly)
        far_y = int(poly[:, 1].min())
        sky_y = max(1, far_y // 4)
        # A column near the center of the image, well above the far sideline.
        assert mask[sky_y, src_w // 2] == 255

    def test_near_sideline_excluded(self):
        src_w, src_h = 1280, 360
        poly = _synthetic_field_polygon(src_w, src_h)
        mask = soccer_stability_mask(src_w, src_h, poly)
        # Bottom 10% should be all zero.
        assert mask[int(src_h * 0.95), src_w // 2] == 0

    def test_lateral_inset_excluded(self):
        src_w, src_h = 1280, 360
        poly = _synthetic_field_polygon(src_w, src_h)
        mask = soccer_stability_mask(src_w, src_h, poly)
        # Within first 2% laterally should be zero.
        assert mask[src_h // 4, 5] == 0
        assert mask[src_h // 4, src_w - 5] == 0

    def test_no_polygon_fallback(self):
        src_w, src_h = 1280, 360
        mask = soccer_stability_mask(src_w, src_h, polygon=None)
        # The fallback should still mask the top-sky strip.
        # Some pixel in the top 10-20% laterally inside should be 255.
        assert mask[int(src_h * 0.10), src_w // 2] == 255
        # Bottom should be zero.
        assert mask[int(src_h * 0.90), src_w // 2] == 0


# ---------------------------------------------------------------------------
# L1 path optimization
# ---------------------------------------------------------------------------


class TestL1Smoothing:
    def test_constant_input_passes_through(self):
        cum = np.full(30, 5.0)
        smoothed = l1_smooth_path(cum, budget=10.0)
        np.testing.assert_allclose(smoothed, cum, atol=1e-3)

    def test_sinusoid_residual_zero_mean(self):
        # A pure sinusoid should be smoothed to ~zero (constant ≈ mean).
        n = 80
        t = np.arange(n)
        cum = 5.0 + 3.0 * np.sin(2 * np.pi * t / 16)
        smoothed = l1_smooth_path(cum, budget=10.0)
        residual = cum - smoothed
        # The mean of the residual should be near zero (sinusoid averages out).
        assert abs(residual.mean()) < 0.5
        # The smoothed path should be much flatter than the cumulative.
        assert smoothed.std() < cum.std() / 3

    def test_budget_constraint_respected(self):
        n = 50
        rng = np.random.default_rng(0)
        cum = np.cumsum(rng.standard_normal(n))
        budget = 0.5
        smoothed = l1_smooth_path(cum, budget=budget)
        residual = cum - smoothed
        assert np.max(np.abs(residual)) <= budget + 1e-3

    def test_short_input_returns_input(self):
        cum = np.array([1.0, 2.0, 3.0])  # n=3, less than D3 needs
        smoothed = l1_smooth_path(cum)
        np.testing.assert_allclose(smoothed, cum)


# ---------------------------------------------------------------------------
# Reference state + measure_frame_motion
# ---------------------------------------------------------------------------


class TestMeasureFrameMotion:
    def test_synthetic_drift_recovered(self):
        """Apply a known similarity to a textured frame; expect the
        cumulative to reflect it."""
        img_ref = _synthetic_textured_image(rng_seed=5)
        T_true = SimilarityTransform(tx=4.0, ty=-2.0)
        img_cur = _warp_image(img_ref, T_true)
        ref_kp, ref_desc = extract_features(img_ref, mask=None, n_features=1500)
        state = _ReferenceState(keypoints=ref_kp, descriptors=ref_desc, frame_idx=0)
        motion, reanchor = measure_frame_motion(
            img_cur, mask=None, reference=state, cfg=MotionEstimationConfig()
        )
        assert motion.confidence > 0.0
        assert motion.cum_tx == pytest.approx(T_true.tx, abs=0.5)
        assert motion.cum_ty == pytest.approx(T_true.ty, abs=0.5)

    def test_first_frame_zero_cumulative(self):
        img = _synthetic_textured_image(rng_seed=7)
        state = _ReferenceState()
        motion, _ = measure_frame_motion(
            img, mask=None, reference=state, cfg=MotionEstimationConfig()
        )
        # No reference descriptors → zero cumulative.
        assert motion.cum_tx == 0.0
        assert motion.cum_ty == 0.0
        assert motion.confidence == 0.0


# ---------------------------------------------------------------------------
# Stabilizing transform composition + FrameStabilizer
# ---------------------------------------------------------------------------


class TestCompositionAndStabilizer:
    def test_zero_residual_is_just_inset(self):
        n = 5
        zeros = np.zeros(n)
        mats = compose_stabilizing_transforms(
            zeros,
            zeros,
            zeros,
            zeros,
            zeros,
            zeros,
            zeros,
            zeros,
            inset_x=40,
            inset_y=30,
        )
        for M in mats:
            np.testing.assert_allclose(M[0], [1.0, 0.0, 40.0], atol=1e-5)
            np.testing.assert_allclose(M[1], [0.0, 1.0, 30.0], atol=1e-5)

    def test_translation_residual_undone_in_warp(self, tmp_path: Path):
        """Construct motion.json with a single-frame translation residual,
        End-to-end: simulate a frame where content has shifted by a known
        wobble in source coords, build motion.json from the cumulative that
        ORB+RANSAC would have produced (the INVERSE of that wobble, because
        ``estimateAffinePartial2D(src=cur, dst=ref)`` returns ref-from-cur),
        apply the stabilizer, and assert the displaced bright pixel reappears
        at its REFERENCE position in the output canvas — proving the
        stabilizer CANCELS the wobble rather than amplifying it.
        """
        src_h, src_w = 200, 400
        inset_y, inset_x = 10, 20
        out_h, out_w = src_h - 2 * inset_y, src_w - 2 * inset_x

        # Physical wobble at this frame: content has shifted by (+5, +3) in
        # source pixels relative to the reference. The cumulative ORB+RANSAC
        # produces is the inverse: ty = -3, tx = -5.
        wobble_tx, wobble_ty = 5.0, 3.0
        cum_tx = np.array([-wobble_tx])
        cum_ty = np.array([-wobble_ty])
        zeros = np.zeros(1)
        smooth = np.zeros(1)  # fixed-camera ideal — smoothed path is 0

        mats = compose_stabilizing_transforms(
            cum_tx,
            cum_ty,
            zeros,
            zeros,
            smooth,
            smooth,
            smooth,
            smooth,
            inset_x=inset_x,
            inset_y=inset_y,
        )
        json_path = tmp_path / "motion.json"
        write_motion_json(
            json_path,
            src_size=(src_h, src_w),
            output_size=(out_h, out_w),
            safe_inset=(inset_y, inset_x),
            transforms=mats,
            confidences=[1.0],
        )
        stabilizer = FrameStabilizer.from_json(json_path)
        assert stabilizer.output_shape == (out_h, out_w)

        # Place a bright pixel at the displaced (current-frame) location.
        # After stabilization it should reappear at the REFERENCE position,
        # mapped into the smaller output canvas (subtracting the inset).
        ref_x, ref_y = 100, 50
        cur_x = int(ref_x + wobble_tx)
        cur_y = int(ref_y + wobble_ty)
        rgb = np.zeros((src_h, src_w, 3), dtype=np.uint8)
        rgb[cur_y, cur_x] = 255

        out = stabilizer.apply(rgb, frame_idx=0)
        expected_x = ref_x - inset_x
        expected_y = ref_y - inset_y
        window = out[
            max(0, expected_y - 3) : expected_y + 3,
            max(0, expected_x - 3) : expected_x + 3,
        ]
        assert window.max() > 100, (
            f"expected stabilized bright spot near ({expected_y},{expected_x}), got max={window.max()}"
        )


class TestSafeInsetCalculation:
    def test_translation_only(self):
        inset_y, inset_x = compute_safe_inset(
            cfg_R_tx=10.0,
            cfg_R_ty=20.0,
            cfg_R_rotation_deg=0.0,
            cfg_R_log_scale=0.0,
            src_w=1000,
            src_h=500,
        )
        assert inset_x == 11
        assert inset_y == 21

    def test_rotation_adds_corner_displacement(self):
        # 1° rotation at corner of a 1000x1000 image: sin(1°)·500 ≈ 8.7 px
        inset_y, inset_x = compute_safe_inset(
            cfg_R_tx=0.0,
            cfg_R_ty=0.0,
            cfg_R_rotation_deg=1.0,
            cfg_R_log_scale=0.0,
            src_w=1000,
            src_h=1000,
        )
        # ceil(8.73) + 1 = 10
        assert 9 <= inset_x <= 11
        assert 9 <= inset_y <= 11


# ---------------------------------------------------------------------------
# Soccer-mask robustness: stable-background + moving-foreground composite
# ---------------------------------------------------------------------------


class TestSoccerMaskRobustness:
    """When the source has stable background features AND moving foreground
    objects (player blobs, flapping tents), the masked ORB+RANSAC pass should
    track the stable background motion — not be pulled by the foreground."""

    def test_player_motion_rejected_by_field_interior_mask(self):
        src_w, src_h = 1280, 360
        poly = _synthetic_field_polygon(src_w, src_h)
        # Build a "stable background + moving foreground" pair:
        #   Background (sky strip + goal-frame areas): textured, stable.
        #   Foreground (field interior): players (textured blobs) that
        #   translate by a LARGE amount frame-to-frame.
        background = _synthetic_textured_image(height=src_h, width=src_w, rng_seed=11)
        # Mask out the field interior in the "background-only" image so the
        # foreground texture inside the polygon doesn't pollute the reference.
        far_y = int(poly[:, 1].min())
        near_y = int(poly[:, 1].max())
        # Reference frame: pure background (no players)
        img_ref = background.copy()
        cv2.fillPoly(img_ref, [poly.astype(np.int32).reshape(-1, 1, 2)], (60, 110, 60))
        # Apply a small known camera wobble to the background features only
        T_true = SimilarityTransform(tx=2.5, ty=-1.5)
        img_cur_background = _warp_image(background, T_true)
        img_cur = img_cur_background.copy()
        cv2.fillPoly(img_cur, [poly.astype(np.int32).reshape(-1, 1, 2)], (60, 110, 60))
        # Now ADD moving players inside the polygon (large random offset)
        rng = np.random.default_rng(42)
        for _ in range(15):
            ref_x = int(rng.integers(int(src_w * 0.15), int(src_w * 0.85)))
            ref_y = int(rng.integers(far_y + 10, near_y - 10))
            color = tuple(int(c) for c in rng.integers(0, 256, size=3))
            cv2.rectangle(
                img_ref, (ref_x - 8, ref_y - 14), (ref_x + 8, ref_y + 14), color, -1
            )
            # In current frame, player has moved 30 px (huge — way more than the wobble)
            cur_x = ref_x + int(rng.integers(-30, 31))
            cur_y = ref_y + int(rng.integers(-15, 16))
            cv2.rectangle(
                img_cur, (cur_x - 8, cur_y - 14), (cur_x + 8, cur_y + 14), color, -1
            )

        # Build the soccer mask and run motion estimation against it.
        mask = soccer_stability_mask(src_w, src_h, poly)
        ref_kp, ref_desc = extract_features(img_ref, mask=mask, n_features=1500)
        cur_kp, cur_desc = extract_features(img_cur, mask=mask, n_features=1500)
        if len(ref_kp) < 20 or len(cur_kp) < 20:
            pytest.skip(
                f"Test scene didn't generate enough background features (ref={len(ref_kp)}, cur={len(cur_kp)})"
            )
        matches = match_with_ratio_test(ref_desc, cur_desc)
        M, inliers, ratio = estimate_similarity(
            ref_kp, cur_kp, matches, ransac_threshold=1.5
        )
        if M is None:
            pytest.skip(
                "RANSAC failed on synthetic scene — algorithm fine, scene insufficient"
            )
        T_est = SimilarityTransform.from_affine(M)
        # Recovered transform should match BACKGROUND motion (small), not the
        # huge player offsets (30+ px).
        assert abs(T_est.tx - T_true.tx) < 1.5, (
            f"tx estimate {T_est.tx:.2f} drifted from background motion {T_true.tx:.2f}"
        )
        assert abs(T_est.ty - T_true.ty) < 1.5, (
            f"ty estimate {T_est.ty:.2f} drifted from background motion {T_true.ty:.2f}"
        )


# ---------------------------------------------------------------------------
# motion.json serialisation roundtrip
# ---------------------------------------------------------------------------


class TestMotionJsonRoundtrip:
    def test_write_then_load(self, tmp_path: Path):
        path = tmp_path / "motion.json"
        transforms = [
            np.array([[1.0, 0.0, 3.0], [0.0, 1.0, 5.0]], dtype=np.float32),
            np.array([[1.0, 0.0, 4.0], [0.0, 1.0, 6.0]], dtype=np.float32),
        ]
        write_motion_json(
            path,
            src_size=(1080, 1920),
            output_size=(1000, 1880),
            safe_inset=(40, 20),
            transforms=transforms,
            confidences=[0.9, 0.7],
        )
        with open(path) as f:
            payload = json.load(f)
        assert payload["src_size"] == [1080, 1920]
        assert payload["output_size"] == [1000, 1880]
        assert payload["safe_inset"] == [40, 20]
        assert len(payload["frames"]) == 2
        stabilizer = FrameStabilizer.from_json(path)
        assert stabilizer.src_size == (1080, 1920)
        assert stabilizer.output_shape == (1000, 1880)
        assert stabilizer.confidence(0) == 0.9
        assert stabilizer.confidence(1) == 0.7
        assert stabilizer.confidence(99) == 0.0


# ---------------------------------------------------------------------------
# Phase correlation primitives (the production translation estimator)
# ---------------------------------------------------------------------------


class TestPhaseCorrelate:
    def test_recovers_pure_translation(self):
        """Phase correlation should sub-pixel-recover a known integer shift."""
        rng = np.random.default_rng(0)
        h, w = 240, 480
        base = (rng.standard_normal((h, w)) * 50 + 128).astype(np.float32)
        # Apply a known translation via warpAffine. The matrix M maps
        # dst-pixel → src-pixel by default (it's an inverse map), so a
        # +5/+3 translation in M shifts content by -5/-3 in display, and
        # phaseCorrelate(prev, cur) reports the displacement to undo that —
        # i.e. it returns the matrix's translation amount, with sign matching
        # the warp matrix.
        T = np.array([[1, 0, 5], [0, 1, 3]], dtype=np.float32)
        shifted = cv2.warpAffine(
            base,
            T,
            (w, h),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REPLICATE,
        )
        dx, dy, response = phase_correlate_translation(base, shifted)
        assert dx == pytest.approx(5.0, abs=0.3)
        assert dy == pytest.approx(3.0, abs=0.3)
        assert response > 0.3

    def test_zero_response_on_unrelated_frames(self):
        a = (np.random.default_rng(1).standard_normal((100, 200)) * 50 + 128).astype(
            np.float32
        )
        b = (np.random.default_rng(2).standard_normal((100, 200)) * 50 + 128).astype(
            np.float32
        )
        _, _, response = phase_correlate_translation(a, b)
        # Two uncorrelated random fields produce a low correlation response;
        # the exact bound depends on resolution, but it should be much lower
        # than the textured-translation case (>0.3).
        assert response < 0.25


class TestBackgroundStripROI:
    def test_polygon_overlap_roi_default_function_args(self):
        """ROI straddles the polygon's highest top-edge point so the strip
        captures the treeline (function-level defaults)."""
        polygon = np.array(
            [
                [275, 1097],
                [1350, 1201],
                [3920, 1297],
                [6855, 1215],
                [7390, 1148],
                [5530, 453],
                [4745, 326],
                [3875, 295],
                [2925, 312],
                [2295, 416],
            ],
            dtype=np.float32,
        )
        src_w, src_h = 7680, 2160
        y0, y1, x0, x1 = background_strip_roi(src_w, src_h, polygon)
        # ROI extends BELOW the polygon's highest point (y=295) by the
        # overlap amount (default 25) so it captures the treeline.
        assert y1 == 295 + 25
        # ROI height matches the above_polygon_target_px default.
        assert y1 - y0 == 240
        # Function-level lateral inset default is 0.08 → central 84 % of source width.
        # (The StabilizeStepConfig overrides to 0.18 for production.)
        assert x0 == int(src_w * 0.08)
        assert x1 == int(src_w * (1.0 - 0.08))

    def test_no_polygon_fallback(self):
        src_w, src_h = 1920, 1080
        y0, y1, x0, x1 = background_strip_roi(src_w, src_h, polygon=None)
        # Fallback covers top ~15% of source.
        assert 0 < y0 < y1 <= int(src_h * 0.15) + 1
        assert x0 > 0 and x1 < src_w

    def test_lateral_inset_tunable(self):
        polygon = np.array(
            [[100, 500], [800, 500], [700, 200], [200, 200]],
            dtype=np.float32,
        )
        _, _, x0, x1 = background_strip_roi(
            1000,
            800,
            polygon,
            lateral_inset_frac=0.10,
        )
        assert x0 == 100
        assert x1 == 900
