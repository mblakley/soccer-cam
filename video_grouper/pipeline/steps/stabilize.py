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
from pydantic import BaseModel, model_validator

from video_grouper.pipeline import register_step
from video_grouper.pipeline.base import PipelineStep, StepContext
from video_grouper.pipeline.manifest import PipelineManifest

logger = logging.getLogger(__name__)


# Strength tiers bundle the four user-visible knobs that trade compute /
# crop-size for correction power: per-axis budgets (translation +
# rotation + scale) and whether to do the 3× per-frame polygon-zone
# blend. Authoritative single source of truth — both the StabilizeStep
# config validator and the reprocess machinery read this map.
#
#   light    — calm-day footage: tight budgets, single-warp. Fastest, smallest crop.
#   standard — current production default: moderate budgets, single-warp.
#   heavy    — breezy day with horizon-edge parallax: full budgets + zone blend.
#   extreme  — gusty day, mast really swaying: wide budgets + zone blend.
#              Output crop loses ~5% more on each side than `heavy`.
STABILIZATION_STRENGTH_PRESETS: dict[str, dict] = {
    "light": {
        "stabilize_max_tx_px": 30.0,
        "stabilize_max_ty_px": 30.0,
        "stabilize_max_rotation_deg": 0.5,
        "stabilize_max_log_scale": 0.003,
        "stabilize_polygon_blend": False,
    },
    "standard": {
        "stabilize_max_tx_px": 60.0,
        "stabilize_max_ty_px": 60.0,
        "stabilize_max_rotation_deg": 1.0,
        "stabilize_max_log_scale": 0.005,
        "stabilize_polygon_blend": False,
    },
    "heavy": {
        "stabilize_max_tx_px": 60.0,
        "stabilize_max_ty_px": 60.0,
        "stabilize_max_rotation_deg": 1.5,
        "stabilize_max_log_scale": 0.005,
        "stabilize_polygon_blend": True,
    },
    "extreme": {
        "stabilize_max_tx_px": 100.0,
        "stabilize_max_ty_px": 100.0,
        "stabilize_max_rotation_deg": 2.5,
        "stabilize_max_log_scale": 0.008,
        "stabilize_polygon_blend": True,
    },
}


class StabilizeStepConfig(BaseModel):
    """Per-axis safe budgets + estimator + L1 weights for the analysis pass."""

    # High-level strength preset. When set, fills in budgets +
    # polygon-blend mode from STABILIZATION_STRENGTH_PRESETS for any
    # individual knob the caller did NOT explicitly override. Lets
    # presets / UI dropdowns ship a single string while power users keep
    # full per-axis control via overrides. "off" is not a value here —
    # turning stabilization off is a pipeline-preset choice (use the
    # `broadcast` preset, which doesn't include this step).
    stabilization_strength: str | None = None

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

    # Polygon-zone blend: fit + smooth 3 separate similarities per frame
    # (sky / field / near band) and emit a zone-blend ``motion.json``.
    # The field polygon doubles as a coarse depth proxy — pixels inside the
    # polygon are warped by the field path; pixels above the polygon's top
    # edge (and laterally between its top corners) are warped by the sky
    # path; everything else by the near path. Cancels the parallax that a
    # single rigid 2D similarity can't because tripod-mast flex moves the
    # camera through a small dome in 3D. ~3× the apply cost (3 warpAffine +
    # a np.where blend), amortised by FrameFanoutStep applying the
    # stabilizer once per decoded frame and sharing the result with all
    # downstream consumers. Falls back to single-warp when polygon is
    # unavailable.
    stabilize_polygon_blend: bool = True

    @model_validator(mode="after")
    def _apply_strength_preset(self):
        """Fill in budgets + polygon-blend from the named strength preset
        for any field the caller did NOT explicitly set, so a preset name
        and per-axis overrides can coexist (overrides win)."""
        if self.stabilization_strength is None:
            return self
        if self.stabilization_strength not in STABILIZATION_STRENGTH_PRESETS:
            raise ValueError(
                f"unknown stabilization_strength {self.stabilization_strength!r}; "
                f"expected one of {sorted(STABILIZATION_STRENGTH_PRESETS)}"
            )
        preset = STABILIZATION_STRENGTH_PRESETS[self.stabilization_strength]
        for field, preset_value in preset.items():
            if field not in self.model_fields_set:
                setattr(self, field, preset_value)
        return self


# ---------------------------------------------------------------------------
# Sync analysis worker
# ---------------------------------------------------------------------------


def _estimate_motion_phasecorr(input_path, polygon, cfg):
    """Phase-correlation translation estimator (the production default).

    Returns a dict whose keys depend on whether ``polygon`` is supplied AND
    ``cfg.stabilize_polygon_blend`` is true:

    Always present:
      ``"all"`` → ``(cum_tx, cum_ty, cum_theta, cum_log_scale)``
        (the single-warp 9-ROI similarity fit, current production behaviour).
      ``"confidences"``, ``"src_h"``, ``"src_w"``, ``"frame_count"``.

    Zone-blend extra keys (only when polygon AND polygon-blend on AND the
    grid actually produced 9 ROIs — i.e. 3 sky + 3 field + 3 near):
      ``"sky"``, ``"field"``, ``"near"`` → each is the same 4-tuple of
      cumulative paths, fit from that band's 3 ROIs only.

    Empirically (real BU14 game footage, calm + windy 30 s slices):
    ~93 % reduction in adjacent-frame |dy| vs raw, +0.93 to +1.00
    correlation with ground truth on the single-warp output. The zone
    blend additionally cancels horizon-edge parallax that the single
    warp can't (camera-on-mast acts as a small 3D dome under gust).
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
    # The "all" entry is the 9-ROI single-similarity fit (current
    # production); the optional sky/field/near entries are 3-ROI per-band
    # fits used by the zone-blend output.
    delta_axes = ("tx", "ty", "theta", "log_scale")
    cum_all: dict[str, list[float]] = {a: [] for a in delta_axes}
    cum_zones: dict[str, dict[str, list[float]]] = {}
    responses: list[float] = []

    with av.open(input_path) as container:
        stream = container.streams.video[0]
        src_w = int(stream.width)
        src_h = int(stream.height)
        rois = stabilization_grid_rois(src_w, src_h, polygon)
        roi_centers = [((x0 + x1) / 2.0, (y0 + y1) / 2.0) for (y0, y1, x0, x1) in rois]
        # 3-row × 3-col grid (sky / field / near). Per-zone fitting is
        # only possible when we have all 9. Without polygon, the grid
        # function returns 3 sky ROIs only, so single-warp is the only
        # mode available.
        zone_blend = (
            getattr(cfg, "stabilize_polygon_blend", False)
            and polygon is not None
            and len(rois) == 9
        )
        zone_indices = {"sky": (0, 1, 2), "field": (3, 4, 5), "near": (6, 7, 8)}
        if zone_blend:
            cum_zones = {z: {a: [] for a in delta_axes} for z in zone_indices}
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
                    # First frame — seed deltas at zero for every axis,
                    # for every output stream (single + optional zones).
                    for a in delta_axes:
                        cum_all[a].append(0.0)
                    if zone_blend:
                        for z in cum_zones:
                            for a in delta_axes:
                                cum_zones[z][a].append(0.0)
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
                    for prev, cur in zip(prev_grays, cur_grays, strict=False):
                        dx, dy, resp = phase_correlate_translation(prev, cur)
                        motion_vecs.append((dx, dy))
                        per_response.append(resp)
                    response = float(np.mean(per_response))

                    def _fit(idxs, motion_vecs=motion_vecs, roi_centers=roi_centers):
                        return fit_similarity_from_motion_vectors(
                            [roi_centers[k] for k in idxs],
                            [motion_vecs[k] for k in idxs],
                        )

                    if response < cfg.stabilize_phasecorr_response_min:
                        # Low-response frame — freeze the last accepted
                        # delta on every axis for every stream so we
                        # don't inject motion-vector noise.
                        delta_all_inv = last_delta.inverse()
                        cum_all["tx"].append(delta_all_inv.tx)
                        cum_all["ty"].append(delta_all_inv.ty)
                        cum_all["theta"].append(delta_all_inv.theta)
                        cum_all["log_scale"].append(delta_all_inv.log_scale)
                        if zone_blend:
                            for z in cum_zones:
                                cum_zones[z]["tx"].append(delta_all_inv.tx)
                                cum_zones[z]["ty"].append(delta_all_inv.ty)
                                cum_zones[z]["theta"].append(delta_all_inv.theta)
                                cum_zones[z]["log_scale"].append(
                                    delta_all_inv.log_scale
                                )
                    else:
                        delta_all = _fit(range(len(roi_centers)))
                        last_delta = delta_all
                        # Negate to match the ORB-convention cumulative
                        # (current → reference). phaseCorrelate(prev, cur)
                        # returns CONTENT motion (cur − prev), so the delta in
                        # current → reference terms is the INVERSE.
                        delta_all_inv = delta_all.inverse()
                        cum_all["tx"].append(delta_all_inv.tx)
                        cum_all["ty"].append(delta_all_inv.ty)
                        cum_all["theta"].append(delta_all_inv.theta)
                        cum_all["log_scale"].append(delta_all_inv.log_scale)
                        if zone_blend:
                            for z, idxs in zone_indices.items():
                                d_inv = _fit(idxs).inverse()
                                cum_zones[z]["tx"].append(d_inv.tx)
                                cum_zones[z]["ty"].append(d_inv.ty)
                                cum_zones[z]["theta"].append(d_inv.theta)
                                cum_zones[z]["log_scale"].append(d_inv.log_scale)
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
    def _integrate(deltas: dict[str, list[float]]) -> tuple[np.ndarray, ...]:
        return tuple(
            np.cumsum(np.array(deltas[a], dtype=np.float64)) for a in delta_axes
        )

    result: dict = {
        "all": _integrate(cum_all),
        "confidences": responses,
        "src_h": src_h,
        "src_w": src_w,
        "frame_count": frame_idx,
    }
    if zone_blend:
        for z, dz in cum_zones.items():
            result[z] = _integrate(dz)
    return result


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
    return {
        "all": (
            np.asarray(cum_tx, dtype=np.float64),
            np.asarray(cum_ty, dtype=np.float64),
            np.asarray(cum_theta, dtype=np.float64),
            np.asarray(cum_log_scale, dtype=np.float64),
        ),
        "confidences": confidences,
        "src_h": src_h,
        "src_w": src_w,
        "frame_count": frame_idx,
    }


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
        result = _estimate_motion_phasecorr(input_path, polygon, cfg)
    elif estimator == "orb":
        result = _estimate_motion_orb(input_path, polygon, cfg)
    else:
        raise ValueError(
            f"stabilize_estimator must be 'phasecorr' or 'orb', got {estimator!r}"
        )

    confidences = result["confidences"]
    src_h = result["src_h"]
    src_w = result["src_w"]
    frame_idx = result["frame_count"]
    R_theta_rad = math.radians(cfg.stabilize_max_rotation_deg)

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

    def _smooth_and_compose(cums: tuple[np.ndarray, ...]):
        """L1-smooth each of (tx, ty, theta, log_scale) under per-axis
        budgets, then compose the per-frame 2×3 stabilizing similarity.

        Returns ``(matrices, peak_residuals_dict)`` where
        ``peak_residuals_dict`` carries x / y / rot-deg peaks for logging.
        """
        cum_tx_a, cum_ty_a, cum_theta_a, cum_log_scale_a = cums
        s_tx = l1_smooth_path(
            cum_tx_a,
            w1=cfg.stabilize_w1,
            w2=cfg.stabilize_w2,
            w3=cfg.stabilize_w3,
            budget=cfg.stabilize_max_tx_px,
            w_stay=cfg.stabilize_w_stay,
        )
        s_ty = l1_smooth_path(
            cum_ty_a,
            w1=cfg.stabilize_w1,
            w2=cfg.stabilize_w2,
            w3=cfg.stabilize_w3,
            budget=cfg.stabilize_max_ty_px,
            w_stay=cfg.stabilize_w_stay,
        )
        s_th = l1_smooth_path(
            cum_theta_a,
            w1=cfg.stabilize_w1,
            w2=cfg.stabilize_w2,
            w3=cfg.stabilize_w3,
            budget=R_theta_rad,
            w_stay=cfg.stabilize_w_stay,
        )
        s_ls = l1_smooth_path(
            cum_log_scale_a,
            w1=cfg.stabilize_w1,
            w2=cfg.stabilize_w2,
            w3=cfg.stabilize_w3,
            budget=cfg.stabilize_max_log_scale,
            w_stay=cfg.stabilize_w_stay,
        )
        mats = compose_stabilizing_transforms(
            cum_tx_a,
            cum_ty_a,
            cum_theta_a,
            cum_log_scale_a,
            s_tx,
            s_ty,
            s_th,
            s_ls,
            inset_x=inset_x,
            inset_y=inset_y,
        )
        peaks = {
            "x": float(np.max(np.abs(cum_tx_a - s_tx))),
            "y": float(np.max(np.abs(cum_ty_a - s_ty))),
            "rot_deg": float(math.degrees(np.max(np.abs(cum_theta_a - s_th)))),
        }
        return mats, peaks

    zone_keys = ("sky", "field", "near")
    zone_mode = all(z in result for z in zone_keys)

    if zone_mode:
        zone_matrices: dict[str, list] = {}
        zone_peaks: dict[str, dict] = {}
        for z in zone_keys:
            zone_matrices[z], zone_peaks[z] = _smooth_and_compose(result[z])
        # Peak summary reports the worst across zones — that's the value
        # the safe-inset budget actually has to absorb.
        peak_res_x = max(zone_peaks[z]["x"] for z in zone_keys)
        peak_res_y = max(zone_peaks[z]["y"] for z in zone_keys)
        peak_res_rot_deg = max(zone_peaks[z]["rot_deg"] for z in zone_keys)
        write_motion_json(
            output_path,
            src_size=(src_h, src_w),
            output_size=(out_h, out_w),
            safe_inset=(inset_y, inset_x),
            transforms=None,
            confidences=confidences,
            zone_transforms=zone_matrices,
            polygon=polygon,
        )
    else:
        matrices, peaks = _smooth_and_compose(result["all"])
        peak_res_x = peaks["x"]
        peak_res_y = peaks["y"]
        peak_res_rot_deg = peaks["rot_deg"]
        write_motion_json(
            output_path,
            src_size=(src_h, src_w),
            output_size=(out_h, out_w),
            safe_inset=(inset_y, inset_x),
            transforms=matrices,
            confidences=confidences,
        )

    return {
        "frame_count": int(frame_idx),
        "src_size": (src_h, src_w),
        "output_size": (out_h, out_w),
        "safe_inset": (inset_y, inset_x),
        "peak_residual_x_px": peak_res_x,
        "peak_residual_y_px": peak_res_y,
        "peak_residual_rotation_deg": peak_res_rot_deg,
        "mean_confidence": float(np.mean(confidences) if confidences else 0.0),
        "mode": "zone-blend" if zone_mode else "single",
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
            "stabilize[%s]: %d frames -> %s (peak residual: x=%.2f px, "
            "y=%.2f px, rot=%.3f°; mean confidence %.2f; output %dx%d, "
            "inset %dx%d)",
            summary["mode"],
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
