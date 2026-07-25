"""Cylindrical view projection for the homegrown virtual broadcast camera.

The source from ``StitchCorrectStage`` is a stitched ~180° panoramic frame whose
pixel columns/rows are linear in azimuth/elevation about the *camera* axis. A flat
2D crop of that source curves goal lines and shears the field near the edges. This
module renders a perspective ("pinhole") view from inside the panorama via a proper
3D rotation, so straight lines stay straight at any pan.

Two corrections matter for matching a broadcast look:

1. **Real rotation, not separable angle addition.** Each output pixel's pinhole ray
   is rotated by the view orientation ``R = Ry(yaw)·Rx(pitch)`` and converted back to
   (azimuth, elevation) for sampling. The earlier ``yaw = view_yaw + phi`` shortcut is
   only correct dead-ahead; off-center it shears the horizon.

2. **Camera mount tilt.** The camera is mounted looking *down* at the field, so the
   panorama's axis is not world-level. Sampling it as if level rolls the horizon by an
   amount proportional to pan angle (a diagonal field that flips sign across the pan).
   ``mount_tilt_deg`` rotates the panorama back to world-up before orienting the view,
   so the field stays horizontal at every pan. ``view_pitch_offset_deg`` then nudges the
   framing (e.g. ball in the upper third) and ``view_roll_deg`` trims any residual roll.

Geometry (radians, right-handed with +x right, +y *down*, +z forward):

* pinhole ray ``d = normalize((ox-cx)/fx, (oy-cy)/fy, 1)``
* the look-at point ``(view_yaw, view_pitch)`` is a *source* (camera-frame) angle; it is
  lifted to world via ``Rx(-tilt)`` to get ``(yaw_w, pitch_w)``
* output rays are oriented in world: ``Ry(yaw_w)·Rx(pitch_w)·Rz(roll)·d``
* world rays are mapped back to camera via ``Rx(tilt)`` and sampled equirectangularly:
  ``src_x = (az/src_hfov + 0.5)·src_w``, ``src_y = (el/src_vfov + 0.5)·src_h``

The normalized pinhole ray grid (independent of yaw/pitch/tilt) is LRU-cached by output
geometry; per frame only cheap rotations + ``arctan2`` run, so there is no per-frame
remap-grid allocation beyond the rotation itself.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

try:  # Numba JITs the per-frame projection to native parallel code (exact, ~4x numpy).
    from numba import (  # type: ignore[import-not-found]  # optional accel dep, no stubs
        njit,
        prange,
    )

    _HAVE_NUMBA = True
except ImportError:  # pragma: no cover - numba is an optional acceleration dependency
    _HAVE_NUMBA = False


@dataclass(frozen=True)
class CylindricalViewParams:
    """Geometry inputs that fully determine the remap grid for one view.

    ``src_vfov_deg`` and ``view_vfov_deg`` may be ``-1`` to mean "derive
    automatically": ``src_hfov_deg * src_h / src_w`` (square pixels) and
    ``view_hfov_deg * out_h / out_w`` respectively.

    ``mount_tilt_deg`` is the camera's downward mount angle (levels the panorama
    to world-up). ``view_pitch_offset_deg`` shifts the framing vertically *after*
    leveling (positive = look down / subject higher in frame). ``view_roll_deg``
    trims any residual roll. All three default to 0 (identity).
    """

    src_w: int
    src_h: int
    src_hfov_deg: float
    out_w: int
    out_h: int
    view_hfov_deg: float
    src_vfov_deg: float = -1.0
    view_vfov_deg: float = -1.0
    view_pitch_deg: float = 0.0
    mount_tilt_deg: float = 0.0
    view_pitch_offset_deg: float = 0.0
    view_roll_deg: float = 0.0


def _resolved_src_vfov(p: CylindricalViewParams) -> float:
    if p.src_vfov_deg < 0:
        return p.src_hfov_deg * p.src_h / p.src_w
    return p.src_vfov_deg


def _resolved_view_vfov(p: CylindricalViewParams) -> float:
    if p.view_vfov_deg < 0:
        return p.view_hfov_deg * p.out_h / p.out_w
    return p.view_vfov_deg


@lru_cache(maxsize=8)
def _pinhole_rays(
    out_w: int, out_h: int, view_hfov_deg: float, view_vfov_deg: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Normalized output-pixel rays in the view frame (+x right, +y down, +z fwd).

    Independent of yaw/pitch/tilt, so cached by output geometry.
    """
    fx = (out_w / 2.0) / np.tan(np.deg2rad(view_hfov_deg) / 2.0)
    fy = (out_h / 2.0) / np.tan(np.deg2rad(view_vfov_deg) / 2.0)
    ox, oy = np.meshgrid(
        np.arange(out_w, dtype=np.float32), np.arange(out_h, dtype=np.float32)
    )
    # float32 is ample for sub-pixel remap maps and roughly halves the per-frame trig
    # cost (the arctan2/hypot in _project dominate map generation).
    x = (ox - out_w / 2.0) / fx
    y = (oy - out_h / 2.0) / fy
    z = np.ones_like(x)
    n = np.sqrt(x * x + y * y + z * z)
    return x / n, y / n, z / n


def _vec(az_rad: float, el_rad: float) -> tuple[float, float, float]:
    """Unit direction for azimuth (about +y) and elevation (+y down)."""
    ce, se = np.cos(el_rad), np.sin(el_rad)
    return ce * np.sin(az_rad), se, ce * np.cos(az_rad)


def _rx(a: float, x, y, z):
    """Rotate about the x-axis (right)."""
    c, s = np.cos(a), np.sin(a)
    return x, c * y - s * z, s * y + c * z


def _ry(a: float, x, y, z):
    """Rotate about the y-axis (vertical/down)."""
    c, s = np.cos(a), np.sin(a)
    return c * x + s * z, y, -s * x + c * z


def _project(
    p: CylindricalViewParams, view_yaw_deg: float, x, y, z
) -> tuple[np.ndarray, np.ndarray]:
    """Project pinhole rays ``(x, y, z)`` (view frame) → source ``(map_x, map_y)``.

    ``view_yaw_deg`` and ``p.view_pitch_deg`` are *source* (camera-frame) angles of
    the look-at point; the view is leveled to world via ``p.mount_tilt_deg``.
    """
    mt = np.deg2rad(p.mount_tilt_deg)
    # Look-at direction: source angle -> world (level) frame.
    bx, by, bz = _vec(np.deg2rad(view_yaw_deg), np.deg2rad(p.view_pitch_deg))
    bx, by, bz = _rx(-mt, bx, by, bz)
    yaw_w = np.arctan2(bx, bz)
    # +offset looks DOWN (subject higher in frame): world pitch has +y=down, and the
    # view-orientation Rx(pitch_w) maps +pitch_w to looking up, so subtract.
    pitch_w = np.arctan2(by, np.hypot(bx, bz)) - np.deg2rad(p.view_pitch_offset_deg)

    # Residual roll about the optical axis, then orient the view in world.
    if p.view_roll_deg:
        cr, sr = (
            np.cos(np.deg2rad(p.view_roll_deg)),
            np.sin(np.deg2rad(p.view_roll_deg)),
        )
        x, y = cr * x - sr * y, sr * x + cr * y
    x, y, z = _rx(pitch_w, x, y, z)
    x, y, z = _ry(yaw_w, x, y, z)
    # World -> camera frame, then sample the panorama equirectangularly.
    x, y, z = _rx(mt, x, y, z)

    az = np.rad2deg(np.arctan2(x, z))
    el = np.rad2deg(np.arctan2(y, np.hypot(x, z)))
    map_x = ((az / p.src_hfov_deg + 0.5) * p.src_w).astype(np.float32)
    map_y = ((el / _resolved_src_vfov(p) + 0.5) * p.src_h).astype(np.float32)
    return map_x, map_y


def _look_at_world(p: CylindricalViewParams, view_yaw_deg: float):
    """Scalar look-at orientation in the world (level) frame: ``(roll, pitch_w, yaw_w,
    mt)`` in radians. Shared by the numpy and Numba projection paths."""
    mt = math.radians(p.mount_tilt_deg)
    bx, by, bz = _vec(math.radians(view_yaw_deg), math.radians(p.view_pitch_deg))
    bx, by, bz = _rx(-mt, bx, by, bz)
    yaw_w = math.atan2(bx, bz)
    pitch_w = math.atan2(by, math.hypot(bx, bz)) - math.radians(p.view_pitch_offset_deg)
    return math.radians(p.view_roll_deg), pitch_w, yaw_w, mt


if _HAVE_NUMBA:

    @njit(parallel=True, fastmath=True, cache=True)
    def _project_numba(  # noqa: PLR0913 - tight numeric kernel, scalars by design
        x, y, z, roll, pitch_w, yaw_w, mt, src_hfov, src_vfov, src_w, src_h, mx, my
    ):
        """Fused, parallel equivalent of :func:`_project`'s per-pixel math. Each output
        ray is rolled, oriented to world (``Rx(pitch_w)·Ry(yaw_w)``), rotated back to the
        camera frame (``Rx(mt)``), then sampled equirectangularly. Matches the numpy path
        to float precision; no intermediate 2M-element allocations, so it scales across
        cores instead of streaming ten temporaries through memory."""
        cr = math.cos(roll)
        sr = math.sin(roll)
        cp = math.cos(pitch_w)
        sp = math.sin(pitch_w)
        cy = math.cos(yaw_w)
        sy = math.sin(yaw_w)
        cm = math.cos(mt)
        sm = math.sin(mt)
        rad2deg = 180.0 / math.pi
        h, w = x.shape
        for i in prange(h):
            for j in range(w):
                vx = x[i, j]
                vy = y[i, j]
                vz = z[i, j]
                if roll != 0.0:  # Rz(roll) about the optical axis
                    tx = cr * vx - sr * vy
                    vy = sr * vx + cr * vy
                    vx = tx
                ty = cp * vy - sp * vz  # Rx(pitch_w)
                vz = sp * vy + cp * vz
                vy = ty
                tx = cy * vx + sy * vz  # Ry(yaw_w)
                vz = -sy * vx + cy * vz
                vx = tx
                ty = cm * vy - sm * vz  # Rx(mt): world -> camera frame
                vz = sm * vy + cm * vz
                vy = ty
                az = math.atan2(vx, vz) * rad2deg
                el = math.atan2(vy, math.sqrt(vx * vx + vz * vz)) * rad2deg
                mx[i, j] = (az / src_hfov + 0.5) * src_w
                my[i, j] = (el / src_vfov + 0.5) * src_h


def cylindrical_remap(
    p: CylindricalViewParams, view_yaw_deg: float, downscale: int = 1
) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(map_x, map_y)`` for ``cv2.remap`` at the given pan.

    ``downscale > 1`` computes the maps on a ``1/downscale`` grid and bilinearly
    upsamples them to full output size. Because the remap is a very smooth warp, the
    error is sub-pixel for small factors (D=2 ≈ 1px in the 7680-wide source) while
    cutting the per-frame trig cost by ``downscale²`` — the map generation, not the
    pixel sampling, is the render bottleneck. ``_project`` does not mutate its ray
    inputs (every op allocates), so the cached grids are passed through directly.
    """
    if downscale > 1:
        import cv2  # local: keep cv2 out of the import path for non-render callers

        ow, oh = p.out_w // downscale, p.out_h // downscale
        x, y, z = _pinhole_rays(ow, oh, p.view_hfov_deg, _resolved_view_vfov(p))
        mx, my = _project(p, view_yaw_deg, x, y, z)
        dst = (p.out_w, p.out_h)
        return (
            cv2.resize(mx, dst, interpolation=cv2.INTER_LINEAR),
            cv2.resize(my, dst, interpolation=cv2.INTER_LINEAR),
        )
    x, y, z = _pinhole_rays(p.out_w, p.out_h, p.view_hfov_deg, _resolved_view_vfov(p))
    if _HAVE_NUMBA:
        roll, pitch_w, yaw_w, mt = _look_at_world(p, view_yaw_deg)
        mx = np.empty_like(x)
        my = np.empty_like(y)
        _project_numba(
            x,
            y,
            z,
            roll,
            pitch_w,
            yaw_w,
            mt,
            p.src_hfov_deg,
            _resolved_src_vfov(p),
            p.src_w,
            p.src_h,
            mx,
            my,
        )
        return mx, my
    return _project(p, view_yaw_deg, x, y, z)


def center_column_rows(p: CylindricalViewParams, view_yaw_deg: float) -> np.ndarray:
    """Source rows (``map_y``) sampled down the output centre column — cheap (one
    column), for vertical-framing / no-cap queries. A value ``< 0`` means that output
    row samples above the source top edge (black cap); ``> src_h`` means below the
    bottom edge.

    Builds ONLY the centre column's rays (``out_h`` points), not the full grid: this is
    called twice per frame by ``_solve_framing`` at a per-frame-varying hfov, so routing
    it through the LRU-cached full ``_pinhole_rays`` (a 2M-point grid) thrashed the cache
    and recomputed the whole grid every call — the dominant per-frame render cost. Same
    values as ``_pinhole_rays[:, out_w // 2]``.
    """
    vfov = _resolved_view_vfov(p)
    fx = (p.out_w / 2.0) / np.tan(np.deg2rad(p.view_hfov_deg) / 2.0)
    fy = (p.out_h / 2.0) / np.tan(np.deg2rad(vfov) / 2.0)
    xv = (p.out_w // 2 - p.out_w / 2.0) / fx  # centre column x (0 for even out_w)
    oy = np.arange(p.out_h, dtype=np.float32)
    y = (oy - p.out_h / 2.0) / fy
    z = np.ones_like(y)
    n = np.sqrt(xv * xv + y * y + z * z)
    x = np.full_like(y, xv)
    return _project(p, view_yaw_deg, x / n, y / n, z / n)[1]


def pixel_to_yaw_pitch(
    px: float,
    py: float,
    src_w: int,
    src_h: int,
    src_hfov_deg: float,
    src_vfov_deg: float = -1.0,
) -> tuple[float, float]:
    """Source pixel → ``(yaw_deg, pitch_deg)`` under equirectangular projection."""
    if src_vfov_deg < 0:
        src_vfov_deg = src_hfov_deg * src_h / src_w
    yaw = (px / src_w - 0.5) * src_hfov_deg
    pitch = (py / src_h - 0.5) * src_vfov_deg
    return yaw, pitch


def yaw_pitch_to_pixel(
    yaw_deg: float,
    pitch_deg: float,
    src_w: int,
    src_h: int,
    src_hfov_deg: float,
    src_vfov_deg: float = -1.0,
) -> tuple[float, float]:
    """Inverse of :func:`pixel_to_yaw_pitch`."""
    if src_vfov_deg < 0:
        src_vfov_deg = src_hfov_deg * src_h / src_w
    px = (yaw_deg / src_hfov_deg + 0.5) * src_w
    py = (pitch_deg / src_vfov_deg + 0.5) * src_h
    return px, py


# ---------------------------------------------------------------------------
# Polygon-derived world-up leveling
# ---------------------------------------------------------------------------
# The field mask outlines a world-horizontal plane. From it we recover the field-plane
# normal (= world-up, in camera coords) via two vanishing points — the touchlines and
# goal lines are each a family of parallel world lines — then level ANY view by rolling
# so world-vertical reads vertical. This is correct at every pan AND re-adapts to each
# game's camera placement (a different polygon yields a different world-up + tilt),
# unlike a fixed roll or a far-touchline-corner fit (which fails when a corner pans off
# screen, and levels the touchline — perpendicular to the goal crossbar — anyway).


def field_world_up(polygon, src_w: int, src_h: int, src_hfov_deg: float):
    """Field-plane normal (world-up) in camera coords from the field-mask polygon.

    Auto-groups the outline into far/near touchlines (top/bottom chains by y) and the
    left/right goal lines (end connectors), fits each line's great-circle plane normal,
    and crosses the two vanishing directions. Returns ``None`` for a degenerate polygon.
    """
    poly = np.asarray(polygon, dtype=float)
    if poly.ndim != 2 or poly.shape[0] < 4:
        return None

    def ray(px, py):
        ys, ps = pixel_to_yaw_pitch(px, py, src_w, src_h, src_hfov_deg)
        return np.array(_vec(np.deg2rad(ys), np.deg2rad(ps)))

    def normal(pts):
        return np.linalg.svd(np.array([ray(px, py) for px, py in pts]))[2][-1]

    ymed = np.median(poly[:, 1])
    far = poly[poly[:, 1] < ymed]
    near = poly[poly[:, 1] >= ymed]
    if len(far) < 2 or len(near) < 2:
        return None
    far = far[np.argsort(far[:, 0])]
    near = near[np.argsort(near[:, 0])]
    d_t = np.cross(normal(far), normal(near))  # touchline direction
    d_g = np.cross(
        normal([far[0], near[0]]),  # goal-line direction
        normal([far[-1], near[-1]]),
    )
    up = np.cross(d_t, d_g)
    n = float(np.linalg.norm(up))
    if n < 1e-9:
        return None
    up = up / n
    return -up if up[1] > 0 else up  # image-up is -y


def mount_tilt_from_up(up) -> float:
    """Camera down-pitch (deg) = the pitch component of world-up, derived from the
    field geometry instead of a hardcoded guess. Levels the bulk; the small residual
    (a mount roll) is handled per view by :func:`leveling_roll`."""
    return float(np.degrees(np.arctan2(-up[2], -up[1])))


def leveling_roll(
    view_yaw_deg: float,
    view_pitch_deg: float,
    view_hfov_deg: float,
    mount_tilt_deg: float,
    up,
    out_w: int,
    out_h: int,
) -> float:
    """View-roll (deg) making world-vertical read vertical for this view, given world-up
    ``up`` (field normal, camera coords). World-horizontal lines then read horizontal."""
    o = np.array(_vec(np.deg2rad(view_yaw_deg), np.deg2rad(view_pitch_deg)))
    mt = np.deg2rad(mount_tilt_deg)
    bx, by, bz = _rx(-mt, *o)
    yaw_w = np.arctan2(bx, bz)
    pitch_w = np.arctan2(by, np.hypot(bx, bz))
    fx = (out_w / 2) / np.tan(np.deg2rad(view_hfov_deg) / 2)
    fy = (out_h / 2) / np.tan(np.deg2rad(view_hfov_deg * out_h / out_w) / 2)

    def c2o(v):
        x, y, z = _rx(-mt, *v)
        x, y, z = _ry(-yaw_w, x, y, z)
        x, y, z = _rx(-pitch_w, x, y, z)
        return np.array([out_w / 2 + x / z * fx, out_h / 2 + y / z * fy])

    d = c2o(o + 0.05 * up) - c2o(o)
    return float(np.degrees(np.arctan2(d[0], -d[1])))


# ---------------------------------------------------------------------------
# Warp-once-crop: constant leveling panorama + per-frame crop
# ---------------------------------------------------------------------------
# The lens un-warp + world-up leveling is FIXED (the camera mount never moves), so it is
# computed ONCE as a world-leveled cylindrical panorama; each frame is then a cheap crop+
# zoom of it following the ball -- no per-frame projection trig, no per-frame remap grid.
# Output is cylindrical (vertical lines straight; far horizontals curve gently).


@dataclass(frozen=True)
class LeveledPano:
    """Constant world-up cylindrical leveling map (built once per camera placement).

    ``map_x``/``map_y`` resample the source into a panorama whose columns/rows are linear
    in world azimuth/elevation; :func:`warp_crop_maps` then crops a view window out of it.
    """

    map_x: np.ndarray
    map_y: np.ndarray
    az_lo: float
    az_hi: float
    el_lo: float
    el_hi: float
    r_cw: np.ndarray  # world -> camera rotation (cols = world axes in camera coords)
    src_vfov_deg: float


def _cam_from_world(world_up) -> np.ndarray:
    """Orthonormal world->camera rotation (cols = world axes in camera coords, +y down):
    ``e_y`` = world-down (=-world_up), ``e_z`` = camera-forward leveled, ``e_x`` completes
    a right-handed (+x right,+y down,+z fwd) frame."""
    w = np.asarray(world_up, float)
    ey = -w / np.linalg.norm(w)
    f0 = np.array([0.0, 0.0, 1.0])
    ez = f0 - np.dot(f0, ey) * ey
    ez /= np.linalg.norm(ez)
    ex = np.cross(ey, ez)
    ex /= np.linalg.norm(ex)
    ez = np.cross(ex, ey)
    return np.column_stack([ex, ey, ez])


def build_leveled_pano(
    world_up,
    polygon,
    src_w: int,
    src_h: int,
    src_hfov_deg: float,
    src_vfov_deg: float = -1.0,
    az_margin_deg: float = 2.0,
    el_margin_deg: float = 12.0,
    deg_per_px: float | None = None,
) -> LeveledPano:
    """Build the constant world-up cylindrical leveling map covering the FULL source.

    The pano spans the whole source footprint (its border under the leveling
    rotation), not just the field polygon: a view window cropped from the pano
    must always find real pixels (or the honest black cap above the source top)
    wherever the camera can legally aim — a pano that stops at the field edge
    forces :func:`crop_box` windows against its boundary during end-field pans,
    and the clipped crop resized to the output stretched the picture (the
    2026-07-10 "warps when it scrolls" defect: 10% of full-game frames width-
    truncated up to x3.1, 33% height-truncated). ``el_margin_deg`` keeps room
    above the source top for the bounded black cap framing.
    """
    if src_vfov_deg < 0:
        src_vfov_deg = src_hfov_deg * src_h / src_w
    r_cw = _cam_from_world(world_up)
    # Sample the SOURCE BORDER (plus the polygon, which lies inside it) through
    # the leveling rotation to get the pano's az/el footprint.
    border: list[tuple[float, float]] = []
    for t in np.linspace(0.0, 1.0, 65):
        border.append((t * src_w, 0.0))
        border.append((t * src_w, float(src_h)))
        border.append((0.0, t * src_h))
        border.append((float(src_w), t * src_h))
    pts = np.concatenate([np.asarray(border, float), np.asarray(polygon, float)])
    azs, els = [], []
    for px, py in pts:
        ya, pa = pixel_to_yaw_pitch(px, py, src_w, src_h, src_hfov_deg, src_vfov_deg)
        dw = r_cw.T @ np.array(_vec(np.deg2rad(ya), np.deg2rad(pa)))
        azs.append(np.degrees(np.arctan2(dw[0], dw[2])))
        els.append(np.degrees(np.arctan2(dw[1], np.hypot(dw[0], dw[2]))))
    az_lo, az_hi = min(azs) - az_margin_deg, max(azs) + az_margin_deg
    el_lo, el_hi = min(els) - el_margin_deg, max(els) + el_margin_deg
    if deg_per_px is None:
        deg_per_px = src_hfov_deg / src_w
    pw = max(2, int((az_hi - az_lo) / deg_per_px))
    ph = max(2, int((el_hi - el_lo) / deg_per_px))
    az = np.deg2rad(np.linspace(az_lo, az_hi, pw, dtype=np.float32))
    el = np.deg2rad(np.linspace(el_lo, el_hi, ph, dtype=np.float32))
    az_g, el_g = np.meshgrid(az, el)
    ce = np.cos(el_g)
    dw = np.stack([ce * np.sin(az_g), np.sin(el_g), ce * np.cos(az_g)], 0)
    dc = np.einsum("ij,jhw->ihw", r_cw, dw)
    x, y, z = dc[0], dc[1], dc[2]
    mx = ((np.rad2deg(np.arctan2(x, z)) / src_hfov_deg + 0.5) * src_w).astype(
        np.float32
    )
    my = (
        (np.rad2deg(np.arctan2(y, np.hypot(x, z))) / src_vfov_deg + 0.5) * src_h
    ).astype(np.float32)
    return LeveledPano(mx, my, az_lo, az_hi, el_lo, el_hi, r_cw, src_vfov_deg)


def _optical_axis_world(p: CylindricalViewParams, view_yaw_deg: float, r_cw):
    """View-centre world ``(az, el)`` in deg: the forward ray taken through the same
    rotations :func:`_project` applies, then into the world frame (correct sign, unlike the
    raw look-at pitch which :func:`render._solve_framing` compensates numerically)."""
    _roll, pitch_w, yaw_w, mt = _look_at_world(p, view_yaw_deg)
    x, y, z = 0.0, 0.0, 1.0
    cp, sp = math.cos(pitch_w), math.sin(pitch_w)
    y, z = cp * y - sp * z, sp * y + cp * z
    cy, sy = math.cos(yaw_w), math.sin(yaw_w)
    x, z = cy * x + sy * z, -sy * x + cy * z
    cm, sm = math.cos(mt), math.sin(mt)
    y, z = cm * y - sm * z, sm * y + cm * z
    dw = r_cw.T @ np.array([x, y, z])
    return math.degrees(math.atan2(dw[0], dw[2])), math.degrees(
        math.atan2(dw[1], math.hypot(dw[0], dw[2]))
    )


def crop_box(pano: LeveledPano, p: CylindricalViewParams, view_yaw_deg: float):
    """The view's window in the leveled panorama as integer ``(x0, y0, w, h)`` pano pixels.

    Centres on the optical axis and spans the view's hfov/vfov (cylindrical zoom = linear
    crop). A window that would cross the pano boundary is SHIFTED to fit, never
    truncated: truncation + the output resize stretched the picture during
    end-field pans (2026-07-10 defect) — shifting means the camera simply stops
    at the source edge, the broadcast behavior. Shared by the cv2
    (:func:`warp_crop_maps`) and OpenCL warp backends so they sample identical
    windows.
    """
    caz, cel = _optical_axis_world(p, view_yaw_deg, pano.r_cw)
    hf, vf = p.view_hfov_deg, _resolved_view_vfov(p)
    ph, pw = pano.map_x.shape
    # Shift-to-fit in angle space (degenerates to full-span when the window is
    # wider than the pano itself).
    half_az = min(hf / 2, (pano.az_hi - pano.az_lo) / 2)
    half_el = min(vf / 2, (pano.el_hi - pano.el_lo) / 2)
    caz = min(max(caz, pano.az_lo + half_az), pano.az_hi - half_az)
    cel = min(max(cel, pano.el_lo + half_el), pano.el_hi - half_el)

    def to_px(az_d, el_d):
        return (
            (az_d - pano.az_lo) / (pano.az_hi - pano.az_lo) * pw,
            (el_d - pano.el_lo) / (pano.el_hi - pano.el_lo) * ph,
        )

    x0, y0 = to_px(caz - half_az, cel - half_el)
    x1, y1 = to_px(caz + half_az, cel + half_el)
    x0i, x1i = max(int(round(min(x0, x1))), 0), min(int(round(max(x0, x1))), pw)
    y0i, y1i = max(int(round(min(y0, y1))), 0), min(int(round(max(y0, y1))), ph)
    return x0i, y0i, x1i - x0i, y1i - y0i


def warp_crop_maps(pano: LeveledPano, p: CylindricalViewParams, view_yaw_deg: float):
    """Per-frame ``(map_x, map_y)`` for ``cv2.remap`` via a crop+zoom of the constant
    leveled panorama -- no per-frame projection trig. The window centres on the view's
    optical axis and spans its hfov/vfov (cylindrical zoom is a linear crop)."""
    import cv2  # local: keep cv2 out of the import path for non-render callers

    x0, y0, w, h = crop_box(pano, p, view_yaw_deg)
    dst = (p.out_w, p.out_h)
    return (
        cv2.resize(
            pano.map_x[y0 : y0 + h, x0 : x0 + w], dst, interpolation=cv2.INTER_LINEAR
        ),
        cv2.resize(
            pano.map_y[y0 : y0 + h, x0 : x0 + w], dst, interpolation=cv2.INTER_LINEAR
        ),
    )
