"""Cylindrical view projection for the homegrown virtual broadcast camera.

The source from ``StitchCorrectStage`` is a stitched ~180° panoramic frame
that is still effectively fisheye-curved (the stitch pass reduces inter-camera
artifacts but does not de-fisheye). A flat 2D crop of that source curves
goal lines and stretches players near output-frame edges. This module
produces a remap grid that projects the source onto a virtual cylinder and
renders a perspective ("pinhole") view from inside it, parameterized by view
yaw / pitch / horizontal FOV. Straight lines stay straight at any pan.

Math (per output pixel ``(ox, oy)``):

1. Pinhole inverse — ray angles from view center::

       phi   = atan((ox - out_w/2) / focal_x)         # local horizontal angle
       theta = atan((oy - out_h/2) / focal_y)         # local vertical angle

   where ``focal_x = (out_w/2) / tan(view_hfov/2)`` and likewise for y.

2. World direction = view rotation applied to the pinhole ray. Yaw-only
   pan with optional fixed pitch (no roll)::

       yaw   = view_yaw + phi
       pitch = view_pitch + theta

3. Source sampling — equirectangular both axes (linear in angle)::

       src_x = (yaw   / src_hfov + 0.5) * src_w
       src_y = (pitch / src_vfov + 0.5) * src_h

   where ``src_vfov`` defaults to ``src_hfov * src_h / src_w`` (square pixel
   sampling at the optical center) but is configurable.

Because yaw enters ``src_x`` linearly, the *base* remap grid is computed
once at ``view_yaw=0`` and a constant pixel offset is added per render call.
The base grid is LRU-cached by view-geometry params, so per-frame remap-grid
construction is free at steady state.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np


@dataclass(frozen=True)
class CylindricalViewParams:
    """Geometry inputs that fully determine the remap base grid.

    ``src_vfov_deg`` and ``view_vfov_deg`` may be ``-1`` to mean
    "derive automatically": for ``src_vfov_deg`` that's
    ``src_hfov_deg * src_h / src_w`` (square pixels at the equator); for
    ``view_vfov_deg`` it's ``view_hfov_deg * out_h / out_w`` (square pixels
    at the output center).
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


def _resolved_src_vfov(p: CylindricalViewParams) -> float:
    if p.src_vfov_deg < 0:
        return p.src_hfov_deg * p.src_h / p.src_w
    return p.src_vfov_deg


def _resolved_view_vfov(p: CylindricalViewParams) -> float:
    if p.view_vfov_deg < 0:
        return p.view_hfov_deg * p.out_h / p.out_w
    return p.view_vfov_deg


@lru_cache(maxsize=8)
def _build_base_grid(p: CylindricalViewParams) -> tuple[np.ndarray, np.ndarray]:
    """Sampling grid into the source at ``view_yaw=0``.

    Returns ``(base_map_x, map_y)`` — float32 arrays of shape ``(out_h, out_w)``.
    Per-frame: add :func:`yaw_pixel_offset` to ``base_map_x``.
    """
    view_hfov_rad = np.deg2rad(p.view_hfov_deg)
    view_vfov_rad = np.deg2rad(_resolved_view_vfov(p))
    src_hfov_rad = np.deg2rad(p.src_hfov_deg)
    src_vfov_rad = np.deg2rad(_resolved_src_vfov(p))
    view_pitch_rad = np.deg2rad(p.view_pitch_deg)

    focal_x = (p.out_w / 2.0) / np.tan(view_hfov_rad / 2.0)
    focal_y = (p.out_h / 2.0) / np.tan(view_vfov_rad / 2.0)

    ox = np.arange(p.out_w, dtype=np.float64)
    oy = np.arange(p.out_h, dtype=np.float64)
    grid_x, grid_y = np.meshgrid(ox, oy)

    phi = np.arctan((grid_x - p.out_w / 2.0) / focal_x)
    theta = np.arctan((grid_y - p.out_h / 2.0) / focal_y)

    base_src_x = (phi / src_hfov_rad + 0.5) * p.src_w
    src_y = ((theta + view_pitch_rad) / src_vfov_rad + 0.5) * p.src_h

    return base_src_x.astype(np.float32), src_y.astype(np.float32)


def yaw_pixel_offset(p: CylindricalViewParams, view_yaw_deg: float) -> float:
    """Pixel offset to add to ``base_map_x`` for a given view yaw."""
    return float(view_yaw_deg / p.src_hfov_deg * p.src_w)


def cylindrical_remap(
    p: CylindricalViewParams, view_yaw_deg: float
) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(map_x, map_y)`` for ``cv2.remap`` at the given pan."""
    base_x, map_y = _build_base_grid(p)
    map_x = base_x + np.float32(yaw_pixel_offset(p, view_yaw_deg))
    return map_x, map_y


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
