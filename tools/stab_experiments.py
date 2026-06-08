"""Stabilization estimator-variant experiment harness.

Verification-only tool (not part of the production pipeline). Runs multiple
motion-estimation strategies on the same source slice and scores each by:

  * Adjacent-frame |dy| reduction vs raw (the most important quality metric)
  * Peak residual magnitude (how big the stabilizer's correction got)
  * Correlation of cumulative residual vs ground-truth integrated motion
  * Mean confidence (estimator self-reported reliability)

Ground truth = phase correlation on a fixed top sky/treeline ROI between
consecutive RAW frames. That's the cleanest measurement of the camera's
actual motion we can get without an IMU.

Variants:

  baseline
    Current production-path ORB+RANSAC similarity + L1 LP smoothing.

  phasecorr_translation
    Replace per-frame motion estimation with phase correlation on a fixed
    background ROI. Translation only — skip rotation + scale. Sub-pixel
    noise floor (~0.1 px) vs ORB+RANSAC (~5 px on cylindrically-warped
    source).

  phasecorr_median
    Same as phasecorr_translation but applies a 5-frame median filter to
    the per-frame deltas before integrating to cumulative. Rejects single-
    frame outlier estimates without lagging the trajectory.

  baseline_gated
    Current ORB+RANSAC, but holds the previous M when per-frame confidence
    drops below a higher threshold (0.6 vs default 0.3) — reduces single-
    frame outlier jumps without changing the estimator.
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
from pathlib import Path

import av
import cv2
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from video_grouper.inference.field_geometry import load_field  # noqa: E402
from video_grouper.inference.stabilization import (  # noqa: E402
    MotionEstimationConfig,
    SimilarityTransform,
    _ReferenceState,
    compose_stabilizing_transforms,
    compute_safe_inset,
    extract_features,
    l1_smooth_path,
    measure_frame_motion,
    soccer_stability_mask,
)


GROUND_TRUTH_ROI = (80, 320, 1500, 5500)  # (y0, y1, x0, x1) — sky/treeline


# ---------------------------------------------------------------------------
# Frame iteration helper
# ---------------------------------------------------------------------------


def iter_frames(path: Path, max_frames: int | None = None):
    """Yield (frame_idx, rgb) for every video frame in *path*."""
    with av.open(str(path)) as c:
        n = 0
        for pkt in c.demux(c.streams.video[0]):
            if pkt.dts is None:
                continue
            for frame in pkt.decode():
                yield n, frame.to_ndarray(format="rgb24"), c.streams.video[0]
                n += 1
                if max_frames is not None and n >= max_frames:
                    return


def get_src_size(path: Path) -> tuple[int, int]:
    with av.open(str(path)) as c:
        v = c.streams.video[0]
        return v.height, v.width


# ---------------------------------------------------------------------------
# Ground truth (phase correlation on the fixed sky ROI)
# ---------------------------------------------------------------------------


def compute_ground_truth(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Per-frame (dx, dy) between consecutive raw frames using phase
    correlation on a fixed sky/treeline ROI. Returned cumulative arrays
    are length-N (N = frame count) starting at 0.
    """
    y0, y1, x0, x1 = GROUND_TRUTH_ROI
    cum_dx = [0.0]
    cum_dy = [0.0]
    prev = None
    for _, rgb, _ in iter_frames(path):
        roi = cv2.cvtColor(rgb[y0:y1, x0:x1], cv2.COLOR_RGB2GRAY).astype(np.float32)
        if prev is not None:
            (dx, dy), _ = cv2.phaseCorrelate(prev, roi)
            cum_dx.append(cum_dx[-1] + dx)
            cum_dy.append(cum_dy[-1] + dy)
        prev = roi
    return np.array(cum_dx), np.array(cum_dy)


# ---------------------------------------------------------------------------
# Variant: baseline (current ORB+RANSAC + L1)
# ---------------------------------------------------------------------------


def variant_baseline(
    path: Path,
    polygon: np.ndarray | None,
    *,
    budget_px: float = 80.0,
    budget_rot_deg: float = 1.0,
    confidence_gate: float = 0.30,  # min_inlier_ratio for the per-frame measurement
) -> dict:
    """Reproduce the production analysis path: ORB+RANSAC per-frame +
    cumulative integration + L1 LP smoothing + similarity composition.

    Returns a dict containing per-frame (cum_tx, cum_ty, ..., confidences,
    smoothed paths, inset, output_size).
    """
    src_h, src_w = get_src_size(path)
    mask = soccer_stability_mask(src_w, src_h, polygon)
    mys, mxs = np.where(mask > 0)
    roi_y0, roi_y1 = int(mys.min()), int(mys.max()) + 1
    roi_x0, roi_x1 = int(mxs.min()), int(mxs.max()) + 1
    cropped_mask = mask[roi_y0:roi_y1, roi_x0:roi_x1]
    kp_offset = (float(roi_x0), float(roi_y0))

    cfg = MotionEstimationConfig(min_inlier_ratio=confidence_gate)
    reference = _ReferenceState()
    cum_tx, cum_ty, cum_theta, cum_log_scale, confs = [], [], [], [], []

    for i, rgb, _ in iter_frames(path):
        cropped = rgb[roi_y0:roi_y1, roi_x0:roi_x1]
        motion, reanchor = measure_frame_motion(
            cropped, cropped_mask, reference, cfg, keypoint_offset=kp_offset
        )
        cum_tx.append(motion.cum_tx)
        cum_ty.append(motion.cum_ty)
        cum_theta.append(motion.cum_theta)
        cum_log_scale.append(motion.cum_log_scale)
        confs.append(motion.confidence)
        if reference.descriptors is None or reanchor:
            kp, desc = extract_features(
                cropped,
                cropped_mask,
                n_features=cfg.n_features,
                keypoint_offset=kp_offset,
            )
            if desc is not None and len(desc) >= cfg.min_inliers:
                reference.keypoints = kp
                reference.descriptors = desc
                reference.cumulative = SimilarityTransform(
                    tx=motion.cum_tx,
                    ty=motion.cum_ty,
                    theta=motion.cum_theta,
                    log_scale=motion.cum_log_scale,
                )
                reference.frame_idx = i

    return _smooth_and_compose(
        np.array(cum_tx),
        np.array(cum_ty),
        np.array(cum_theta),
        np.array(cum_log_scale),
        confs,
        src_h,
        src_w,
        budget_px,
        budget_rot_deg,
    )


# ---------------------------------------------------------------------------
# Variant: phase correlation translation
# ---------------------------------------------------------------------------


def variant_phasecorr_translation(
    path: Path,
    polygon: np.ndarray | None = None,
    *,
    budget_px: float = 80.0,
    median_window: int | None = None,
) -> dict:
    """Pure translation via phase correlation on a fixed sky/treeline ROI.

    Theta + scale left at zero — for a tripod camera those are sub-degree
    and sub-percent, which the broadcast crop tolerates without correction.
    Optional median filter on the per-frame deltas if ``median_window`` is set.
    """
    src_h, src_w = get_src_size(path)
    y0, y1, x0, x1 = GROUND_TRUTH_ROI
    # Use a slightly wider ROI than the ground truth so they don't perfectly
    # correlate (still informative as a noise-floor reference even with
    # almost-identical ROI — phase correlation does generalize across
    # nearby ROIs).
    y0, y1, x0, x1 = (
        max(0, y0 - 20),
        min(src_h, y1 + 20),
        max(0, x0 - 100),
        min(src_w, x1 + 100),
    )

    dxs, dys = [], []
    prev = None
    for _, rgb, _ in iter_frames(path):
        roi = cv2.cvtColor(rgb[y0:y1, x0:x1], cv2.COLOR_RGB2GRAY).astype(np.float32)
        if prev is not None:
            (dx, dy), _ = cv2.phaseCorrelate(prev, roi)
            dxs.append(dx)
            dys.append(dy)
        prev = roi

    dxs_arr = np.array(dxs)
    dys_arr = np.array(dys)
    if median_window:
        from scipy.signal import medfilt

        dxs_arr = medfilt(dxs_arr, kernel_size=median_window)
        dys_arr = medfilt(dys_arr, kernel_size=median_window)

    # Build cumulative (length = N frames, with cum[0] = 0).
    #
    # IMPORTANT sign convention. phaseCorrelate(prev, cur) returns the motion
    # FROM prev TO cur, i.e. how much content moved between frames. Integrated,
    # cum_ty = current_y - initial_y = camera_position trajectory in source
    # coords. But the rest of the stabilization stack (compose_stabilizing_
    # transforms + FrameStabilizer) was tuned for the ORB+RANSAC convention,
    # where ``cv2.estimateAffinePartial2D(src=cur, dst=ref)`` returns the
    # current→reference transform — the INVERSE of the camera motion. So we
    # NEGATE the phase-correlation cumulative to match the ORB convention.
    cum_tx = np.concatenate([[0.0], -np.cumsum(dxs_arr)])
    cum_ty = np.concatenate([[0.0], -np.cumsum(dys_arr)])
    n = len(cum_tx)
    cum_theta = np.zeros(n)
    cum_log_scale = np.zeros(n)
    confs = [1.0] * n  # high confidence on phase correlation responses (mostly)

    return _smooth_and_compose(
        cum_tx,
        cum_ty,
        cum_theta,
        cum_log_scale,
        confs,
        src_h,
        src_w,
        budget_px,
        1.0,
    )


# ---------------------------------------------------------------------------
# Variant: ORB+RANSAC with HIGHER confidence gate
# ---------------------------------------------------------------------------


def variant_baseline_gated(path, polygon, **kwargs):
    return variant_baseline(path, polygon, confidence_gate=0.55, **kwargs)


# ---------------------------------------------------------------------------
# Variant: ORB+RANSAC with median filter on cumulative
# ---------------------------------------------------------------------------


def variant_baseline_median(
    path,
    polygon,
    *,
    budget_px=80.0,
    budget_rot_deg=1.0,
):
    """Same baseline, but median-filter the cumulative axis values before
    L1 LP smoothing. Rejects single-frame outlier estimates without
    introducing lag.
    """
    from scipy.signal import medfilt

    src_h, src_w = get_src_size(path)
    mask = soccer_stability_mask(src_w, src_h, polygon)
    mys, mxs = np.where(mask > 0)
    roi_y0, roi_y1 = int(mys.min()), int(mys.max()) + 1
    roi_x0, roi_x1 = int(mxs.min()), int(mxs.max()) + 1
    cropped_mask = mask[roi_y0:roi_y1, roi_x0:roi_x1]
    kp_offset = (float(roi_x0), float(roi_y0))

    cfg = MotionEstimationConfig()
    reference = _ReferenceState()
    cum_tx, cum_ty, cum_theta, cum_log_scale, confs = [], [], [], [], []
    for i, rgb, _ in iter_frames(path):
        cropped = rgb[roi_y0:roi_y1, roi_x0:roi_x1]
        motion, reanchor = measure_frame_motion(
            cropped, cropped_mask, reference, cfg, keypoint_offset=kp_offset
        )
        cum_tx.append(motion.cum_tx)
        cum_ty.append(motion.cum_ty)
        cum_theta.append(motion.cum_theta)
        cum_log_scale.append(motion.cum_log_scale)
        confs.append(motion.confidence)
        if reference.descriptors is None or reanchor:
            kp, desc = extract_features(
                cropped,
                cropped_mask,
                n_features=cfg.n_features,
                keypoint_offset=kp_offset,
            )
            if desc is not None and len(desc) >= cfg.min_inliers:
                reference.keypoints = kp
                reference.descriptors = desc
                reference.cumulative = SimilarityTransform(
                    tx=motion.cum_tx,
                    ty=motion.cum_ty,
                    theta=motion.cum_theta,
                    log_scale=motion.cum_log_scale,
                )
                reference.frame_idx = i

    # Median-filter the cumulative trajectory (5-frame window)
    K = 5
    return _smooth_and_compose(
        medfilt(np.array(cum_tx), K),
        medfilt(np.array(cum_ty), K),
        medfilt(np.array(cum_theta), K),
        medfilt(np.array(cum_log_scale), K),
        confs,
        src_h,
        src_w,
        budget_px,
        budget_rot_deg,
    )


# ---------------------------------------------------------------------------
# Shared: L1 smoothing + matrix composition + write motion-style dict
# ---------------------------------------------------------------------------


def _smooth_and_compose(
    cum_tx,
    cum_ty,
    cum_theta,
    cum_log_scale,
    confidences,
    src_h,
    src_w,
    budget_px,
    budget_rot_deg,
) -> dict:
    smooth_tx = l1_smooth_path(cum_tx, budget=budget_px)
    smooth_ty = l1_smooth_path(cum_ty, budget=budget_px)
    smooth_theta = l1_smooth_path(cum_theta, budget=math.radians(budget_rot_deg))
    smooth_log_scale = l1_smooth_path(cum_log_scale, budget=0.005)
    inset_y, inset_x = compute_safe_inset(
        budget_px, budget_px, budget_rot_deg, 0.005, src_w, src_h
    )
    mats = compose_stabilizing_transforms(
        cum_tx,
        cum_ty,
        cum_theta,
        cum_log_scale,
        smooth_tx,
        smooth_ty,
        smooth_theta,
        smooth_log_scale,
        inset_x=inset_x,
        inset_y=inset_y,
    )
    return {
        "src_size": (src_h, src_w),
        "output_size": (src_h - 2 * inset_y, src_w - 2 * inset_x),
        "safe_inset": (inset_y, inset_x),
        "transforms": mats,
        "confidences": list(confidences),
        "cum_tx": np.asarray(cum_tx),
        "cum_ty": np.asarray(cum_ty),
        "smooth_tx": smooth_tx,
        "smooth_ty": smooth_ty,
    }


# ---------------------------------------------------------------------------
# Scoring (run a stabilizer's M list against raw frames, measure quality)
# ---------------------------------------------------------------------------


def score_variant(path: Path, variant_result: dict, truth_cum_dx, truth_cum_dy) -> dict:
    """Run each frame through the variant's M, measure adjacent-frame motion
    in stabilized output vs raw, plus correlation of M residual vs truth.
    """
    from video_grouper.inference.stabilization import FrameStabilizer

    stab = FrameStabilizer(
        src_size=variant_result["src_size"],
        output_size=variant_result["output_size"],
        safe_inset=variant_result["safe_inset"],
        transforms=variant_result["transforms"],
        confidences=variant_result["confidences"],
    )

    y0, y1, x0, x1 = GROUND_TRUTH_ROI

    raw_dys = []
    stab_dys = []
    prev_raw = prev_stab = None
    for n, rgb, _ in iter_frames(path):
        raw_roi = cv2.cvtColor(rgb[y0:y1, x0:x1], cv2.COLOR_RGB2GRAY).astype(np.float32)
        stab_rgb = stab.apply(rgb, n)
        # Stabilized output is smaller; sample the same ABSOLUTE ROI (shifted by inset)
        iy, ix = variant_result["safe_inset"]
        sy0 = max(0, y0 - iy)
        sy1 = sy0 + (y1 - y0)
        sx0 = max(0, x0 - ix)
        sx1 = sx0 + (x1 - x0)
        if sy1 > stab_rgb.shape[0] or sx1 > stab_rgb.shape[1]:
            sy1 = min(sy1, stab_rgb.shape[0])
            sy0 = sy1 - (y1 - y0)
            sx1 = min(sx1, stab_rgb.shape[1])
            sx0 = sx1 - (x1 - x0)
        stab_roi = cv2.cvtColor(stab_rgb[sy0:sy1, sx0:sx1], cv2.COLOR_RGB2GRAY).astype(
            np.float32
        )
        if prev_raw is not None:
            (_, dy), _ = cv2.phaseCorrelate(prev_raw, raw_roi)
            raw_dys.append(dy)
            (_, dy), _ = cv2.phaseCorrelate(prev_stab, stab_roi)
            stab_dys.append(dy)
        prev_raw = raw_roi
        prev_stab = stab_roi

    raw_dys = np.array(raw_dys)
    stab_dys = np.array(stab_dys)
    # Correlation of algorithm M ty residual with truth integrated dy
    iy_inset, _ = variant_result["safe_inset"]
    n_M = len(variant_result["transforms"])
    M_tys = np.array([M[1][2] - iy_inset for M in variant_result["transforms"]])
    n_common = min(len(truth_cum_dy), n_M)
    corr = float(np.corrcoef(truth_cum_dy[:n_common], M_tys[:n_common])[0, 1])

    return {
        "raw_mean_|dy|": float(np.abs(raw_dys).mean()),
        "raw_peak_|dy|": float(np.abs(raw_dys).max()),
        "stab_mean_|dy|": float(np.abs(stab_dys).mean()),
        "stab_peak_|dy|": float(np.abs(stab_dys).max()),
        "reduction_pct": float(
            (1 - np.abs(stab_dys).mean() / max(1e-9, np.abs(raw_dys).mean())) * 100
        ),
        "M_ty_peak": float(np.abs(M_tys).max()),
        "M_ty_std": float(np.std(M_tys)),
        "M_truth_correlation": corr,
        "mean_confidence": float(np.mean(variant_result["confidences"])),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument(
        "--polygon", type=Path, default=Path(".verify/field_polygon.json")
    )
    parser.add_argument(
        "--variants",
        nargs="+",
        default=[
            "baseline",
            "phasecorr_translation",
            "phasecorr_median",
            "baseline_gated",
            "baseline_median",
        ],
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    polygon, _hom = (
        load_field(str(args.polygon)) if args.polygon.exists() else (None, None)
    )
    logging.info("loaded polygon: %s", "yes" if polygon is not None else "no")
    logging.info("computing ground truth via phase correlation...")
    truth_cum_dx, truth_cum_dy = compute_ground_truth(args.input)
    logging.info(
        "ground truth peak |cum_dy| = %.2f px, std = %.2f px",
        np.abs(truth_cum_dy).max(),
        np.std(truth_cum_dy),
    )

    variants = {
        "baseline": variant_baseline,
        "phasecorr_translation": variant_phasecorr_translation,
        "phasecorr_median": lambda p, poly: variant_phasecorr_translation(
            p, poly, median_window=5
        ),
        "baseline_gated": variant_baseline_gated,
        "baseline_median": variant_baseline_median,
        # Smaller-budget L1 — forces the smoother to track cum more closely,
        # so the residual (and thus the stabilizing M shift) is smaller.
        # Less aggressive stabilization, which baseline_median showed is better
        # on this footage.
        "phasecorr_tight": lambda p, poly: variant_phasecorr_translation(
            p, poly, budget_px=15.0
        ),
        "phasecorr_tight_median": lambda p, poly: variant_phasecorr_translation(
            p, poly, budget_px=15.0, median_window=5
        ),
    }

    rows = []
    for name in args.variants:
        if name not in variants:
            logging.warning("unknown variant: %s", name)
            continue
        logging.info("=== running variant: %s ===", name)
        result = variants[name](args.input, polygon)
        score = score_variant(args.input, result, truth_cum_dx, truth_cum_dy)
        rows.append((name, score))
        logging.info("  %s: %s", name, score)

    # Print final comparison table
    print("\n" + "=" * 120)
    print(
        f"{'variant':<24} {'raw_mean':<10} {'stab_mean':<10} {'reduction':<11} "
        f"{'M_peak':<8} {'M_std':<8} {'corr':<8} {'conf':<6}"
    )
    print("=" * 120)
    for name, s in rows:
        print(
            f"{name:<24} "
            f"{s['raw_mean_|dy|']:<10.2f} "
            f"{s['stab_mean_|dy|']:<10.2f} "
            f"{s['reduction_pct']:<+11.1f} "
            f"{s['M_ty_peak']:<8.1f} "
            f"{s['M_ty_std']:<8.1f} "
            f"{s['M_truth_correlation']:<+8.2f} "
            f"{s['mean_confidence']:<6.2f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
