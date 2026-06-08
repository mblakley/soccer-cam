"""``stabilize`` pipeline step — analyse a wobbling source, write ``motion.json``.

Per the user-approved camera-stabilization plan, this step is the offline
analysis pass that produces the per-frame stabilizing similarity transform.
Downstream steps (``detect``, ``render``) consume ``motion.json`` via
:class:`~video_grouper.inference.stabilization.FrameStabilizer` and apply the
correction to their decoded frames in-memory — so ball detections and the
broadcast crop are both anchored to a world-stable reference frame instead of
the wobbling camera.

The step is **opt-in**: it must be added to the pipeline explicitly (the
``broadcast_stabilized`` preset does this). Disabling it is a no-op — no
``motion_path`` artifact, no inline application downstream.
"""

from __future__ import annotations

import asyncio
import logging
import math
from pathlib import Path

import numpy as np
from pydantic import BaseModel

from video_grouper.pipeline import register_step
from video_grouper.pipeline.base import PipelineStep, StepContext
from video_grouper.pipeline.manifest import PipelineManifest

logger = logging.getLogger(__name__)


class StabilizeStepConfig(BaseModel):
    """Per-axis safe budgets + estimator + L1 weights for the analysis pass."""

    # Motion estimator. "phasecorr" (default) uses sub-pixel phase correlation
    # on a fixed sky/treeline ROI — empirically gives ~93% reduction in
    # adjacent-frame drift on real game footage. "orb" uses the older ORB +
    # RANSAC similarity estimator (kept as a fallback for cases where phase
    # correlation fails — e.g. a featureless overcast sky with no treeline).
    stabilize_estimator: str = "phasecorr"

    # Per-axis safe budgets (the path-optimization cannot let the residual
    # exceed these, which keeps the output crop borderless). Rotation budget
    # is generous (1.5°) because real tripod-mast wobble on the BU14 footage
    # cumulated to ~1.45° over 30s, and clipping that throws away the actual
    # roll component we need to cancel.
    stabilize_max_tx_px: float = 60.0
    stabilize_max_ty_px: float = 60.0
    stabilize_max_rotation_deg: float = 1.5
    stabilize_max_log_scale: float = 0.005  # ≈ ±0.5% scale change

    # L1 path-smoothing weights (velocity : acceleration : jerk) — standard
    # Grundmann 1 : 10 : 100 strongly prefers zero-jerk paths.
    stabilize_w1: float = 1.0
    stabilize_w2: float = 10.0
    stabilize_w3: float = 100.0
    stabilize_w_stay: float = 1.0e-3

    # Phase correlation tuning. The ROI straddles the field polygon's top
    # edge (so it captures the treeline boundary — the most stable feature).
    # Lateral inset of 18% drops the cylindrical-projection corners where
    # angular-to-pixel scale varies most; empirically a 4000-wide central
    # ROI gives a 4-5x cleaner motion estimate than the full 6500-wide one.
    stabilize_roi_lateral_inset_frac: float = 0.18
    stabilize_roi_polygon_edge_overlap_px: int = 25
    stabilize_roi_above_polygon_target_px: int = 240
    stabilize_roi_top_skip_frac: float = 0.025
    # Below this phase-correlation response, hold previous motion estimate
    # (defends against featureless / overcast frames).
    stabilize_phasecorr_response_min: float = 0.05

    # ORB / RANSAC tuning (only used when stabilize_estimator == "orb").
    stabilize_n_features: int = 1500
    stabilize_edge_threshold: int = 12
    stabilize_fast_threshold: int = 15
    stabilize_ratio_test: float = 0.75
    stabilize_ransac_threshold_px: float = 1.5
    stabilize_ransac_max_iters: int = 2000
    stabilize_min_inliers: int = 20
    stabilize_min_inlier_ratio: float = 0.30
    # Drifting-reference policy (ORB path only).
    stabilize_reanchor_min_frames: int = 60
    stabilize_reanchor_max_frames: int = 600
    stabilize_reanchor_inlier_ratio: float = 0.40
    stabilize_reanchor_translation_px: float = 40.0
    stabilize_reanchor_rotation_deg: float = 0.4

    # Output file name (under ``ctx.group_dir``).
    stabilize_output_name: str = "motion.json"


# ---------------------------------------------------------------------------
# Sync analysis worker
# ---------------------------------------------------------------------------


def _estimate_motion_phasecorr(input_path, polygon, cfg):
    """Phase-correlation translation estimator (the production default).

    Returns ``(cum_tx, cum_ty, cum_theta, cum_log_scale, confidences,
    src_h, src_w, frame_count)``. ``theta`` and ``log_scale`` are zeros
    arrays — tripod-camera rotation is sub-degree and the broadcast crop
    tolerates it without explicit correction.

    Empirically (real BU14 game footage, calm + windy 30 s slices):
    ~93 % reduction in adjacent-frame |dy| vs raw, +0.93 to +1.00
    correlation with ground truth. ORB+RANSAC on the same source gave
    -25 % reduction (worse than no stabilization).
    """
    import av
    import cv2

    from video_grouper.inference.stabilization import (
        SimilarityTransform,
        fit_similarity_from_motion_vectors,
        phase_correlate_translation,
        stabilization_grid_rois,
    )

    # Per-frame DELTA accumulator (one delta per axis). The cumulative is
    # an additive per-axis integration of the per-frame delta-INVERSES (to
    # match the ORB convention compose_stabilizing_transforms expects).
    cum_tx_deltas: list[float] = []
    cum_ty_deltas: list[float] = []
    cum_theta_deltas: list[float] = []
    cum_log_scale_deltas: list[float] = []
    responses: list[float] = []

    with av.open(input_path) as container:
        stream = container.streams.video[0]
        src_w = int(stream.width)
        src_h = int(stream.height)
        rois = stabilization_grid_rois(src_w, src_h, polygon)
        roi_centers = [((x0 + x1) / 2.0, (y0 + y1) / 2.0) for (y0, y1, x0, x1) in rois]
        logger.info(
            "stabilize(phasecorr+similarity): %dx%d source, %dx grid of ROIs "
            "(each %dx%d) — soccer_polygon=%s",
            src_w,
            src_h,
            len(rois),
            rois[0][3] - rois[0][2],
            rois[0][1] - rois[0][0],
            polygon is not None,
        )

        prev_grays: list[np.ndarray | None] = [None] * len(rois)
        frame_idx = 0
        last_delta = SimilarityTransform.identity()
        for packet in container.demux(stream):
            if packet.dts is None:
                continue
            for frame in packet.decode():
                rgb = frame.to_ndarray(format="rgb24")
                cur_grays = [
                    cv2.cvtColor(rgb[y0:y1, x0:x1], cv2.COLOR_RGB2GRAY).astype(
                        np.float32
                    )
                    for (y0, y1, x0, x1) in rois
                ]
                if any(g is None for g in prev_grays):
                    # First frame — seed deltas at zero.
                    cum_tx_deltas.append(0.0)
                    cum_ty_deltas.append(0.0)
                    cum_theta_deltas.append(0.0)
                    cum_log_scale_deltas.append(0.0)
                    responses.append(1.0)
                else:
                    # Per-ROI phase correlation → 9 motion vectors. Fit a 2D
                    # similarity (translation + rotation + scale) to the
                    # vectors via cv2.estimateAffinePartial2D. The fitted
                    # rotation is the ROLL component: left/right ROIs at the
                    # same Y move in opposite vertical directions when the
                    # camera rolls about its mount, and the similarity slope
                    # recovers that directly.
                    motion_vecs: list[tuple[float, float]] = []
                    per_response: list[float] = []
                    for prev, cur in zip(prev_grays, cur_grays):
                        dx, dy, resp = phase_correlate_translation(prev, cur)
                        motion_vecs.append((dx, dy))
                        per_response.append(resp)
                    response = float(np.mean(per_response))
                    if response < cfg.stabilize_phasecorr_response_min:
                        delta = last_delta
                    else:
                        delta = fit_similarity_from_motion_vectors(
                            roi_centers, motion_vecs
                        )
                        last_delta = delta
                    # Negate to match the ORB-convention cumulative
                    # (current → reference). phaseCorrelate(prev, cur)
                    # returns CONTENT motion (cur − prev), so the delta in
                    # current → reference terms is the INVERSE.
                    delta_inv = delta.inverse()
                    cum_tx_deltas.append(delta_inv.tx)
                    cum_ty_deltas.append(delta_inv.ty)
                    cum_theta_deltas.append(delta_inv.theta)
                    cum_log_scale_deltas.append(delta_inv.log_scale)
                    responses.append(response)
                prev_grays = cur_grays
                frame_idx += 1

    if frame_idx == 0:
        raise RuntimeError(f"stabilize: input {input_path!r} had no decodable frames")

    # Deltas are already ORB-convention (current→reference, the inverse of
    # camera motion) because the per-frame loop above stored ``delta_inv``
    # values. Per-axis additive integration is exact for translation and a
    # good small-angle approximation for theta/log_scale (≤ a few degrees
    # over a 30 s clip on a tripod camera).
    cum_tx = np.cumsum(np.array(cum_tx_deltas, dtype=np.float64))
    cum_ty = np.cumsum(np.array(cum_ty_deltas, dtype=np.float64))
    cum_theta = np.cumsum(np.array(cum_theta_deltas, dtype=np.float64))
    cum_log_scale = np.cumsum(np.array(cum_log_scale_deltas, dtype=np.float64))
    return cum_tx, cum_ty, cum_theta, cum_log_scale, responses, src_h, src_w, frame_idx


def _estimate_motion_orb(input_path, polygon, cfg):
    """ORB + RANSAC similarity estimator (legacy / fallback).

    Kept for cases where phase correlation might fail — e.g. a featureless
    overcast sky with no treeline texture. On the BU14 footage it was the
    original production path and underperformed phasecorr by ~120 percentage
    points of adjacent-frame |dy| reduction.
    """
    import av

    from video_grouper.inference.stabilization import (
        MotionEstimationConfig,
        SimilarityTransform,
        _ReferenceState,
        extract_features,
        measure_frame_motion,
        soccer_stability_mask,
    )

    estimation_cfg = MotionEstimationConfig(
        n_features=cfg.stabilize_n_features,
        edge_threshold=cfg.stabilize_edge_threshold,
        fast_threshold=cfg.stabilize_fast_threshold,
        ratio=cfg.stabilize_ratio_test,
        ransac_threshold=cfg.stabilize_ransac_threshold_px,
        ransac_max_iters=cfg.stabilize_ransac_max_iters,
        min_inliers=cfg.stabilize_min_inliers,
        min_inlier_ratio=cfg.stabilize_min_inlier_ratio,
        reanchor_min_frames=cfg.stabilize_reanchor_min_frames,
        reanchor_max_frames=cfg.stabilize_reanchor_max_frames,
        reanchor_inlier_ratio=cfg.stabilize_reanchor_inlier_ratio,
        reanchor_translation_px=cfg.stabilize_reanchor_translation_px,
        reanchor_rotation_deg=cfg.stabilize_reanchor_rotation_deg,
    )

    cum_tx: list[float] = []
    cum_ty: list[float] = []
    cum_theta: list[float] = []
    cum_log_scale: list[float] = []
    confidences: list[float] = []

    with av.open(input_path) as container:
        stream = container.streams.video[0]
        src_w = int(stream.width)
        src_h = int(stream.height)
        mask = soccer_stability_mask(src_w, src_h, polygon)
        mask_ys, mask_xs = np.where(mask > 0)
        if len(mask_ys) == 0:
            raise RuntimeError(
                "stabilize: soccer_stability_mask produced an empty mask — "
                "no stable-reference regions available; check src dims + polygon."
            )
        roi_y0, roi_y1 = int(mask_ys.min()), int(mask_ys.max()) + 1
        roi_x0, roi_x1 = int(mask_xs.min()), int(mask_xs.max()) + 1
        cropped_mask = mask[roi_y0:roi_y1, roi_x0:roi_x1]
        keypoint_offset = (float(roi_x0), float(roi_y0))
        logger.info(
            "stabilize(orb): %dx%d source, mask non-zero %.1f%%, ORB ROI bbox "
            "%dx%d (offset %d,%d) — soccer_polygon=%s",
            src_w,
            src_h,
            100.0 * (mask > 0).mean(),
            roi_x1 - roi_x0,
            roi_y1 - roi_y0,
            roi_x0,
            roi_y0,
            polygon is not None,
        )

        reference = _ReferenceState()
        frame_idx = 0
        for packet in container.demux(stream):
            if packet.dts is None:
                continue
            for frame in packet.decode():
                rgb = frame.to_ndarray(format="rgb24")
                cropped_rgb = rgb[roi_y0:roi_y1, roi_x0:roi_x1]
                motion, reanchor = measure_frame_motion(
                    cropped_rgb,
                    cropped_mask,
                    reference,
                    estimation_cfg,
                    keypoint_offset=keypoint_offset,
                )
                cum_tx.append(motion.cum_tx)
                cum_ty.append(motion.cum_ty)
                cum_theta.append(motion.cum_theta)
                cum_log_scale.append(motion.cum_log_scale)
                confidences.append(motion.confidence)
                if reference.descriptors is None or reanchor:
                    kp, desc = extract_features(
                        cropped_rgb,
                        cropped_mask,
                        n_features=cfg.stabilize_n_features,
                        edge_threshold=cfg.stabilize_edge_threshold,
                        fast_threshold=cfg.stabilize_fast_threshold,
                        keypoint_offset=keypoint_offset,
                    )
                    if desc is not None and len(desc) >= cfg.stabilize_min_inliers:
                        reference.keypoints = kp
                        reference.descriptors = desc
                        reference.cumulative = SimilarityTransform(
                            tx=motion.cum_tx,
                            ty=motion.cum_ty,
                            theta=motion.cum_theta,
                            log_scale=motion.cum_log_scale,
                        )
                        reference.frame_idx = frame_idx
                frame_idx += 1

    if frame_idx == 0:
        raise RuntimeError(f"stabilize: input {input_path!r} had no decodable frames")
    return (
        np.asarray(cum_tx, dtype=np.float64),
        np.asarray(cum_ty, dtype=np.float64),
        np.asarray(cum_theta, dtype=np.float64),
        np.asarray(cum_log_scale, dtype=np.float64),
        confidences,
        src_h,
        src_w,
        frame_idx,
    )


def _analyze_video(
    input_path: str,
    output_path: str,
    field_polygon_path: str | None,
    cfg: StabilizeStepConfig,
) -> dict:
    """Single-pass motion analysis + L1 path optimization.

    Returns a small summary dict (frame_count, peak residuals, mean confidence)
    for logging / smoke checks. The full per-frame matrices land in
    ``output_path`` (``motion.json``).
    """
    from video_grouper.inference.field_geometry import load_field
    from video_grouper.inference.stabilization import (
        compose_stabilizing_transforms,
        compute_safe_inset,
        l1_smooth_path,
        write_motion_json,
    )

    polygon, _homography = load_field(field_polygon_path)

    estimator = cfg.stabilize_estimator
    if estimator == "phasecorr":
        (
            cum_tx_arr,
            cum_ty_arr,
            cum_theta_arr,
            cum_log_scale_arr,
            confidences,
            src_h,
            src_w,
            frame_idx,
        ) = _estimate_motion_phasecorr(input_path, polygon, cfg)
    elif estimator == "orb":
        (
            cum_tx_arr,
            cum_ty_arr,
            cum_theta_arr,
            cum_log_scale_arr,
            confidences,
            src_h,
            src_w,
            frame_idx,
        ) = _estimate_motion_orb(input_path, polygon, cfg)
    else:
        raise ValueError(
            f"stabilize_estimator must be 'phasecorr' or 'orb', got {estimator!r}"
        )

    # ----- Stage B: L1-norm path optimization per axis -----
    R_theta_rad = math.radians(cfg.stabilize_max_rotation_deg)

    smooth_tx = l1_smooth_path(
        cum_tx_arr,
        w1=cfg.stabilize_w1,
        w2=cfg.stabilize_w2,
        w3=cfg.stabilize_w3,
        budget=cfg.stabilize_max_tx_px,
        w_stay=cfg.stabilize_w_stay,
    )
    smooth_ty = l1_smooth_path(
        cum_ty_arr,
        w1=cfg.stabilize_w1,
        w2=cfg.stabilize_w2,
        w3=cfg.stabilize_w3,
        budget=cfg.stabilize_max_ty_px,
        w_stay=cfg.stabilize_w_stay,
    )
    smooth_theta = l1_smooth_path(
        cum_theta_arr,
        w1=cfg.stabilize_w1,
        w2=cfg.stabilize_w2,
        w3=cfg.stabilize_w3,
        budget=R_theta_rad,
        w_stay=cfg.stabilize_w_stay,
    )
    smooth_log_scale = l1_smooth_path(
        cum_log_scale_arr,
        w1=cfg.stabilize_w1,
        w2=cfg.stabilize_w2,
        w3=cfg.stabilize_w3,
        budget=cfg.stabilize_max_log_scale,
        w_stay=cfg.stabilize_w_stay,
    )

    # ----- Compose per-frame stabilizing matrices -----
    inset_y, inset_x = compute_safe_inset(
        cfg.stabilize_max_tx_px,
        cfg.stabilize_max_ty_px,
        cfg.stabilize_max_rotation_deg,
        cfg.stabilize_max_log_scale,
        src_w,
        src_h,
    )
    out_h = src_h - 2 * inset_y
    out_w = src_w - 2 * inset_x

    matrices = compose_stabilizing_transforms(
        cum_tx_arr,
        cum_ty_arr,
        cum_theta_arr,
        cum_log_scale_arr,
        smooth_tx,
        smooth_ty,
        smooth_theta,
        smooth_log_scale,
        inset_x=inset_x,
        inset_y=inset_y,
    )

    write_motion_json(
        output_path,
        src_size=(src_h, src_w),
        output_size=(out_h, out_w),
        safe_inset=(inset_y, inset_x),
        transforms=matrices,
        confidences=confidences,
    )

    # Summary for logging.
    peak_res_x = float(np.max(np.abs(cum_tx_arr - smooth_tx)))
    peak_res_y = float(np.max(np.abs(cum_ty_arr - smooth_ty)))
    peak_res_rot_deg = float(math.degrees(np.max(np.abs(cum_theta_arr - smooth_theta))))
    return {
        "frame_count": int(frame_idx),
        "src_size": (src_h, src_w),
        "output_size": (out_h, out_w),
        "safe_inset": (inset_y, inset_x),
        "peak_residual_x_px": peak_res_x,
        "peak_residual_y_px": peak_res_y,
        "peak_residual_rotation_deg": peak_res_rot_deg,
        "mean_confidence": float(np.mean(confidences) if confidences else 0.0),
    }


# ---------------------------------------------------------------------------
# Step
# ---------------------------------------------------------------------------


class StabilizeStep(PipelineStep):
    """Two-stage motion analysis → per-frame stabilizing similarity JSON.

    Stage A (per-frame): ORB features in the soccer-aware background mask,
    RANSAC similarity against a drifting reference, cumulative integration.
    Stage B (offline): per-axis L1 path optimization yielding a piecewise
    path; residual = cumulative − smoothed = stabilizing motion to undo.
    """

    name = "stabilize"
    config_model = StabilizeStepConfig
    consumes = ("input_path",)
    produces = ("motion_path",)
    runtime = "service"
    requires = ("av", "cv2", "scipy")
    resources = ("ram_heavy",)

    async def run(self, manifest: PipelineManifest, ctx: StepContext) -> bool:
        in_path = Path(manifest.get("input_path"))
        out_path = ctx.group_dir / self.config.stabilize_output_name
        # Field polygon is optional — analysis falls back to the no-polygon
        # sky-strip mask when absent.
        field_polygon_path = manifest.get("field_polygon_path")

        summary = await asyncio.to_thread(
            _analyze_video,
            str(in_path),
            str(out_path),
            field_polygon_path,
            self.config,
        )
        logger.info(
            "stabilize: %d frames -> %s (peak residual: x=%.2f px, y=%.2f px, "
            "rot=%.3f°; mean confidence %.2f; output %dx%d, inset %dx%d)",
            summary["frame_count"],
            out_path,
            summary["peak_residual_x_px"],
            summary["peak_residual_y_px"],
            summary["peak_residual_rotation_deg"],
            summary["mean_confidence"],
            summary["output_size"][1],
            summary["output_size"][0],
            summary["safe_inset"][1],
            summary["safe_inset"][0],
        )
        manifest.put("motion_path", str(out_path))
        return True


register_step(StabilizeStep.name, StabilizeStep, StabilizeStepConfig)
