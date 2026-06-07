"""Camera-stabilization primitives for the broadcast render pipeline.

The :func:`StabilizeStep` pipeline step analyzes a recording's source video and
writes a ``motion.json`` sidecar; ``detect`` and ``render`` then apply the same
per-frame correction to their decoded frames before doing their own work, so
detections and the rendered broadcast view are both anchored to a world-stable
reference frame instead of the wobbling camera.

Algorithm (per-frame, against a maintained reference):

1. ORB features inside a multi-region :func:`soccer_stability_mask` (sky strip,
   far-touchline edge, goal-frame vicinities, corner-flag pole bases — never the
   field interior or near-sideline).
2. BFMatcher + Lowe ratio test (drops ambiguous matches on flapping fabric).
3. RANSAC :class:`cv2.estimateAffinePartial2D` constrained to a similarity
   (translation + rotation + uniform scale; no shear). Tight reprojection
   threshold so any feature on a moving player/spectator/flag gets rejected.

Per-frame transforms are decomposed into :class:`SimilarityTransform`
``(tx, ty, theta, log_scale)``, the cumulative path is built additively, and
each axis is smoothed independently via L1-norm optimization (Grundmann et al.
2011 "Auto-Directed Video Stabilization with Robust L1 Optimal Camera Paths").
The per-frame **stabilizing** similarity is the smoothed-minus-cumulative
residual, composed with a constant translation that recentres the smaller
"safe" crop onto the wobbling source.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import cv2
import numpy as np

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Similarity transform: (tx, ty, theta, log_scale) ↔ 2×3 affine matrix
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SimilarityTransform:
    """2D similarity transform (translation + rotation + uniform scale).

    Stored as the decomposed ``(tx, ty, theta, log_scale)`` because each axis
    composes near-additively under the small-rotation assumption that holds
    for a tripod-mounted camera (≤1°), which is what makes the per-axis L1
    path optimization valid.
    """

    tx: float = 0.0
    ty: float = 0.0
    theta: float = 0.0  # radians
    log_scale: float = 0.0

    @property
    def scale(self) -> float:
        return math.exp(self.log_scale)

    def to_affine(self) -> np.ndarray:
        """Return the equivalent 2×3 affine matrix."""
        s = self.scale
        c, sn = math.cos(self.theta), math.sin(self.theta)
        return np.array(
            [[s * c, -s * sn, self.tx], [s * sn, s * c, self.ty]],
            dtype=np.float32,
        )

    @classmethod
    def from_affine(cls, M: np.ndarray) -> "SimilarityTransform":
        """Decompose a 2×3 affine (assumed to be a similarity) into its axes."""
        a = float(M[0, 0])
        b = float(M[1, 0])
        s_sq = a * a + b * b
        return cls(
            tx=float(M[0, 2]),
            ty=float(M[1, 2]),
            theta=math.atan2(b, a),
            log_scale=0.5 * math.log(max(s_sq, 1e-12)),
        )

    @classmethod
    def identity(cls) -> "SimilarityTransform":
        return cls()

    def compose(self, other: "SimilarityTransform") -> "SimilarityTransform":
        """Return ``self ∘ other`` — apply ``other`` first, then ``self``."""
        return SimilarityTransform.from_affine(
            _compose_affine(self.to_affine(), other.to_affine())
        )

    def inverse(self) -> "SimilarityTransform":
        """Return the inverse similarity."""
        s_inv = math.exp(-self.log_scale)
        c, sn = math.cos(-self.theta), math.sin(-self.theta)
        new_tx = -s_inv * (c * self.tx - sn * self.ty)
        new_ty = -s_inv * (sn * self.tx + c * self.ty)
        return SimilarityTransform(
            tx=new_tx,
            ty=new_ty,
            theta=-self.theta,
            log_scale=-self.log_scale,
        )


def _compose_affine(A: np.ndarray, B: np.ndarray) -> np.ndarray:
    """Compose two 2×3 affines as 3×3 homogenous matrix multiply; return 2×3."""
    A3 = np.vstack([A, [0.0, 0.0, 1.0]])
    B3 = np.vstack([B, [0.0, 0.0, 1.0]])
    return (A3 @ B3)[:2].astype(np.float32)


# ---------------------------------------------------------------------------
# ORB feature extraction + matching + RANSAC similarity estimation
# ---------------------------------------------------------------------------


def extract_features(
    rgb: np.ndarray,
    mask: np.ndarray | None,
    n_features: int = 1500,
    edge_threshold: int = 12,
    fast_threshold: int = 15,
):
    """Detect ORB keypoints + compute BRIEF-like descriptors inside ``mask``.

    Returns ``(keypoints, descriptors)``. ``descriptors`` is ``None`` when no
    keypoints are found (caller must guard).
    """
    orb = cv2.ORB_create(
        nfeatures=n_features,
        edgeThreshold=edge_threshold,
        fastThreshold=fast_threshold,
    )
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY) if rgb.ndim == 3 else rgb
    keypoints, descriptors = orb.detectAndCompute(gray, mask=mask)
    return keypoints, descriptors


def match_with_ratio_test(
    ref_desc: np.ndarray | None,
    cur_desc: np.ndarray | None,
    ratio: float = 0.75,
) -> list:
    """BFMatcher knn-2 + Lowe ratio test.

    The ratio test drops a match when the best and second-best reference
    descriptors are nearly equidistant from the current descriptor — the
    canonical defence against textured-but-moving regions (flapping tent
    fabric is the classic soccer failure mode: many similar tent-fold
    descriptors yield ambiguous matches).
    """
    if ref_desc is None or cur_desc is None or len(ref_desc) < 2 or len(cur_desc) < 2:
        return []
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
    knn_matches = matcher.knnMatch(cur_desc, ref_desc, k=2)
    good = []
    for pair in knn_matches:
        if len(pair) < 2:
            continue
        m, n = pair
        if m.distance < ratio * n.distance:
            good.append(m)
    return good


def estimate_similarity(
    ref_kp,
    cur_kp,
    matches: list,
    ransac_threshold: float = 1.5,
    max_iters: int = 2000,
) -> tuple[np.ndarray | None, int, float]:
    """RANSAC-fit a similarity (translation + rotation + uniform scale) to
    matched keypoint pairs. Tight reprojection threshold filters outliers from
    moving objects whose actual displacement exceeds the camera's wobble.

    Returns ``(M, inlier_count, inlier_ratio)``. ``M`` is the 2×3 affine
    that maps a CURRENT keypoint into the REFERENCE frame.
    """
    if len(matches) < 4:
        return None, 0, 0.0
    src_pts = np.float32([cur_kp[m.queryIdx].pt for m in matches]).reshape(-1, 1, 2)
    dst_pts = np.float32([ref_kp[m.trainIdx].pt for m in matches]).reshape(-1, 1, 2)
    M, inliers = cv2.estimateAffinePartial2D(
        src_pts,
        dst_pts,
        method=cv2.RANSAC,
        ransacReprojThreshold=ransac_threshold,
        maxIters=max_iters,
    )
    if M is None or inliers is None:
        return None, 0, 0.0
    inlier_count = int(inliers.sum())
    inlier_ratio = inlier_count / max(1, len(matches))
    return M.astype(np.float32), inlier_count, inlier_ratio


# ---------------------------------------------------------------------------
# Soccer-aware background mask (the heart of "yes, this works for soccer")
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SoccerMaskConfig:
    """Tunables for :func:`soccer_stability_mask`.

    Defaults are tuned for a centerline tripod-mounted ~180° panorama
    (Reolink Duo 3 + 16' tripod, per HARDWARE_SETUP.md). The mask is built
    ONCE per recording; the camera is fixed so no per-frame cost.
    """

    # Drop the extreme dewarp corners.
    lateral_inset_frac: float = 0.05
    # Skip the very top (timestamp banner / device overlay).
    sky_strip_top_frac: float = 0.02
    # Distance above the far touchline excluded — usually has spectators / tents.
    spectator_buffer_frac: float = 0.04
    # Goal-frame rectangle dims (relative to source dims).
    goal_box_height_frac: float = 0.06
    goal_box_width_frac: float = 0.03
    # Corner-flag pole disk radius — captures the fixed pole base; the flapping
    # fabric is higher up and excluded by the lateral inset / spectator buffer.
    corner_disk_radius_frac: float = 0.012
    # Far-touchline (white-on-green) edge band thickness, half on each side.
    touchline_band_px: int = 5
    # Bottom band excluded (near sideline foot traffic).
    near_sideline_bottom_frac: float = 0.20


def soccer_stability_mask(
    src_w: int,
    src_h: int,
    polygon: np.ndarray | None,
    config: SoccerMaskConfig | None = None,
) -> np.ndarray:
    """Build a binary mask of soccer's genuinely stable references.

    Includes: sky / distant treeline strip above the field, the far-touchline
    line edge, goal-frame rectangles around each goal, and small disks around
    each corner-flag pole base. Excludes: field interior (moving players),
    near-sideline foot traffic, extreme dewarp corners, and the spectator band
    immediately above the far touchline (canopies, walking parents).

    Without a polygon, falls back to a generic top-sky strip — usable but not
    soccer-aware.
    """
    cfg = config or SoccerMaskConfig()
    mask = np.zeros((src_h, src_w), dtype=np.uint8)
    x_left = int(src_w * cfg.lateral_inset_frac)
    x_right = int(src_w * (1.0 - cfg.lateral_inset_frac))

    if polygon is None or len(polygon) < 4:
        # No polygon → top 20% of the (laterally-inset) source as the sky strip.
        y_top = int(src_h * cfg.sky_strip_top_frac)
        y_bot = int(src_h * 0.20)
        mask[y_top:y_bot, x_left:x_right] = 255
        return mask

    poly = polygon.astype(np.float32)
    top_y = float(poly[:, 1].min())  # smallest y = topmost (far sideline in panorama)
    bot_y = float(poly[:, 1].max())  # largest y = bottommost (near sideline)
    mid_y = 0.5 * (top_y + bot_y)
    top_half = poly[poly[:, 1] < mid_y]  # far-side vertices

    # Region 1: sky / distant-treeline strip ABOVE the polygon, leaving a
    # spectator-band buffer above the far touchline (where tents tend to sit).
    sky_top = int(src_h * cfg.sky_strip_top_frac)
    spec_buf = int(src_h * cfg.spectator_buffer_frac)
    sky_bot = max(sky_top + 1, int(top_y) - spec_buf)
    if sky_bot > sky_top:
        mask[sky_top:sky_bot, x_left:x_right] = 255

    # Region 2: far-touchline edge band — straddling the polygon's top edge.
    # Draw a wide polyline along the far-side vertices in left-to-right order.
    band = int(cfg.touchline_band_px)
    if len(top_half) >= 2:
        order = np.argsort(top_half[:, 0])
        ordered = top_half[order]
        for i in range(len(ordered) - 1):
            cv2.line(
                mask,
                (int(ordered[i, 0]), int(ordered[i, 1])),
                (int(ordered[i + 1, 0]), int(ordered[i + 1, 1])),
                255,
                thickness=band * 2,
            )

    # Region 3: goal-frame rectangles at the far-left and far-right corners.
    # Extends mostly ABOVE the polygon's top edge (goal posts go up from the
    # touchline); the polygon-interior fill at the end clips any below-edge
    # spill so we don't accidentally include in-field action.
    if len(top_half) >= 2:
        gw = int(src_w * cfg.goal_box_width_frac)
        gh = int(src_h * cfg.goal_box_height_frac)
        far_left = top_half[np.argmin(top_half[:, 0])]
        far_right = top_half[np.argmax(top_half[:, 0])]
        for tip in (far_left, far_right):
            cx, cy = int(tip[0]), int(tip[1])
            x0, y0 = max(0, cx - gw // 2), max(0, cy - gh)
            x1, y1 = min(src_w, cx + gw // 2), min(src_h, cy + gh // 5)
            cv2.rectangle(mask, (x0, y0), (x1, y1), 255, thickness=-1)

    # Region 4: corner-flag pole disks at the polygon's left/right extremes.
    r = max(8, int(src_w * cfg.corner_disk_radius_frac))
    for idx in (int(np.argmin(poly[:, 0])), int(np.argmax(poly[:, 0]))):
        cx, cy = int(poly[idx, 0]), int(poly[idx, 1])
        cv2.circle(mask, (cx, cy), r, 255, thickness=-1)

    # Apply exclusions.
    # Lateral inset (drop extreme dewarp).
    mask[:, :x_left] = 0
    mask[:, x_right:] = 0
    # Near-sideline (bottom band — foot traffic, camera operator gear).
    bottom = int(src_h * (1.0 - cfg.near_sideline_bottom_frac))
    mask[bottom:, :] = 0
    # Field interior — players move; never trust features inside the polygon.
    cv2.fillPoly(mask, [poly.astype(np.int32).reshape(-1, 1, 2)], 0)

    return mask


# ---------------------------------------------------------------------------
# Reference-frame state machine: maintain a drifting reference across the video
# ---------------------------------------------------------------------------


@dataclass
class _ReferenceState:
    """Cached descriptors + cumulative offset for the active reference frame."""

    keypoints: list = field(default_factory=list)
    descriptors: np.ndarray | None = None
    frame_idx: int = 0
    # Cumulative similarity from the reference at frame_idx back to frame 0 of
    # the video (so all per-frame measurements live in a single coordinate
    # system even as references rotate).
    cumulative: SimilarityTransform = field(
        default_factory=SimilarityTransform.identity
    )


def _should_reanchor(
    measured: SimilarityTransform,
    inlier_ratio: float,
    cfg: "MotionEstimationConfig",
    frames_since_anchor: int,
) -> bool:
    """Trigger re-anchor when the current reference is degrading.

    Re-anchor cases:
      * inlier ratio dropped (scene changed enough that the descriptors aren't
        stable, e.g. sun angle, cloud cover),
      * the frame-to-reference similarity drifted past a configured budget,
      * the active reference has aged past the soft cap.
    """
    if frames_since_anchor < cfg.reanchor_min_frames:
        return False
    if inlier_ratio < cfg.reanchor_inlier_ratio:
        return True
    if frames_since_anchor >= cfg.reanchor_max_frames:
        return True
    dist = math.hypot(measured.tx, measured.ty)
    if dist > cfg.reanchor_translation_px:
        return True
    if abs(measured.theta) > math.radians(cfg.reanchor_rotation_deg):
        return True
    return False


@dataclass(frozen=True)
class MotionEstimationConfig:
    """Tunables for the per-frame motion estimation loop."""

    n_features: int = 1500
    edge_threshold: int = 12
    fast_threshold: int = 15
    ratio: float = 0.75
    ransac_threshold: float = 1.5
    ransac_max_iters: int = 2000
    min_inliers: int = 20
    min_inlier_ratio: float = 0.3
    # Drifting-reference policy.
    reanchor_min_frames: int = 60  # don't churn references too often
    reanchor_max_frames: int = 600  # ~30 s at 20 fps; cap reference age
    reanchor_inlier_ratio: float = 0.4
    reanchor_translation_px: float = 40.0
    reanchor_rotation_deg: float = 0.4


# ---------------------------------------------------------------------------
# L1-norm path optimization (Grundmann et al. 2011, per-axis independent)
# ---------------------------------------------------------------------------


def l1_smooth_path(
    cum: np.ndarray | list,
    w1: float = 1.0,
    w2: float = 10.0,
    w3: float = 100.0,
    budget: float = 60.0,
    w_stay: float = 1e-3,
) -> np.ndarray:
    """Box-constrained L1 path optimization for a single cumulative axis.

    Solves::

        minimize   w1·|D1 p|_1 + w2·|D2 p|_1 + w3·|D3 p|_1 + w_stay·|p − cum|_1
        subject to |cum[i] - p[i]| <= budget   for all i

    via :func:`scipy.optimize.linprog` with the HiGHS solver. The L1 norms
    are expressed via per-frame slack variables that sandwich each finite
    difference. The result is a piecewise-constant / piecewise-linear /
    piecewise-parabolic path (whichever segment minimises the weighted L1) —
    for a fixed-tripod camera the optimum is approximately constant, which
    is exactly what we want.

    The fixed weight ratio ``1:10:100`` (velocity : acceleration : jerk) is
    the standard Grundmann choice that strongly prefers zero-jerk paths. The
    tiny ``w_stay`` term resolves the LP's degeneracy when ``cum`` is
    near-constant (D1=D2=D3=0 for many feasible p) by gently pulling p
    toward cum within the box budget.
    """
    from scipy.optimize import linprog
    from scipy.sparse import csr_matrix

    cum_arr = np.asarray(cum, dtype=np.float64)
    n = int(cum_arr.size)
    if n < 4:
        # Not enough samples for D3; return the input as-is.
        return cum_arr.copy()

    n_s0 = n
    n_s1, n_s2, n_s3 = n - 1, n - 2, n - 3
    n_p = n
    s0_off = n_p
    s1_off = n_p + n_s0
    s2_off = n_p + n_s0 + n_s1
    s3_off = n_p + n_s0 + n_s1 + n_s2
    n_total = n_p + n_s0 + n_s1 + n_s2 + n_s3

    # Objective: zero for p, w_stay for the (p − cum) slack, w_k for each
    # derivative slack family.
    c = np.zeros(n_total, dtype=np.float64)
    c[s0_off:s1_off] = w_stay
    c[s1_off:s2_off] = w1
    c[s2_off:s3_off] = w2
    c[s3_off:] = w3

    # Total constraint count (row count of A_ub):
    #   2N box constraints (one ≤ for each side)
    # + 2N stay-close slack constraints
    # + 2(N-1) D1 slack constraints
    # + 2(N-2) D2 slack constraints
    # + 2(N-3) D3 slack constraints
    n_rows = 2 * n + 2 * n_s0 + 2 * n_s1 + 2 * n_s2 + 2 * n_s3
    b_ub = np.zeros(n_rows, dtype=np.float64)
    row_blocks: list[np.ndarray] = []
    col_blocks: list[np.ndarray] = []
    data_blocks: list[np.ndarray] = []

    def add_terms(rows: np.ndarray, cols: np.ndarray, data: np.ndarray) -> None:
        row_blocks.append(np.asarray(rows, dtype=np.int64))
        col_blocks.append(np.asarray(cols, dtype=np.int64))
        data_blocks.append(np.asarray(data, dtype=np.float64))

    # ---- Box constraints (rows 0..2N-1) ----
    # Row 2i  : -p[i]  <= budget - cum[i]
    # Row 2i+1: +p[i]  <= cum[i] + budget
    i = np.arange(n)
    add_terms(2 * i, i, -np.ones(n))
    add_terms(2 * i + 1, i, np.ones(n))
    b_ub[2 * i] = budget - cum_arr
    b_ub[2 * i + 1] = cum_arr + budget
    row_off = 2 * n

    # ---- Stay-close slack constraints |p[i] - cum[i]| <= s0[i] ----
    # Row row_off+2i  :  p[i] - cum[i] - s0[i] <= 0  →  p[i] - s0[i] <= cum[i]
    # Row row_off+2i+1: -p[i] + cum[i] - s0[i] <= 0  → -p[i] - s0[i] <= -cum[i]
    even = row_off + 2 * i
    odd = row_off + 2 * i + 1
    add_terms(even, i, np.ones(n))
    add_terms(even, s0_off + i, -np.ones(n))
    add_terms(odd, i, -np.ones(n))
    add_terms(odd, s0_off + i, -np.ones(n))
    b_ub[even] = cum_arr
    b_ub[odd] = -cum_arr
    row_off += 2 * n

    # ---- D1 slack constraints |p[i+1] - p[i]| <= s1[i] ----
    # Row row_off+2k  :  p[i+1] - p[i] - s1[k] <= 0   (k = i1)
    # Row row_off+2k+1: -p[i+1] + p[i] - s1[k] <= 0
    i1 = np.arange(n_s1)
    even = row_off + 2 * i1
    odd = row_off + 2 * i1 + 1
    add_terms(even, i1 + 1, np.ones(n_s1))
    add_terms(even, i1, -np.ones(n_s1))
    add_terms(even, s1_off + i1, -np.ones(n_s1))
    add_terms(odd, i1 + 1, -np.ones(n_s1))
    add_terms(odd, i1, np.ones(n_s1))
    add_terms(odd, s1_off + i1, -np.ones(n_s1))
    row_off += 2 * n_s1

    # ---- D2 slack constraints |p[i+2] - 2 p[i+1] + p[i]| <= s2[i] ----
    i2 = np.arange(n_s2)
    even = row_off + 2 * i2
    odd = row_off + 2 * i2 + 1
    add_terms(even, i2 + 2, np.ones(n_s2))
    add_terms(even, i2 + 1, -2 * np.ones(n_s2))
    add_terms(even, i2, np.ones(n_s2))
    add_terms(even, s2_off + i2, -np.ones(n_s2))
    add_terms(odd, i2 + 2, -np.ones(n_s2))
    add_terms(odd, i2 + 1, 2 * np.ones(n_s2))
    add_terms(odd, i2, -np.ones(n_s2))
    add_terms(odd, s2_off + i2, -np.ones(n_s2))
    row_off += 2 * n_s2

    # ---- D3 slack constraints |p[i+3] - 3 p[i+2] + 3 p[i+1] - p[i]| <= s3[i] ----
    i3 = np.arange(n_s3)
    even = row_off + 2 * i3
    odd = row_off + 2 * i3 + 1
    add_terms(even, i3 + 3, np.ones(n_s3))
    add_terms(even, i3 + 2, -3 * np.ones(n_s3))
    add_terms(even, i3 + 1, 3 * np.ones(n_s3))
    add_terms(even, i3, -np.ones(n_s3))
    add_terms(even, s3_off + i3, -np.ones(n_s3))
    add_terms(odd, i3 + 3, -np.ones(n_s3))
    add_terms(odd, i3 + 2, 3 * np.ones(n_s3))
    add_terms(odd, i3 + 1, -3 * np.ones(n_s3))
    add_terms(odd, i3, np.ones(n_s3))
    add_terms(odd, s3_off + i3, -np.ones(n_s3))

    A_ub = csr_matrix(
        (
            np.concatenate(data_blocks),
            (np.concatenate(row_blocks), np.concatenate(col_blocks)),
        ),
        shape=(n_rows, n_total),
    )

    bounds = [(None, None)] * n_p + [(0.0, None)] * (n_s0 + n_s1 + n_s2 + n_s3)

    res = linprog(
        c=c,
        A_ub=A_ub,
        b_ub=b_ub,
        bounds=bounds,
        method="highs",
    )
    if not res.success:
        raise RuntimeError(f"L1 path optimization failed: {res.message}")
    return np.asarray(res.x[:n_p], dtype=np.float64)


# ---------------------------------------------------------------------------
# Per-frame motion estimation pass (the StabilizeStep's main loop helper)
# ---------------------------------------------------------------------------


@dataclass
class FrameMotion:
    """Per-frame motion estimate output of :func:`estimate_video_motion`."""

    cum_tx: float = 0.0
    cum_ty: float = 0.0
    cum_theta: float = 0.0
    cum_log_scale: float = 0.0
    confidence: float = 0.0


def measure_frame_motion(
    rgb: np.ndarray,
    mask: np.ndarray,
    reference: _ReferenceState,
    cfg: MotionEstimationConfig,
) -> tuple[FrameMotion, bool]:
    """Estimate (one frame's) similarity to the active reference + cumulative offset.

    Updates ``reference.cumulative`` if a re-anchor is triggered (so the
    caller can keep treating the cumulative as world-frame-anchored).

    Returns ``(motion, reanchored)`` — when ``reanchored`` is True, the
    caller should refresh the reference's descriptors from the current frame
    on the next iteration (cheaper to do at the call site after RANSAC).
    """
    kp, desc = extract_features(
        rgb,
        mask,
        n_features=cfg.n_features,
        edge_threshold=cfg.edge_threshold,
        fast_threshold=cfg.fast_threshold,
    )
    if desc is None or reference.descriptors is None:
        # First frame, or features missing — just seed cumulative as identity.
        return (
            FrameMotion(
                cum_tx=reference.cumulative.tx,
                cum_ty=reference.cumulative.ty,
                cum_theta=reference.cumulative.theta,
                cum_log_scale=reference.cumulative.log_scale,
                confidence=0.0,
            ),
            False,
        )

    matches = match_with_ratio_test(reference.descriptors, desc, ratio=cfg.ratio)
    M, inlier_count, inlier_ratio = estimate_similarity(
        reference.keypoints,
        kp,
        matches,
        ransac_threshold=cfg.ransac_threshold,
        max_iters=cfg.ransac_max_iters,
    )

    if (
        M is None
        or inlier_count < cfg.min_inliers
        or inlier_ratio < cfg.min_inlier_ratio
    ):
        # Failed measurement — hold the previous cumulative; confidence 0
        # (downstream L1 caller will infer the residual is unreliable).
        return (
            FrameMotion(
                cum_tx=reference.cumulative.tx,
                cum_ty=reference.cumulative.ty,
                cum_theta=reference.cumulative.theta,
                cum_log_scale=reference.cumulative.log_scale,
                confidence=0.0,
            ),
            False,
        )

    measured = SimilarityTransform.from_affine(M)
    # Cumulative-from-reference + measured-current-to-reference = cumulative-current.
    cumulative_now = reference.cumulative.compose(measured)
    confidence = min(1.0, inlier_ratio)

    return (
        FrameMotion(
            cum_tx=cumulative_now.tx,
            cum_ty=cumulative_now.ty,
            cum_theta=cumulative_now.theta,
            cum_log_scale=cumulative_now.log_scale,
            confidence=confidence,
        ),
        _should_reanchor(
            measured,
            inlier_ratio,
            cfg,
            frames_since_anchor=max(1, reference.frame_idx),
        ),
    )


# ---------------------------------------------------------------------------
# Stabilizing-transform composition: residual → per-frame warpAffine matrix
# ---------------------------------------------------------------------------


def compose_stabilizing_transforms(
    cum_tx: np.ndarray,
    cum_ty: np.ndarray,
    cum_theta: np.ndarray,
    cum_log_scale: np.ndarray,
    smooth_tx: np.ndarray,
    smooth_ty: np.ndarray,
    smooth_theta: np.ndarray,
    smooth_log_scale: np.ndarray,
    inset_x: int,
    inset_y: int,
) -> list[np.ndarray]:
    """Per-frame 2×3 warpAffine matrix that maps stabilized-output pixel →
    wobbling-source pixel (cv2.warpAffine sampling convention).

    The composition::

        T_residual ∘ T_inset

    where ``T_residual`` is the residual cumulative-minus-smoothed similarity
    (cancels the wobble), and ``T_inset`` is a constant translation that
    re-centres the smaller "safe" crop onto the source canvas.
    """
    t_inset = SimilarityTransform(tx=float(inset_x), ty=float(inset_y))
    matrices = []
    for i in range(len(cum_tx)):
        residual = SimilarityTransform(
            tx=float(cum_tx[i] - smooth_tx[i]),
            ty=float(cum_ty[i] - smooth_ty[i]),
            theta=float(cum_theta[i] - smooth_theta[i]),
            log_scale=float(cum_log_scale[i] - smooth_log_scale[i]),
        )
        combined = residual.compose(t_inset)
        matrices.append(combined.to_affine())
    return matrices


# ---------------------------------------------------------------------------
# FrameStabilizer — loaded from motion.json, applies per-frame warpAffine
# ---------------------------------------------------------------------------


class FrameStabilizer:
    """Loads a ``motion.json`` produced by ``StabilizeStep`` and applies the
    per-frame stabilizing similarity to decoded RGB frames in-memory.

    The caller's geometry should be sized against :attr:`output_shape`
    (``(out_h, out_w)``) rather than the raw source dimensions — the
    stabilized frames are smaller by ``2·safe_inset`` on each axis so the
    warp never samples outside the source.
    """

    def __init__(
        self,
        src_size: tuple[int, int],
        output_size: tuple[int, int],
        safe_inset: tuple[int, int],
        transforms: list[np.ndarray],
        confidences: list[float],
    ):
        self.src_size = src_size  # (h, w)
        self.output_size = output_size  # (h, w)
        self.safe_inset = safe_inset  # (y, x)
        self._transforms = [np.asarray(M, dtype=np.float32) for M in transforms]
        self._confidences = list(confidences)
        # Identity-with-inset fallback when frame_idx is past the end.
        out_h, out_w = output_size
        y0, x0 = safe_inset
        self._identity_inset = np.array(
            [[1.0, 0.0, float(x0)], [0.0, 1.0, float(y0)]],
            dtype=np.float32,
        )
        self._out_w = out_w
        self._out_h = out_h

    @classmethod
    def from_json(cls, path: str | Path) -> "FrameStabilizer":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        src_size = tuple(data["src_size"])
        output_size = tuple(data["output_size"])
        safe_inset = tuple(data["safe_inset"])
        transforms = [
            np.asarray(frame["M"], dtype=np.float32) for frame in data["frames"]
        ]
        confidences = [float(frame["confidence"]) for frame in data["frames"]]
        return cls(src_size, output_size, safe_inset, transforms, confidences)

    @property
    def output_shape(self) -> tuple[int, int]:
        """``(height, width)`` of stabilized output frames."""
        return self.output_size

    def apply(self, rgb: np.ndarray, frame_idx: int) -> np.ndarray:
        """Warp-affine ``rgb`` by the precomputed stabilizing similarity.

        ``M`` in ``motion.json`` is the **dst→src** sampling map (output pixel
        → source pixel), so we pass ``WARP_INVERSE_MAP`` to keep OpenCV from
        internally inverting it (its default treats ``M`` as forward).
        """
        if 0 <= frame_idx < len(self._transforms):
            M = self._transforms[frame_idx]
        else:
            # Past the end of the trajectory (e.g. trailing trim frames). Use
            # the constant-inset translation so the consumer's geometry stays
            # valid for these frames.
            M = self._identity_inset
        return cv2.warpAffine(
            rgb,
            M,
            dsize=(self._out_w, self._out_h),
            flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
            borderMode=cv2.BORDER_REPLICATE,
        )

    def confidence(self, frame_idx: int) -> float:
        if 0 <= frame_idx < len(self._confidences):
            return self._confidences[frame_idx]
        return 0.0


# ---------------------------------------------------------------------------
# motion.json serialisation
# ---------------------------------------------------------------------------


def write_motion_json(
    path: str | Path,
    src_size: tuple[int, int],
    output_size: tuple[int, int],
    safe_inset: tuple[int, int],
    transforms: list[np.ndarray],
    confidences: list[float],
) -> None:
    """Serialise to ``motion.json`` with the format consumed by
    :class:`FrameStabilizer`."""
    payload = {
        "src_size": list(src_size),
        "output_size": list(output_size),
        "safe_inset": list(safe_inset),
        "frames": [
            {
                "M": [list(map(float, row)) for row in M],
                "confidence": float(c),
            }
            for M, c in zip(transforms, confidences)
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)


def compute_safe_inset(
    cfg_R_tx: float,
    cfg_R_ty: float,
    cfg_R_rotation_deg: float,
    cfg_R_log_scale: float,
    src_w: int,
    src_h: int,
) -> tuple[int, int]:
    """Conservative inset bound that covers translation + rotation + scale,
    using a corner-displacement worst case.

    For a corner at ``(r, r)`` from the source centre, a rotation of θ
    displaces it by ``r·sin(θ)`` orthogonally; a scale change of ``ds``
    displaces it by ``ds·r``. Add to the per-axis translation budget.
    """
    r = max(src_w, src_h) / 2.0
    rot_px = r * math.sin(math.radians(cfg_R_rotation_deg))
    scale_px = (math.exp(cfg_R_log_scale) - 1.0) * r
    inset_x = int(math.ceil(cfg_R_tx + rot_px + scale_px)) + 1
    inset_y = int(math.ceil(cfg_R_ty + rot_px + scale_px)) + 1
    return inset_y, inset_x
