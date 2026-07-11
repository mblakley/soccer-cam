"""Zero-touch field-plane geometry: image <-> world meters from the field polygon.

Everything the world-model needs about *where the ball can be* is derived from
the auto-detected 10-point field polygon — no user marking, no calibration:

1. **Ground-plane homography** mapping image pixels <-> field-plane metric
   coordinates, fit from the 10 boundary points (the two touchlines) against a
   field rectangle of known/assumed dimensions.
2. **Geometric ball-size(location) prior** — the apparent pixel diameter of a
   real 0.22 m ball at any field location, computed by projecting a ball-sized
   ground segment through the homography. This is the size-consistency
   discriminator that geometrically rejects the look-alikes the appearance
   detector false-fires on (a 50 px player blob *cannot* be the ball in the far
   field where the ball must be ~8 px). Unlike ``field_warp.build_field_warp``
   it needs **no measured detections** — pure geometry, so it works zero-touch
   on a brand-new game the moment the field is detected.
3. **Field + dome support** — the in-bounds region (polygon + margin) plus an
   upward image margin for the airborne "dome" above the field.

When the field polygon is missing or degenerate this degrades **gracefully** to
a neutral geometry: support accepts everywhere and the size prior is a uniform
fallback, so a poor field detection never breaks ball detection outright (the
manual polygon-refine + re-render path is the rare escape hatch, handled
upstream).

Polygon layout (from ``video_grouper.inference.field_detector``)::

       9---8---7---6---5     far sideline  (top of image, world_y = field_width)
      /                 \\
     0---1---2---3---4         near sideline (bottom of image, world_y = 0)

near sideline: index 0 (left) .. 4 (right);  far sideline: index 5 (right) .. 9 (left).

Pure numpy + cv2 (no torch / onnxruntime) so it imports at both training and
inference time.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

# Real-world ball diameter. Size 5 (regulation, U12+) = 0.22 m; size 4 ~ 0.20 m.
SOCCER_BALL_DIAMETER_M = 0.22

# Assumed field dimensions when metric scale has not been auto-estimated from
# markings (goal width 7.32 m, penalty box, center circle). Reasonable youth
# 11v11 defaults; the homography *topology* is independent of these, only the
# metric outputs (and the physics thresholds downstream) scale with them.
DEFAULT_FIELD_LENGTH_M = 95.0
DEFAULT_FIELD_WIDTH_M = 60.0

# Neutral fallback apparent ball size (px) used when there is no valid polygon.
DEFAULT_FALLBACK_BALL_PX = 8.0

# Minimum polygon area (px^2) to be considered a real field, not a degenerate
# sliver from a bad detection.
MIN_POLYGON_AREA_PX = 50_000.0

# Max acceptable mean reprojection error (px) of the assumed equal-spaced world
# rectangle mapped back through the fitted homography vs the actual polygon; above
# this the fit is rejected as unusable and geometry degrades to neutral.
#
# CALIBRATION (measured on real held-out polygons, 2026-07-11): the detector's 10
# touchline points are NOT at exact 0.25 intervals, so even a GOOD polygon fits the
# idealized equal-spaced rectangle only coarsely — ~250-500 px mean error — and
# least-squares absorbs moderate corruption (heavy jitter still ~350-450 px). The
# reprojection error therefore cannot separate a good polygon from a subtly-bad one;
# it is a CATASTROPHE gate only. The threshold is set well above the good+corrupted
# band so a real polygon is never wrongly rejected (which would collapse the whole
# metric tracker), while singular / non-finite / absurd (>1000 px) fits still fall
# back gracefully. A precise geometric sanity check (polygon ordering/convexity) is
# a possible future upgrade if stronger rejection is needed.
MAX_REPROJ_ERROR_PX = 1000.0


def _apply_homography(h: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """Apply a 3x3 homography to ``(M, 2)`` points in float64.

    Done by hand (not ``cv2.perspectiveTransform``) to keep full float64
    precision for round-trip tests.
    """
    pts = np.asarray(pts, dtype=np.float64).reshape(-1, 2)
    hom = np.concatenate([pts, np.ones((pts.shape[0], 1))], axis=1) @ h.T
    w = hom[:, 2:3]
    # Guard against division by ~0 at the horizon (points on the field never hit
    # this, but a caller may probe outside the field). Preserve sign: for a small
    # NEGATIVE w, np.sign(w)*1e-12 + 1e-12 collapsed to exactly 0.0 -> inf/nan,
    # the very failure this guard exists to prevent.
    w = np.where(np.abs(w) < 1e-12, np.where(w < 0.0, -1e-12, 1e-12), w)
    return hom[:, :2] / w


def _touchline_world_points(length_m: float, width_m: float) -> np.ndarray:
    """World coords of the 10 boundary points (equally spaced on each touchline).

    Near touchline at ``world_y = 0`` (index 0 left .. 4 right), far touchline at
    ``world_y = width`` (index 5 right .. 9 left). World X runs 0..length along
    the touchline.
    """
    near_x = np.array([0.0, 0.25, 0.5, 0.75, 1.0]) * length_m
    far_x = np.array([1.0, 0.75, 0.5, 0.25, 0.0]) * length_m  # right -> left
    near = np.column_stack([near_x, np.zeros(5)])
    far = np.column_stack([far_x, np.full(5, width_m)])
    return np.concatenate([near, far], axis=0)  # (10, 2)


@dataclass(frozen=True)
class FieldGeometry:
    """Zero-touch field geometry derived from the field polygon.

    Build once per game (the camera is fixed) via :func:`build_field_geometry`
    and reuse across all frames. When ``valid`` is False the object is a neutral
    fallback (no homography) whose support accepts everywhere and whose size
    prior is uniform.

    Attributes:
        polygon: ``(10, 2)`` field polygon in source pixels, or ``None``.
        h_img2world: ``3x3`` image->world homography, or ``None`` if neutral.
        h_world2img: ``3x3`` world->image homography, or ``None`` if neutral.
        field_length_m: Assumed/estimated field length (world X extent).
        field_width_m: Assumed/estimated field width (world Y extent).
        ball_diameter_m: Real ball diameter used for the size prior.
        valid: True if a usable homography was fit; False = neutral fallback.
        fallback_ball_px: Uniform apparent ball size used when neutral.
    """

    polygon: np.ndarray | None
    h_img2world: np.ndarray | None
    h_world2img: np.ndarray | None
    field_length_m: float
    field_width_m: float
    ball_diameter_m: float
    valid: bool
    fallback_ball_px: float

    # ---- coordinate transforms --------------------------------------------

    def image_to_world(self, pts_xy: np.ndarray) -> np.ndarray:
        """Map source pixel ``(x, y)`` to field-plane metric ``(X, Y)``.

        Assumes the points lie on the ground plane. ``(M, 2)`` in, ``(M, 2)``
        out. Raises if the geometry is neutral (no homography).
        """
        if not self.valid or self.h_img2world is None:
            raise ValueError("image_to_world requires a valid (non-neutral) geometry")
        return _apply_homography(self.h_img2world, pts_xy)

    def world_to_image(self, pts_xy: np.ndarray) -> np.ndarray:
        """Map field-plane metric ``(X, Y)`` to source pixel ``(x, y)``."""
        if not self.valid or self.h_world2img is None:
            raise ValueError("world_to_image requires a valid (non-neutral) geometry")
        return _apply_homography(self.h_world2img, pts_xy)

    # ---- size prior (the geometric discriminator) -------------------------

    def expected_ball_diameter_px(self, pts_xy: np.ndarray) -> np.ndarray:
        """Apparent ball diameter (px) at each source pixel location.

        Projects a ``ball_diameter_m`` ground segment centered at each point
        (averaged over the world-X and world-Y orientations) back to image
        pixels. Larger near the camera (bottom of image), smaller in the far
        field (top) — the perspective gradient, derived purely from geometry.

        ``(M, 2)`` or ``(2,)`` in; returns ``(M,)``. Neutral geometry returns
        the uniform ``fallback_ball_px`` for every point.
        """
        pts = np.asarray(pts_xy, dtype=np.float64).reshape(-1, 2)
        if not self.valid:
            return np.full(pts.shape[0], self.fallback_ball_px)

        world = self.image_to_world(pts)  # (M, 2)
        half = self.ball_diameter_m / 2.0
        sizes = np.empty(pts.shape[0], dtype=np.float64)
        for axis in (0, 1):  # world-X then world-Y oriented segment
            off = np.zeros((1, 2))
            off[0, axis] = half
            img_plus = self.world_to_image(world + off)
            img_minus = self.world_to_image(world - off)
            seg = np.linalg.norm(img_plus - img_minus, axis=1)
            sizes = seg if axis == 0 else (sizes + seg)
        return sizes / 2.0

    def size_consistency_logprob(
        self,
        pts_xy: np.ndarray,
        observed_diameter_px: np.ndarray,
        rel_sigma: float = 0.5,
    ) -> np.ndarray:
        """Log-likelihood that a blob of ``observed_diameter_px`` is the ball.

        Gaussian in log-size: a candidate whose apparent size grossly mismatches
        the geometric expectation at its location (e.g. a player-sized blob in
        the far field) is heavily penalized. ``rel_sigma`` is the std of
        ``log(observed/expected)`` (0.5 ~ a factor of ~1.65 tolerance).

        Neutral geometry returns zeros (no size information → no penalty).
        """
        pts = np.asarray(pts_xy, dtype=np.float64).reshape(-1, 2)
        obs = np.asarray(observed_diameter_px, dtype=np.float64).reshape(-1)
        if not self.valid:
            return np.zeros(pts.shape[0])
        expected = np.clip(self.expected_ball_diameter_px(pts), 1e-3, None)
        obs = np.clip(obs, 1e-3, None)
        z = np.log(obs / expected) / rel_sigma
        return -0.5 * z * z

    # ---- support region ----------------------------------------------------

    def is_in_support(
        self,
        pts_xy: np.ndarray,
        margin_px: float = 50.0,
        dome_px: float = 0.0,
    ) -> np.ndarray:
        """Boolean mask: is each point in the field + dome support region.

        Uses ``cv2.pointPolygonTest`` against the field polygon with a soft
        ``margin_px`` (covers throw-ins / balls on the line). ``dome_px`` adds an
        extra *upward* (toward smaller y / the far field) tolerance for airborne
        balls that appear above the far touchline. Neutral geometry accepts
        everywhere.

        ``(M, 2)`` or ``(2,)`` in; returns ``(M,)`` bool.
        """
        pts = np.asarray(pts_xy, dtype=np.float64).reshape(-1, 2)
        if self.polygon is None:
            return np.ones(pts.shape[0], dtype=bool)

        poly = self.polygon.reshape(-1, 1, 2).astype(np.float32)
        out = np.empty(pts.shape[0], dtype=bool)
        far_top = float(self.polygon[:, 1].min())  # smallest y = far edge
        for i, (x, y) in enumerate(pts):
            dist = cv2.pointPolygonTest(poly, (float(x), float(y)), True)
            inside = dist >= -margin_px
            # Airborne dome: accept points above the far edge within dome_px.
            if not inside and dome_px > 0.0 and (far_top - dome_px) <= y < far_top:
                inside = True
            out[i] = inside
        return out


def build_field_geometry(
    polygon: np.ndarray | None,
    field_length_m: float = DEFAULT_FIELD_LENGTH_M,
    field_width_m: float = DEFAULT_FIELD_WIDTH_M,
    ball_diameter_m: float = SOCCER_BALL_DIAMETER_M,
    fallback_ball_px: float = DEFAULT_FALLBACK_BALL_PX,
) -> FieldGeometry:
    """Build :class:`FieldGeometry` from an auto-detected 10-point polygon.

    Never raises. Degrades in two independent stages:

    - **Support** (the field/dome region) is available whenever the polygon is a
      finite ``(10, 2)`` of sufficient area — it only needs the polygon as a
      perimeter, so it works even for a human-edited polygon whose point ordering
      doesn't match the equal-spacing assumption.
    - **Metric homography + size prior** additionally require a clean homography
      fit (low reprojection error). If that fails, ``valid`` is False and the
      size prior falls back to uniform, but support still works.

    Args:
        polygon: ``(10, 2)`` field polygon in source pixels, or ``None``.
        field_length_m: World X extent (touchline length).
        field_width_m: World Y extent (near->far distance).
        ball_diameter_m: Real ball diameter for the size prior.
        fallback_ball_px: Uniform apparent ball size when the homography is unfit.
    """
    poly_support: np.ndarray | None = None
    if polygon is not None:
        poly = np.asarray(polygon, dtype=np.float64)
        if (
            poly.shape == (10, 2)
            and np.all(np.isfinite(poly))
            and abs(cv2.contourArea(poly.astype(np.float32))) >= MIN_POLYGON_AREA_PX
        ):
            poly_support = poly

    h_img2world: np.ndarray | None = None
    h_world2img: np.ndarray | None = None
    if poly_support is not None:
        world = _touchline_world_points(field_length_m, field_width_m)
        h, _ = cv2.findHomography(poly_support, world, 0)
        if h is not None:
            try:
                h_inv = np.linalg.inv(h)
            except np.linalg.LinAlgError:
                h_inv = None
            if h_inv is not None:
                # Measure the FIT quality: map the assumed equal-spaced world
                # rectangle back to image through the fitted inverse and compare to
                # the actual polygon (pixels, matching MAX_REPROJ_ERROR_PX). A
                # crooked / mis-ordered / foreshortened polygon cannot match the
                # rectangle, so its fit error is large and the homography is
                # rejected (-> neutral geometry + uniform size prior).
                # NB: round-tripping poly -> world -> poly through h and its own
                # algebraic inverse is the identity (err ~ 1e-11) and measures
                # nothing — it made this gate a no-op that always passed.
                reproj = _apply_homography(h_inv, world)
                if np.all(np.isfinite(reproj)):
                    err = float(np.mean(np.linalg.norm(reproj - poly_support, axis=1)))
                    if err <= MAX_REPROJ_ERROR_PX:
                        h_img2world, h_world2img = h, h_inv

    return FieldGeometry(
        polygon=None if poly_support is None else poly_support.astype(np.float32),
        h_img2world=h_img2world,
        h_world2img=h_world2img,
        field_length_m=field_length_m,
        field_width_m=field_width_m,
        ball_diameter_m=ball_diameter_m,
        valid=h_img2world is not None,
        fallback_ball_px=fallback_ball_px,
    )
