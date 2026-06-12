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
    def from_affine(cls, M: np.ndarray) -> SimilarityTransform:
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
    def identity(cls) -> SimilarityTransform:
        return cls()

    def compose(self, other: SimilarityTransform) -> SimilarityTransform:
        """Return ``self ∘ other`` — apply ``other`` first, then ``self``."""
        return SimilarityTransform.from_affine(
            _compose_affine(self.to_affine(), other.to_affine())
        )

    def inverse(self) -> SimilarityTransform:
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
    keypoint_offset: tuple[float, float] = (0.0, 0.0),
):
    """Detect ORB keypoints + compute BRIEF-like descriptors inside ``mask``.

    The mask filters returned keypoints, but FAST corner detection still
    scans the whole image — at 7680×2160 that costs ~1 s/frame. Callers
    that have already cropped the image to a smaller region (the mask
    bbox, say) pass ``keypoint_offset=(dx, dy)`` so the returned keypoints
    are translated back into the original (source) coordinate frame.

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
    dx, dy = keypoint_offset
    if (dx or dy) and keypoints:
        for kp in keypoints:
            kp.pt = (kp.pt[0] + dx, kp.pt[1] + dy)
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
# Phase correlation — direct sub-pixel translation estimator
# ---------------------------------------------------------------------------
#
# Empirically (on 30s slices of real BU14 game footage, calm + windy stretches)
# phase correlation on a fixed sky/treeline ROI beats ORB+RANSAC by a wide
# margin: ~93% reduction in adjacent-frame |dy| vs ORB's -25% (which actively
# made things worse). Three reasons it wins:
#
#   1. Sub-pixel translation directly (~0.1 px noise floor).
#   2. ORB+RANSAC on cylindrically-warped panoramic source has spatial bias:
#      pixels-per-degree varies across the image, so the similarity fit
#      averages mismatched scales, over-estimating translation by ~2.8x.
#   3. A tripod camera's wobble is dominated by translation; rotation/scale
#      are sub-degree/sub-percent and the broadcast crop tolerates them.
#
# So the production estimator is phase correlation. ORB+RANSAC stays in the
# module as a fallback estimator (selected via StabilizeStepConfig) but is
# not the default.


def background_strip_roi(
    src_w: int,
    src_h: int,
    polygon: np.ndarray | None,
    *,
    lateral_inset_frac: float = 0.08,
    polygon_edge_overlap_px: int = 25,
    above_polygon_target_px: int = 240,
    top_skip_frac: float = 0.025,
) -> tuple[int, int, int, int]:
    """Derive a ``(y0, y1, x0, x1)`` ROI for phase correlation that straddles
    the field polygon's top edge — the treeline boundary, the strongest
    stable feature in a soccer-camera panorama.

    Kept as a single-ROI helper for callers that don't need multi-region
    averaging; :func:`stabilization_rois` returns the full 3-region list used
    by the production estimator.

    Lateral inset (8 % default) drops the extreme dewarp distortion. With no
    polygon, falls back to a fixed top-of-source band.
    """
    x0 = int(src_w * lateral_inset_frac)
    x1 = int(src_w * (1.0 - lateral_inset_frac))
    y_top_safe = int(src_h * top_skip_frac)
    if polygon is None or len(polygon) == 0:
        # No-polygon fallback: top ~15 % of source so we still pick up the
        # likely treeline / distant horizon.
        y0 = y_top_safe
        y1 = max(y0 + 50, int(src_h * 0.15))
        return y0, y1, x0, x1
    poly = np.asarray(polygon, dtype=np.float32)
    polygon_top_y = int(poly[:, 1].min())
    # Extend ROI a configurable margin BELOW the polygon's highest point so
    # the strip captures the treeline boundary (where most stable features
    # live), not just sky which can have moving clouds.
    y1 = max(y_top_safe + 50, polygon_top_y + polygon_edge_overlap_px)
    y0 = max(y_top_safe, y1 - above_polygon_target_px)
    return y0, y1, x0, x1


def stabilization_grid_rois(
    src_w: int,
    src_h: int,
    polygon: np.ndarray | None,
    *,
    roi_w_px: int = 1300,
    roi_h_px: int = 280,
) -> list[tuple[int, int, int, int]]:
    """3x3 grid of phase-correlation ROIs spanning the source spatially.

    Per-ROI phase correlation gives 9 motion vectors that
    :func:`fit_similarity_from_motion_vectors` then fits to a 2D similarity
    (translation + rotation + scale). Multiple X positions at the same Y
    are what reveal ROLL — left and right edges of a roll-wobbling camera
    move in opposite vertical directions, and the per-frame slope yields θ.

    Three Y bands (sky / field-mid / foreground) and three X bands
    (left / center / right). Center positions are picked to keep ROIs inside
    the typical field polygon's lateral extent and the dewarped-safe
    horizontal band (drops the worst cylindrical-projection corners).
    """
    if polygon is None or len(polygon) == 0:
        # Without a polygon, fall back to a single-band 3x1 grid in the
        # top of the source so we still get rotation from the left/right
        # vertical-motion gradient.
        y_top = max(40, int(src_h * 0.04))
        y_bot = y_top + roi_h_px
        x_step = src_w // 4
        return [
            (y_top, y_bot, x_step, x_step + roi_w_px),
            (y_top, y_bot, 2 * x_step - roi_w_px // 2, 2 * x_step + roi_w_px // 2),
            (y_top, y_bot, 3 * x_step - roi_w_px, 3 * x_step),
        ]
    poly = np.asarray(polygon, dtype=np.float32)
    polygon_top_y = int(poly[:, 1].min())
    polygon_bottom_y = int(poly[:, 1].max())
    poly_h = max(1, polygon_bottom_y - polygon_top_y)

    # Y anchors: sky strip (just above polygon top), mid-field (~35% down
    # into polygon, where field lines provide texture), foreground (just
    # below polygon bottom, where the near sideline / bench lives).
    y_anchors = [
        max(40, polygon_top_y - roi_h_px + 50),
        polygon_top_y + int(poly_h * 0.35),
        min(src_h - roi_h_px - 1, polygon_bottom_y + 100),
    ]
    # X anchors: evenly spaced across the laterally-safe band. Center
    # positions chosen so the LEFT-RIGHT span is maximised (more rotation
    # leverage per degree of roll) while staying off the extreme dewarp
    # corners. With a 7680-wide source: left x=1450, center x=3200, right
    # x=5300 — giving ~3850 px span between left and right ROI centers,
    # ~0.5 of the source width.
    x_anchors_pct = [0.19, 0.42, 0.69]
    x_anchors = [int(src_w * p) for p in x_anchors_pct]

    rois = []
    for ya in y_anchors:
        for xa in x_anchors:
            rois.append((ya, ya + roi_h_px, xa, xa + roi_w_px))
    return rois


def fit_similarity_from_motion_vectors(
    roi_centers: list[tuple[float, float]],
    motion_vectors: list[tuple[float, float]],
    *,
    method: int | None = None,
    ransac_threshold: float = 2.0,
) -> SimilarityTransform:
    """Fit a 2D similarity (translation + rotation + uniform scale) to per-ROI
    motion vectors.

    *roi_centers* are the (x, y) source pixel coordinates of the ROI midpoints;
    *motion_vectors* are the per-ROI (dx, dy) measured by phase correlation.
    Together they give 2D point correspondences (src → src + delta) that
    :func:`cv2.estimateAffinePartial2D` can fit a similarity to.

    Why this matters for stabilization: a per-frame similarity captures the
    ROLL component that pure translation can't. For a camera rolling about its
    optical center, left/right edges move in opposite vertical directions; the
    fitted similarity's ``theta`` recovers that rotation directly, and the
    downstream warpAffine in :class:`FrameStabilizer` undoes it across the
    whole frame.
    """
    if len(roi_centers) < 2:
        return SimilarityTransform.identity()
    src_pts = np.array(roi_centers, dtype=np.float32).reshape(-1, 1, 2)
    dst_pts = np.array(
        [
            (cx + dx, cy + dy)
            for (cx, cy), (dx, dy) in zip(roi_centers, motion_vectors, strict=False)
        ],
        dtype=np.float32,
    ).reshape(-1, 1, 2)
    if method is None:
        method = cv2.RANSAC
    M, _ = cv2.estimateAffinePartial2D(
        src_pts,
        dst_pts,
        method=method,
        ransacReprojThreshold=ransac_threshold,
    )
    if M is None:
        return SimilarityTransform.identity()
    return SimilarityTransform.from_affine(M)


def stabilization_rois(
    src_w: int,
    src_h: int,
    polygon: np.ndarray | None,
    *,
    lateral_inset_frac: float = 0.18,
    polygon_edge_overlap_px: int = 25,
    above_polygon_target_px: int = 240,
    top_skip_frac: float = 0.025,
    roi_strip_height_px: int = 300,
) -> list[tuple[int, int, int, int]]:
    """Return THREE phase-correlation ROIs spanning the source vertically:
    sky/treeline (above polygon top), field mid, and foreground (below
    polygon bottom). The production estimator averages per-frame deltas
    across all three to defuse parallax from camera-mast translation.

    Why three: a tripod mast flexing in wind produces both rotation AND
    translation. Translation causes parallax — near objects (foreground)
    shift more than distant ones (treeline). A single ROI's motion estimate
    over-corrects far-away regions and under-corrects nearby ones. Averaging
    three depth bands lands the estimate in the middle, giving roughly
    uniform stabilization across the full vertical extent (empirically
    +68 % reduction in the worst-region vs +51 % for sky-only on the same
    BU14 windy slice).

    Wider lateral inset (18 % default) drops the extreme dewarp corners
    where the cylindrical projection's pixels-per-degree is most distorted.

    Falls back to a single sky-strip ROI when no polygon is available.
    """
    # Without a polygon we can only place the sky strip — fall back to the
    # single-ROI helper.
    if polygon is None or len(polygon) == 0:
        return [
            background_strip_roi(
                src_w,
                src_h,
                None,
                lateral_inset_frac=lateral_inset_frac,
                polygon_edge_overlap_px=polygon_edge_overlap_px,
                above_polygon_target_px=above_polygon_target_px,
                top_skip_frac=top_skip_frac,
            )
        ]
    poly = np.asarray(polygon, dtype=np.float32)
    polygon_top_y = int(poly[:, 1].min())
    polygon_bottom_y = int(poly[:, 1].max())
    x0 = int(src_w * lateral_inset_frac)
    x1 = int(src_w * (1.0 - lateral_inset_frac))
    y_top_safe = int(src_h * top_skip_frac)
    h = roi_strip_height_px

    # 1. Sky / treeline strip (above polygon, capturing the boundary).
    sky_y1 = max(y_top_safe + 50, polygon_top_y + polygon_edge_overlap_px)
    sky_y0 = max(y_top_safe, sky_y1 - above_polygon_target_px)
    rois = [(sky_y0, sky_y1, x0, x1)]

    # 2. Field-mid strip (between polygon top and bottom). Phase correlation
    # works despite moving players because grass+field-line pixels dominate.
    poly_h = max(1, polygon_bottom_y - polygon_top_y)
    mid_y0 = polygon_top_y + int(poly_h * 0.35)
    mid_y1 = min(src_h, mid_y0 + h)
    rois.append((mid_y0, mid_y1, x0, x1))

    # 3. Foreground strip (below polygon's near sideline). Captures
    # bench / gear / grass below the field — the highest-parallax region.
    fg_y0 = min(src_h - h - 1, polygon_bottom_y + 100)
    fg_y1 = min(src_h, fg_y0 + h)
    if fg_y1 > fg_y0:
        rois.append((fg_y0, fg_y1, x0, x1))
    return rois


def phase_correlate_translation(
    prev_gray: np.ndarray,
    cur_gray: np.ndarray,
) -> tuple[float, float, float]:
    """Sub-pixel ``(dx, dy)`` translation from *prev_gray* to *cur_gray*,
    plus a [0, 1] response score (the correlation peak magnitude).

    Inputs must be the same shape, grayscale, float32. The response score
    drops on featureless / low-texture frames — caller can use it as a
    confidence gate.
    """
    (dx, dy), response = cv2.phaseCorrelate(prev_gray, cur_gray)
    return float(dx), float(dy), float(response)


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
    cfg: MotionEstimationConfig,
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
    keypoint_offset: tuple[float, float] = (0.0, 0.0),
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
        keypoint_offset=keypoint_offset,
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
    """Per-frame 2×3 warpAffine matrix (dst→src under WARP_INVERSE_MAP) that
    samples the wobbling source so a stabilized output pixel shows the
    content the smoothed-camera-path frame would have at that pixel.

    ``cv2.estimateAffinePartial2D(src=cur_kp, dst=ref_kp)`` gives the
    transform that maps a CURRENT keypoint into the REFERENCE frame, so the
    cumulative ``(cum_tx, cum_ty, cum_theta, cum_log_scale)`` is the
    current→reference transform — the **inverse** of the camera's wobble in
    source coords. To undo the residual wobble we therefore apply
    ``residual.inverse()`` (which is exactly the wobble in source coords),
    then re-centre the smaller "safe" crop with ``T_inset``::

        M = T_residual_inverse ∘ T_inset
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
        combined = residual.inverse().compose(t_inset)
        matrices.append(combined.to_affine())
    return matrices


# ---------------------------------------------------------------------------
# FrameStabilizer — loaded from motion.json, applies per-frame warpAffine
# ---------------------------------------------------------------------------


def build_polygon_zone_mask(
    polygon: np.ndarray,
    src_h: int,
    src_w: int,
) -> np.ndarray:
    """Build a 3-zone classification mask from the field polygon — the
    foundation of polygon-aware stabilization.

    The polygon is a 2D depth proxy:

    * **Zone 1 (sky):** above the polygon's top edge AND laterally within
      the polygon's top extent (i.e. actual sky/treeline beyond the field).
      Far-distance content — minimal parallax.
    * **Zone 2 (field):** inside the polygon. Field surface — moderate depth.
    * **Zone 3 (near):** everything else. Below the polygon (foreground /
      bench / coach line) AND above the polygon at the lateral corners
      where sideline spectators sit — these are physically NEAR the camera
      despite being at the top of the source image, which is why a naive
      top/bottom row blend fails them.

    Returns a ``(src_h, src_w)`` uint8 array with values 1, 2, or 3.
    """
    poly = np.asarray(polygon, dtype=np.float32)
    mid_y = 0.5 * (poly[:, 1].min() + poly[:, 1].max())
    top_half = poly[poly[:, 1] < mid_y]
    if len(top_half) < 2:
        # Degenerate polygon — fall back to "everything is field" so the
        # caller's existing single-warp path still works.
        return np.full((src_h, src_w), 2, dtype=np.uint8)
    top_x_min = int(top_half[:, 0].min())
    top_x_max = int(top_half[:, 0].max())

    # Hard polygon fill: zone-2 pixels.
    poly_int = poly.astype(np.int32).reshape(-1, 1, 2)
    poly_fill = np.zeros((src_h, src_w), dtype=np.uint8)
    cv2.fillPoly(poly_fill, [poly_int], 255)

    # For each x column, look up the polygon's top-edge y at that x by
    # piecewise-linear interpolation between adjacent top-half vertices.
    top_y_per_x = np.full(src_w, src_h, dtype=np.int32)
    top_sorted = top_half[np.argsort(top_half[:, 0])]
    for i in range(len(top_sorted) - 1):
        x0, y0 = int(top_sorted[i, 0]), int(top_sorted[i, 1])
        x1, y1 = int(top_sorted[i + 1, 0]), int(top_sorted[i + 1, 1])
        if x1 == x0:
            continue
        for x in range(x0, x1 + 1):
            t = (x - x0) / (x1 - x0)
            top_y_per_x[x] = int(y0 + t * (y1 - y0))

    # Default everything to zone 3 (near), then carve out field + sky.
    zone = np.full((src_h, src_w), 3, dtype=np.uint8)
    yy = np.arange(src_h).reshape(-1, 1)
    xx = np.arange(src_w).reshape(1, -1)
    poly_top_y = top_y_per_x.reshape(1, -1)
    in_polygon = poly_fill > 0
    above_polygon = yy < poly_top_y
    in_lateral_sky = (xx >= top_x_min) & (xx <= top_x_max)
    zone[in_polygon] = 2
    zone[above_polygon & in_lateral_sky & ~in_polygon] = 1
    return zone


class FrameStabilizer:
    """Loads a ``motion.json`` produced by ``StabilizeStep`` and applies the
    per-frame stabilizing similarity to decoded RGB frames in-memory.

    The caller's geometry should be sized against :attr:`output_shape`
    (``(out_h, out_w)``) rather than the raw source dimensions — the
    stabilized frames are smaller by ``2·safe_inset`` on each axis so the
    warp never samples outside the source.

    Two modes:

    * **single-warp** (default) — one similarity matrix per frame, applied
      to the whole frame with a single ``warpAffine``.
    * **polygon-zone blend** — three similarity matrices per frame, one for
      each of the three depth zones derived from the field polygon, blended
      per-pixel via the precomputed zone mask. Roughly 3 × the apply cost
      of single-warp, in exchange for stabilising the high-parallax corners
      (sideline spectators above the polygon's lateral extent) that no
      single similarity can handle.
    """

    def __init__(
        self,
        src_size: tuple[int, int],
        output_size: tuple[int, int],
        safe_inset: tuple[int, int],
        transforms: list[np.ndarray] | None,
        confidences: list[float],
        *,
        zone_transforms: dict[str, list[np.ndarray]] | None = None,
        polygon: np.ndarray | None = None,
    ):
        self.src_size = src_size  # (h, w)
        self.output_size = output_size  # (h, w)
        self.safe_inset = safe_inset  # (y, x)
        self._confidences = list(confidences)
        out_h, out_w = output_size
        y0, x0 = safe_inset
        self._identity_inset = np.array(
            [[1.0, 0.0, float(x0)], [0.0, 1.0, float(y0)]],
            dtype=np.float32,
        )
        self._out_w = out_w
        self._out_h = out_h
        if zone_transforms is not None and polygon is not None:
            # Polygon-zone blend mode.
            self._mode = "zone"
            self._zone_transforms = {
                name: [np.asarray(M, dtype=np.float32) for M in lst]
                for name, lst in zone_transforms.items()
            }
            src_h, src_w = src_size
            # The FULL (source-res) zone mask is kept around for point
            # transformation — looking up which band a SOURCE-coord ball
            # detection sits in so we can apply that band's per-frame
            # similarity to its (x, y).
            self._zone_mask_full = build_polygon_zone_mask(polygon, src_h, src_w)
            iy, ix = safe_inset
            zone_crop = self._zone_mask_full[iy : iy + out_h, ix : ix + out_w]
            # Pre-compute per-zone broadcast masks (H, W, 1) once.
            self._mask_sky = (zone_crop == 1)[..., None]
            self._mask_field = (zone_crop == 2)[..., None]
            # near is the remainder — no mask needed if we use np.where chains
            self._transforms = None
        else:
            self._mode = "single"
            if transforms is None:
                raise ValueError(
                    "FrameStabilizer: single-warp mode requires a 'transforms' "
                    "list; zone mode requires 'zone_transforms' + 'polygon'."
                )
            self._transforms = [np.asarray(M, dtype=np.float32) for M in transforms]

    @classmethod
    def from_json(cls, path: str | Path) -> FrameStabilizer:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        src_size = tuple(data["src_size"])
        output_size = tuple(data["output_size"])
        safe_inset = tuple(data["safe_inset"])
        # Backward-compatible single-warp path.
        if "frames" in data:
            transforms = [
                np.asarray(frame["M"], dtype=np.float32) for frame in data["frames"]
            ]
            confidences = [float(frame["confidence"]) for frame in data["frames"]]
            return cls(src_size, output_size, safe_inset, transforms, confidences)
        # Polygon-zone blend path.
        if "zones" not in data or "polygon" not in data:
            raise ValueError(
                f"motion.json at {path} has neither 'frames' nor "
                f"('zones' + 'polygon') — cannot construct FrameStabilizer."
            )
        zone_transforms = {
            name: [np.asarray(M, dtype=np.float32) for M in lst]
            for name, lst in data["zones"].items()
        }
        confidences = [float(c) for c in data.get("confidences", [])]
        polygon = np.asarray(data["polygon"], dtype=np.float32)
        return cls(
            src_size,
            output_size,
            safe_inset,
            transforms=None,
            confidences=confidences,
            zone_transforms=zone_transforms,
            polygon=polygon,
        )

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
        if self._mode == "zone":
            return self._apply_zones(rgb, frame_idx)
        if 0 <= frame_idx < len(self._transforms):
            M = self._transforms[frame_idx]
        else:
            # Past the end of the trajectory — use the constant-inset
            # translation so consumer geometry stays valid.
            M = self._identity_inset
        return cv2.warpAffine(
            rgb,
            M,
            dsize=(self._out_w, self._out_h),
            flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
            borderMode=cv2.BORDER_REPLICATE,
        )

    def _apply_zones(self, rgb: np.ndarray, frame_idx: int) -> np.ndarray:
        def pick(name: str) -> np.ndarray:
            lst = self._zone_transforms[name]
            if 0 <= frame_idx < len(lst):
                return lst[frame_idx]
            return self._identity_inset

        common = {
            "dsize": (self._out_w, self._out_h),
            "flags": cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
            "borderMode": cv2.BORDER_REPLICATE,
        }
        sky = cv2.warpAffine(rgb, pick("sky"), **common)
        field = cv2.warpAffine(rgb, pick("field"), **common)
        near = cv2.warpAffine(rgb, pick("near"), **common)
        # Per-pixel select: sky if zone==1, field if zone==2, else near.
        return np.where(self._mask_sky, sky, np.where(self._mask_field, field, near))

    def confidence(self, frame_idx: int) -> float:
        if 0 <= frame_idx < len(self._confidences):
            return self._confidences[frame_idx]
        return 0.0

    def transform_points(self, points_xy: np.ndarray, frame_idx: int) -> np.ndarray:
        """Map ``points_xy`` from source pixels to stabilized output pixels.

        ``points_xy`` is an ``(N, 2)`` float array of ``(x, y)`` source-frame
        coordinates (e.g. ball-detection centroids). Returns an ``(N, 2)``
        array of stabilized-output-frame coordinates.

        In zone-blend mode each point looks up its zone from the polygon-
        derived mask AT THE SOURCE PIXEL and uses that zone's per-frame
        similarity — so a ball in the field band gets warped by the field
        path, exactly like the rendered pixels around it.

        This is the cheap path for re-running stabilization without
        re-decoding: existing detections in raw-source coords can be
        forwarded through a new ``motion.json`` for ~µs per point.

        ``M`` in motion.json is the dst→src sampling map (output → source);
        to send a source point forward to the output we apply ``M⁻¹``.
        """
        pts = np.asarray(points_xy, dtype=np.float32)
        if pts.ndim != 2 or pts.shape[1] != 2:
            raise ValueError(f"points_xy must be (N, 2); got shape {pts.shape}")
        if pts.shape[0] == 0:
            return pts.copy()

        if self._mode == "single":
            if 0 <= frame_idx < len(self._transforms):
                M = self._transforms[frame_idx]
            else:
                M = self._identity_inset
            M_inv = cv2.invertAffineTransform(M)
            homog = np.concatenate(
                [pts, np.ones((pts.shape[0], 1), dtype=np.float32)], axis=1
            )
            return (M_inv @ homog.T).T

        # Zone-blend: per-point zone lookup + zone-specific inverse.
        src_h, src_w = self.src_size
        out = np.empty_like(pts)
        zone_to_name = {1: "sky", 2: "field", 3: "near"}
        for i, (xs, ys) in enumerate(pts):
            ix = int(np.clip(xs, 0, src_w - 1))
            iy = int(np.clip(ys, 0, src_h - 1))
            zone_name = zone_to_name[int(self._zone_mask_full[iy, ix])]
            lst = self._zone_transforms[zone_name]
            if 0 <= frame_idx < len(lst):
                M = lst[frame_idx]
            else:
                M = self._identity_inset
            M_inv = cv2.invertAffineTransform(M)
            out[i] = M_inv @ np.array([xs, ys, 1.0], dtype=np.float32)
        return out


# ---------------------------------------------------------------------------
# motion.json serialisation
# ---------------------------------------------------------------------------


def write_motion_json(
    path: str | Path,
    src_size: tuple[int, int],
    output_size: tuple[int, int],
    safe_inset: tuple[int, int],
    transforms: list[np.ndarray] | None,
    confidences: list[float],
    *,
    zone_transforms: dict[str, list[np.ndarray]] | None = None,
    polygon: np.ndarray | None = None,
) -> None:
    """Serialise to ``motion.json`` consumed by :class:`FrameStabilizer`.

    Single-warp mode (default): pass ``transforms`` as the per-frame 2×3
    similarities. Zone-blend mode: pass ``zone_transforms`` (dict keyed by
    ``sky`` / ``field`` / ``near``) and the ``polygon`` used to build the
    zone mask at apply time.

    The two modes are mutually exclusive — one of the two pair must be set.
    """
    if zone_transforms is not None and polygon is not None:
        payload = {
            "src_size": list(src_size),
            "output_size": list(output_size),
            "safe_inset": list(safe_inset),
            "polygon": [[float(x), float(y)] for x, y in np.asarray(polygon)],
            "zones": {
                name: [[[float(v) for v in row] for row in M] for M in lst]
                for name, lst in zone_transforms.items()
            },
            "confidences": [float(c) for c in confidences],
        }
    else:
        if transforms is None:
            raise ValueError(
                "write_motion_json: single-warp mode needs 'transforms'; zone "
                "mode needs 'zone_transforms' + 'polygon'."
            )
        payload = {
            "src_size": list(src_size),
            "output_size": list(output_size),
            "safe_inset": list(safe_inset),
            "frames": [
                {
                    "M": [list(map(float, row)) for row in M],
                    "confidence": float(c),
                }
                for M, c in zip(transforms, confidences, strict=False)
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
