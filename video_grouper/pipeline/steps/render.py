"""Render step — broadcast-quality virtual camera over a cylindrical projection.

Ports the ball-following control logic specified in ``docs/VIRTUAL_CAMERA.md``
(velocity lead-room, field-zone + speed zoom, asymmetric pan smoothing,
dead-ball and missing-ball overrides, broadcast vs coach modes) onto the
cylindrical renderer in :mod:`video_grouper.inference.cylindrical_view`. A flat
2D crop of the stitched ~180° panorama curves goal lines and stretches players
near the frame edge; the cylindrical render projects the source onto a virtual
cylinder and renders a perspective view from inside it, so straight lines stay
straight at every pan.

Vertical framing. The source is a single side-mounted camera, so far-side play
sits near the top edge. Two configurable strategies keep the ball in frame
without a hard vertical crop:

* ``render_vertical_tracking=True`` (default): the view pitch gently follows the
  ball's pitch (heavily smoothed, clamped so the view never samples past the
  source edge) — the natural broadcast feel.
* ``render_vertical_tracking=False``: the pitch is fixed at the field's vertical
  centre and the zoom is floored so the *whole* field height stays in view —
  "zoom out to always show the ball", pan horizontally only.

Either way a vertical-containment floor guarantees the ball's pitch stays inside
the rendered vertical FOV: the zoom can never go tighter than what keeps the
ball on screen.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

# NOTE: cylindrical_view is numpy-only and safe to import at module top, but
# field_geometry imports cv2 — keep it LAZY (inside the functions that use it)
# so this module still imports in the tray bundle (which excludes cv2/av); the
# step is gated out there at runtime by runtime="service" + requires=().
from video_grouper.inference.cylindrical_view import (
    CylindricalViewParams,
    build_leveled_pano,
    center_column_rows,
    crop_box,
    cylindrical_remap,
    field_world_up,
    leveling_roll,
    mount_tilt_from_up,
    pixel_to_yaw_pitch,
    warp_crop_maps,
    yaw_pitch_to_pixel,
)
from video_grouper.pipeline import register_step
from video_grouper.pipeline.base import PipelineStep, StepContext
from video_grouper.pipeline.frame_consumer import (
    FrameConsumer,
    FrameSourceInfo,
    register_frame_consumer,
)
from video_grouper.pipeline.manifest import PipelineManifest

logger = logging.getLogger(__name__)


class RenderStepConfig(BaseModel):
    render_mode: str = "broadcast"  # "broadcast" | "coach"
    # Gated + recency-windowed pan smoother (the experiment-tuned camera path). When False,
    # falls back to the legacy instantaneous-target EMA + velocity lead-room (frantic on
    # noisy/sparse tracks). See CameraMode.pan_* for the tunables.
    render_pan_smoothing: bool = True
    render_output_width: int = 1920
    render_output_height: int = 1080
    # Source optics. The stitched Reolink/Dahua panorama is ~180° horizontal.
    render_src_hfov_deg: float = 180.0
    # Projection. "cylindrical" (default): build the world-up leveling warp ONCE as a
    # constant panorama and crop a ball-following window per frame (cheap, no per-frame
    # reprojection; a constant cylindrical warp + ball-following crop). "pinhole": full
    # per-frame rectilinear reprojection (straightens lines, ~4x costlier). Both level the
    # field via the same polygon world-up; cylindrical needs that world-up (else falls back
    # to pinhole for the frame).
    render_projection: str = "cylindrical"
    # Warp backend. "cv2" (default): CPU remap -- universally portable, no extra deps.
    # "opencl": zero-copy pyopencl kernel (constant leveling map + crop box, computed on the
    # GPU) -- faster on integrated GPUs and frees the CPU for the (bottleneck) decode; needs
    # pyopencl + an OpenCL device and the cylindrical projection. Falls back to cv2 if the
    # OpenCL backend is unavailable.
    render_backend: str = "cv2"
    # Camera mount tilt: the down-angle of the camera relative to world-level. The
    # panorama's axis is tilted by this, so without correction the rendered horizon
    # rolls proportional to pan (a diagonal field). The cylindrical view levels the
    # panorama to world-up by this angle. Used only as a fallback when render_auto_level
    # is off or no field polygon is available; otherwise the tilt is DERIVED from the
    # field polygon per camera install. ~17-24° for the tripod-mounted Reolink Duo 3.
    render_mount_tilt_deg: float = 0.0
    # Residual roll trim (deg) about the optical axis. Fallback only — with auto-level
    # on, the per-frame leveling roll is derived from the field polygon instead.
    render_view_roll_deg: float = 0.0
    # Auto-leveling: when on AND a field polygon is available, derive the camera mount
    # tilt + per-frame leveling roll from the polygon's world-up (field-plane normal),
    # so world-horizontal lines (goal crossbars, field lines) read horizontal at every
    # pan and the geometry re-adapts to each camera placement. Falls back to the fixed
    # render_mount_tilt_deg / render_view_roll_deg when off or no polygon.
    render_auto_level: bool = True
    # Vertical framing offset applied after leveling (positive = look down / subject
    # higher in frame), for the broadcast "lead room below the ball" look.
    render_view_pitch_offset_deg: float = 0.0
    # Adaptive no-cap vertical framing (used when render_mount_tilt_deg != 0): aim to
    # put the ball at this fraction from the top, but clamp the view so it never
    # samples past the source edge (no black cap), zooming in only if the field is
    # taller than the source allows.
    render_target_ball_frac: float = 0.45
    render_cap_margin_deg: float = 1.5
    # Allowed black top-cap (deg of source above the top edge). 0 = strict no-cap
    # (the view never samples past the source top). A positive value lets the camera
    # aim up into a bounded cap rather than dumping foreground when the ball is far
    # upfield — matching a side-mounted broadcast camera that keeps a small sky cap.
    render_top_cap_deg: float = 8.0
    # Reject ball detections OUTSIDE the field polygon (treat the frame as missing → the
    # camera holds its bearing and widens instead of lunging off-field). Off-field
    # detections are false positives (sideline / foreground clusters) that would otherwise
    # steer the camera onto empty space. No-op when no polygon is available.
    render_mask_offfield: bool = True
    # Distance+velocity zoom curve (the calibrated broadcast zoom): far/slow → tight,
    # near OR fast → wide. hfov = base + min(speed/norm,1)·speed_gain + depth·depth_gain,
    # clamped. ``depth`` is the ball's field depth (0 far touchline → 1 near), ``speed``
    # the ball's source px/frame. Used when render_auto_zoom is on (else the zone+speed
    # zoom). Degrees are absolute view HFOV; defaults match the Reolink Duo 3 calibration.
    render_auto_zoom: bool = True
    render_zoom_base_deg: float = 47.0
    render_zoom_min_deg: float = 46.0
    render_zoom_max_deg: float = 58.0
    render_zoom_speed_norm_px: float = 15.0
    render_zoom_speed_gain_deg: float = 8.0
    render_zoom_depth_gain_deg: float = 5.0
    # Uniform widening of the final view HFOV (and derived VFOV). The distance/velocity
    # curve above is calibrated for pinhole framing; broadcast framing runs ~1.25x wider,
    # so the cylindrical default opens the view to match. Kept as a multiplier so the curve's
    # dynamics are preserved.
    render_zoom_scale: float = 1.25
    # Vertical framing.
    render_vertical_tracking: bool = True
    render_view_pitch_deg: float = 0.0  # fallback field-centre pitch (no polygon)
    render_field_half_pitch_deg: float = 19.0  # fallback field half-height in pitch
    render_vertical_margin_deg: float = 6.0  # headroom kept above/below the ball
    # Pan clamp padding beyond the field's lateral extent.
    render_yaw_padding_deg: float = 8.0
    render_video_bitrate: str = "8M"


# ---------------------------------------------------------------------------
# Camera modes (parameter overrides) — tuned for broadcast framing.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CameraMode:
    """Tunables controlling how aggressively the camera tracks the ball.

    Zoom values are crop-width fractions of the source horizontal FOV; the
    renderer multiplies by ``src_hfov_deg`` to get the view's HFOV in degrees.
    """

    zone_box_boundary: float = 0.10
    zone_third_boundary: float = 0.33

    zoom_box: float = 0.13
    zoom_third: float = 0.18
    zoom_midfield: float = 0.22
    zoom_speed_bias_max: float = 0.06

    max_lead_room_fraction: float = 0.20

    pan_smoothing_min: float = 0.04
    pan_smoothing_max: float = 0.12
    zoom_smoothing: float = 0.03
    pitch_smoothing: float = 0.05  # vertical follow — slower than pan

    deadball_box_zoom: float = 0.13
    deadball_third_zoom: float = 0.20
    deadball_midfield_zoom: float = 0.28

    deadball_speed_threshold_px_per_frame: float = 4.0
    deadball_frame_count: int = 15
    max_expected_speed_px_per_frame: float = 100.0

    missing_ball_short_frames: int = 15
    missing_ball_medium_frames: int = 60
    missing_ball_long_zoom: float = 0.30

    velocity_ema: float = 0.3  # EMA on per-frame finite-difference velocity

    # --- Gated + recency-windowed pan smoother (active when render_pan_smoothing=True) ---
    # Reject detection teleports (gate), aim at a recency-weighted average of recent accepted
    # yaws over a short window, and ease there with a low-gain EMA whose gain rises with recent
    # spread. Tuned on real game footage to broadcast-quality steadiness (per-frame pan ~5 px
    # median vs the legacy path's frequent >100 px lunges).
    pan_gate_px: float = (
        700.0  # reject a detection jumping farther than this (source px)
    )
    pan_reacq_frames: int = (
        15  # frames lost before a far detection is accepted (reacquire)
    )
    pan_window_sec: float = 3.0  # recency-weighted averaging window (seconds)
    pan_inertia: float = 0.985  # base camera EMA; alpha = 1 - this (higher = smoother)
    pan_vf_gain: float = 1.0  # extra responsiveness from recent-detection spread
    pan_vf_smooth: float = 0.75  # smoothing of the adaptive (spread) term
    pan_vf_norm_deg: float = 9.375  # yaw spread (deg) mapping to vf=1 (~400 source px)


BROADCAST_MODE = CameraMode()

COACH_MODE = CameraMode(
    zoom_box=0.22,
    zoom_third=0.28,
    zoom_midfield=0.32,
    zoom_speed_bias_max=0.04,
    max_lead_room_fraction=0.08,
    pan_smoothing_min=0.03,
    pan_smoothing_max=0.08,
    zoom_smoothing=0.02,
    pitch_smoothing=0.03,
    deadball_box_zoom=0.30,
    deadball_third_zoom=0.32,
    deadball_midfield_zoom=0.35,
    missing_ball_long_zoom=0.40,
)


def _resolve_mode(name: str) -> CameraMode:
    if name == "coach":
        return COACH_MODE
    if name == "broadcast":
        return BROADCAST_MODE
    raise ValueError(f"render_mode must be 'broadcast' or 'coach', got {name!r}")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _classify_zone(field_x: float, mode: CameraMode) -> str:
    if field_x < mode.zone_box_boundary:
        return "left_box"
    if field_x < mode.zone_third_boundary:
        return "left_third"
    if field_x < 1.0 - mode.zone_third_boundary:
        return "midfield"
    if field_x < 1.0 - mode.zone_box_boundary:
        return "right_third"
    return "right_box"


def _zone_base_zoom(zone: str, mode: CameraMode) -> float:
    return {
        "left_box": mode.zoom_box,
        "right_box": mode.zoom_box,
        "left_third": mode.zoom_third,
        "right_third": mode.zoom_third,
        "midfield": mode.zoom_midfield,
    }[zone]


def _deadball_zone_zoom(zone: str, mode: CameraMode) -> float:
    if zone in ("left_box", "right_box"):
        return mode.deadball_box_zoom
    if zone in ("left_third", "right_third"):
        return mode.deadball_third_zoom
    return mode.deadball_midfield_zoom


def _normalized_speed(vx: float, vy: float, max_expected: float) -> float:
    return max(0.0, min(1.0, math.hypot(vx, vy) / max_expected))


def _trajectory_xy(entry) -> tuple[float, float] | None:
    """Normalize a trajectory.json row (``[x, y]`` or ``{"x","y"}``) to a pair."""
    if entry is None:
        return None
    if isinstance(entry, dict):
        return float(entry["x"]), float(entry["y"])
    return float(entry[0]), float(entry[1])


def compute_entries(
    trajectory: list, velocity_ema: float
) -> list[tuple[float, float, float, float] | None]:
    """Turn a per-frame ``[x, y]`` trajectory into ``(x, y, vx, vy)`` rows.

    Velocity is an EMA of the finite difference across populated frames, so the
    lead-room / speed-zoom logic has motion even though the tracker only stores
    positions. Gaps (``None``) are preserved; velocity carries across a gap.
    """
    out: list[tuple[float, float, float, float] | None] = []
    vx = vy = 0.0
    prev: tuple[float, float] | None = None
    for entry in trajectory:
        xy = _trajectory_xy(entry)
        if xy is None:
            out.append(None)
            continue
        if prev is not None:
            vx = velocity_ema * (xy[0] - prev[0]) + (1 - velocity_ema) * vx
            vy = velocity_ema * (xy[1] - prev[1]) + (1 - velocity_ema) * vy
        out.append((xy[0], xy[1], vx, vy))
        prev = xy
    return out


@dataclass
class _ViewGeom:
    """Resolved vertical geometry for the source + field."""

    src_hfov_deg: float
    src_vfov_deg: float
    aspect: float  # out_w / out_h
    base_pitch_deg: float  # field vertical centre
    field_half_pitch_deg: float  # field half-height, in pitch degrees
    pitch_limit_deg: float  # |view_pitch| max before the view samples off-source
    mount_tilt_deg: float = 0.0  # camera down-pitch (polygon-derived or cfg fallback)
    world_up: object = None  # field-plane normal (cam coords) for leveling, or None
    leveled_pano: object = (
        None  # constant world-up cylindrical map (cylindrical projection)
    )
    polygon: object = None  # field polygon (source px); gates off-field ball detections


def _resolve_geometry(
    src_w: int,
    src_h: int,
    cfg: RenderStepConfig,
    polygon,
) -> _ViewGeom:
    src_vfov = cfg.render_src_hfov_deg * src_h / src_w
    aspect = cfg.render_output_width / cfg.render_output_height
    if polygon is not None and len(polygon) > 0:
        ys = polygon[:, 1]
        _, pitch_top = pixel_to_yaw_pitch(
            0.0, float(ys.min()), src_w, src_h, cfg.render_src_hfov_deg
        )
        _, pitch_bot = pixel_to_yaw_pitch(
            0.0, float(ys.max()), src_w, src_h, cfg.render_src_hfov_deg
        )
        base_pitch = (pitch_top + pitch_bot) / 2.0
        field_half = abs(pitch_bot - pitch_top) / 2.0
    else:
        base_pitch = cfg.render_view_pitch_deg
        field_half = cfg.render_field_half_pitch_deg
    # Polygon-derived world-up: levels the field at every pan, derives the mount tilt.
    world_up = None
    mount_tilt = cfg.render_mount_tilt_deg
    leveled_pano = None
    if cfg.render_auto_level and polygon is not None and len(polygon) >= 4:
        world_up = field_world_up(polygon, src_w, src_h, cfg.render_src_hfov_deg)
        if world_up is not None:
            mount_tilt = mount_tilt_from_up(world_up)
            logger.info(
                "render: polygon world-up leveling on — derived mount_tilt=%.2f° "
                "(world_up=%s)",
                mount_tilt,
                tuple(round(float(v), 3) for v in world_up),
            )
            if cfg.render_projection == "cylindrical":
                # Build the constant world-up leveling panorama ONCE; per-frame views are
                # cheap crops of it (no per-frame reprojection).
                leveled_pano = build_leveled_pano(
                    world_up, polygon, src_w, src_h, cfg.render_src_hfov_deg, src_vfov
                )
                logger.info(
                    "render: cylindrical projection — leveled pano %dx%d built once",
                    leveled_pano.map_x.shape[1],
                    leveled_pano.map_x.shape[0],
                )
    return _ViewGeom(
        src_hfov_deg=cfg.render_src_hfov_deg,
        src_vfov_deg=src_vfov,
        aspect=aspect,
        base_pitch_deg=base_pitch,
        field_half_pitch_deg=field_half,
        pitch_limit_deg=src_vfov / 2.0,
        mount_tilt_deg=mount_tilt,
        world_up=world_up,
        leveled_pano=leveled_pano,
        polygon=polygon,
    )


def _project_maps(geom: _ViewGeom, cfg: RenderStepConfig, params, view_yaw):
    """Per-frame ``(map_x, map_y)`` for ``cv2.remap``: a cheap crop of the constant leveled
    panorama (cylindrical) or the full per-frame pinhole reprojection. Falls back to pinhole
    when the leveled pano is unavailable (no polygon world-up)."""
    if cfg.render_projection == "cylindrical" and geom.leveled_pano is not None:
        return warp_crop_maps(geom.leveled_pano, params, view_yaw)
    return cylindrical_remap(params, view_yaw_deg=view_yaw)


def _make_warper(geom: _ViewGeom, cfg: RenderStepConfig, src_w, src_h, out_w, out_h):
    """An :class:`OpenCLWarper` when ``render_backend='opencl'`` is requested AND usable
    (pyopencl + an OpenCL device + the cylindrical leveled pano), else ``None`` (cv2 path)."""
    if (
        cfg.render_backend != "opencl"
        or cfg.render_projection != "cylindrical"
        or geom.leveled_pano is None
    ):
        return None
    try:
        from video_grouper.inference.opencl_warp import OpenCLWarper

        if not OpenCLWarper.available():
            logger.info(
                "render: OpenCL backend requested but no OpenCL device — using cv2"
            )
            return None
        warper = OpenCLWarper(geom.leveled_pano, src_w, src_h, out_w, out_h)
        logger.info("render: OpenCL zero-copy warp backend active")
        return warper
    except (
        Exception
    ) as exc:  # pragma: no cover - driver/build failure → safe cv2 fallback
        logger.warning("render: OpenCL warper unavailable (%s) — using cv2", exc)
        return None


def _warp_frame(rgb, geom: _ViewGeom, cfg: RenderStepConfig, params, view_yaw, warper):
    """Render one frame: the GPU OpenCL warp (constant pano + crop box) when ``warper`` is
    present, else the cv2 remap path. Both return an ``out_h x out_w x 3`` uint8 image."""
    if warper is not None:
        return warper.warp(rgb, crop_box(geom.leveled_pano, params, view_yaw))
    import cv2

    map_x, map_y = _project_maps(geom, cfg, params, view_yaw)
    return cv2.remap(
        rgb,
        map_x,
        map_y,
        interpolation=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )


def _solve_framing(
    src_w: int,
    src_h: int,
    geom: _ViewGeom,
    cfg: RenderStepConfig,
    yaw_s: float,
    pitch_s: float,
    ball_row: float,
    base_hfov: float,
) -> tuple[float, float]:
    """Mount-tilt-aware vertical framing → ``(view_pitch_offset_deg, hfov)``.

    Solves the down-offset (and zooms in only if needed) so the view never samples
    past the source edge — no black cap — while putting the ball near
    ``cfg.render_target_ball_frac``. Uses the projection's near-linear response in the
    offset (two centre-column samples → slope) so it costs a handful of cheap queries
    per frame. ``base_hfov`` is returned unchanged unless a zoom-in is required to fit
    the field within the source vertically.
    """
    out_w, out_h = cfg.render_output_width, cfg.render_output_height
    mt = geom.mount_tilt_deg
    m = cfg.render_cap_margin_deg / geom.src_vfov_deg * src_h
    # Top constraint target: with a permitted top cap, the top output row may sample
    # up to `cap_rows` ABOVE the source top (negative source row = black cap), so the
    # camera can aim up to frame a far ball instead of dumping foreground.
    cap_rows = cfg.render_top_cap_deg / geom.src_vfov_deg * src_h
    top_target = -cap_rows if cap_rows > 0 else m
    bi = int(cfg.render_target_ball_frac * out_h)
    hfov = base_hfov
    lo = hi = 0.0
    for _ in range(6):
        p0 = CylindricalViewParams(
            src_w,
            src_h,
            geom.src_hfov_deg,
            out_w,
            out_h,
            round(hfov, 1),
            -1.0,
            -1.0,
            round(pitch_s, 1),
            mt,
            0.0,
            0.0,
        )
        p1 = CylindricalViewParams(
            src_w,
            src_h,
            geom.src_hfov_deg,
            out_w,
            out_h,
            round(hfov, 1),
            -1.0,
            -1.0,
            round(pitch_s, 1),
            mt,
            10.0,
            0.0,
        )
        c0 = center_column_rows(p0, round(yaw_s, 1))
        c1 = center_column_rows(p1, round(yaw_s, 1))

        def solve(idx, target):
            slope = (c1[idx] - c0[idx]) / 10.0
            return (target - c0[idx]) / slope if abs(slope) > 1e-6 else 0.0

        p_notop = solve(0, top_target)
        p_nobot = solve(out_h - 1, src_h - m)
        lo, hi = min(p_notop, p_nobot), max(p_notop, p_nobot)
        if hi >= lo:
            return float(min(max(solve(bi, ball_row), lo), hi)), hfov
        hfov *= 0.9
    return float((lo + hi) / 2), hfov


# ---------------------------------------------------------------------------
# Per-frame state machine
# ---------------------------------------------------------------------------


@dataclass
class _CameraState:
    smoothed_yaw: float | None = None
    smoothed_pitch: float | None = None
    smoothed_zoom: float | None = None
    stationary_frames: int = 0
    missing_frames: int = 0
    # Video frame rate — set by the render loop so the pan window (seconds) maps to frames.
    fps: float = 20.0
    # Gated + recency-windowed pan smoother state (render_pan_smoothing).
    pan_frame: int = 0
    pan_last: tuple[float, float] | None = (
        None  # last accepted ball (px, py) for gating
    )
    pan_lost: int = 0
    pan_vf: float = 0.0
    pan_buf: list = field(default_factory=list)  # (frame_idx, yaw_raw) recent accepted


def _tick(
    state: _CameraState,
    entry: tuple[float, float, float, float] | None,
    src_w: int,
    src_h: int,
    geom: _ViewGeom,
    mode: CameraMode,
    cfg: RenderStepConfig,
    yaw_min: float,
    yaw_max: float,
    homography,
) -> tuple[float, float, float]:
    """Advance the camera by one frame → ``(yaw_deg, pitch_deg, view_hfov_deg)``."""
    margin = cfg.render_vertical_margin_deg

    def containment_zoom(ball_pitch: float, view_pitch: float) -> float:
        """Min zoom (hfov fraction) keeping the ball's pitch inside the V-FOV."""
        need_vfov = 2.0 * (abs(ball_pitch - view_pitch) + margin)
        return (need_vfov * geom.aspect) / geom.src_hfov_deg

    # Off-field detection rejection: a ball outside the field polygon is a detector false
    # positive (sideline / foreground). Treat it as missing so the camera holds + widens
    # rather than lunging off-field — the source of the worst edge wedges. The polygon is
    # already a render input via the field-mark, even though we defer auto-detecting it.
    if (
        entry is not None
        and cfg.render_mask_offfield
        and geom.polygon is not None
        and not _point_in_polygon(entry[0], entry[1], geom.polygon)
    ):
        entry = None

    if entry is None:
        state.missing_frames += 1
        if state.smoothed_yaw is None:
            state.smoothed_yaw = _clamp(0.0, yaw_min, yaw_max)
        if state.smoothed_pitch is None:
            state.smoothed_pitch = geom.base_pitch_deg
        if state.smoothed_zoom is None:
            state.smoothed_zoom = mode.zoom_midfield
        if state.missing_frames >= mode.missing_ball_medium_frames:
            # Long gap: HOLD the last pan bearing (where the ball was last seen)
            # and widen to a stable wide shot. Do NOT recentre the pan to
            # mid-field — that drifts the camera off the action onto empty
            # grass, the single worst artefact of a sparse trajectory. Pitch
            # eases back to the field centre for a steady wide hold; yaw is
            # held so the action stays roughly framed until the ball reappears.
            state.smoothed_zoom += mode.zoom_smoothing * (
                mode.missing_ball_long_zoom - state.smoothed_zoom
            )
            state.smoothed_pitch += mode.zoom_smoothing * (
                geom.base_pitch_deg - state.smoothed_pitch
            )
            # yaw intentionally held — no drift to centre.
        elif state.missing_frames >= mode.missing_ball_short_frames:
            # Medium gap: zoom out toward midfield, hold pan/pitch.
            state.smoothed_zoom += mode.zoom_smoothing * (
                mode.zoom_midfield - state.smoothed_zoom
            )
        return (
            state.smoothed_yaw,
            state.smoothed_pitch,
            state.smoothed_zoom * geom.src_hfov_deg,
        )

    state.missing_frames = 0
    px, py, vx, vy = entry
    speed = math.hypot(vx, vy)
    norm_speed = _normalized_speed(vx, vy, mode.max_expected_speed_px_per_frame)

    if speed < mode.deadball_speed_threshold_px_per_frame:
        state.stationary_frames += 1
    else:
        state.stationary_frames = 0
    is_dead_ball = state.stationary_frames >= mode.deadball_frame_count

    yaw_raw, ball_pitch = pixel_to_yaw_pitch(px, py, src_w, src_h, geom.src_hfov_deg)

    # ---- vertical: track the ball's pitch (clamped) or hold field centre ----
    if cfg.render_vertical_tracking:
        if state.smoothed_pitch is None:
            state.smoothed_pitch = ball_pitch
        else:
            state.smoothed_pitch += mode.pitch_smoothing * (
                ball_pitch - state.smoothed_pitch
            )
    else:
        state.smoothed_pitch = geom.base_pitch_deg
    view_pitch = state.smoothed_pitch

    # ---- zoom: zone + speed, then floor for vertical containment ----
    if is_dead_ball:
        field_x = _ball_field_x(px, py, src_w, homography)
        target_zoom = _deadball_zone_zoom(_classify_zone(field_x, mode), mode)
    elif cfg.render_auto_zoom:
        # Distance+velocity curve: far/slow → tight, near OR fast → wide.
        half = geom.field_half_pitch_deg or 1.0
        depth = _clamp(
            (ball_pitch - (geom.base_pitch_deg - half)) / (2.0 * half), 0.0, 1.0
        )
        spd = min(speed / cfg.render_zoom_speed_norm_px, 1.0)
        hfov = _clamp(
            cfg.render_zoom_base_deg
            + spd * cfg.render_zoom_speed_gain_deg
            + depth * cfg.render_zoom_depth_gain_deg,
            cfg.render_zoom_min_deg,
            cfg.render_zoom_max_deg,
        )
        target_zoom = hfov / geom.src_hfov_deg
    else:
        field_x = _ball_field_x(px, py, src_w, homography)
        target_zoom = (
            _zone_base_zoom(_classify_zone(field_x, mode), mode)
            + norm_speed * mode.zoom_speed_bias_max
        )
    if cfg.render_vertical_tracking:
        # Floor around the (tracked) view pitch — keeps the ball in frame if the
        # smoothed pitch lags the ball.
        target_zoom = max(target_zoom, containment_zoom(ball_pitch, view_pitch))
    else:
        # No tilt: floor so the whole field height stays in view, AND so the
        # ball itself stays in view even if it leaves the field vertically
        # (a high kick) — "zoom out to always show the ball".
        whole_field = (
            2.0 * (geom.field_half_pitch_deg + margin) * geom.aspect
        ) / geom.src_hfov_deg
        target_zoom = max(
            target_zoom, whole_field, containment_zoom(ball_pitch, view_pitch)
        )

    # ---- pan ----
    if cfg.render_pan_smoothing:
        # Gated + recency-windowed pan (ported from the experiment camera tuner). Reject
        # detection teleports (gate), aim at a recency-weighted average of recent accepted
        # yaws over a short window, and ease there with a low-gain EMA whose gain rises with
        # recent spread. This holds steady on noisy / sparse tracks instead of chasing the
        # instantaneous ball yaw every frame — the fix for "frantic" pan.
        state.pan_frame += 1
        accept = (
            state.pan_last is None
            or state.pan_lost > mode.pan_reacq_frames
            or math.hypot(px - state.pan_last[0], py - state.pan_last[1])
            < mode.pan_gate_px
        )
        if accept:
            state.pan_last = (px, py)
            state.pan_lost = 0
            state.pan_buf.append((state.pan_frame, yaw_raw))
        else:
            state.pan_lost += 1
        cutoff = state.pan_frame - max(1, int(mode.pan_window_sec * state.fps))
        while state.pan_buf and state.pan_buf[0][0] <= cutoff:
            state.pan_buf.pop(0)
        if state.pan_buf:
            kmin = state.pan_buf[0][0]
            wsum = ysum = 0.0
            for kf, yw in state.pan_buf:
                w = kf - kmin + 1.0
                wsum += w
                ysum += w * yw
            target_yaw = ysum / wsum
            if len(state.pan_buf) > 1:
                mean = sum(yw for _, yw in state.pan_buf) / len(state.pan_buf)
                spread = (
                    sum((yw - mean) ** 2 for _, yw in state.pan_buf)
                    / len(state.pan_buf)
                ) ** 0.5
            else:
                spread = 0.0
            state.pan_vf = mode.pan_vf_smooth * state.pan_vf + (
                1 - mode.pan_vf_smooth
            ) * min(1.0, spread / mode.pan_vf_norm_deg)
            alpha = (1 - mode.pan_inertia) * (1 + state.pan_vf * mode.pan_vf_gain)
            state.smoothed_yaw = (
                target_yaw
                if state.smoothed_yaw is None
                else state.smoothed_yaw + alpha * (target_yaw - state.smoothed_yaw)
            )
        elif state.smoothed_yaw is None:
            state.smoothed_yaw = yaw_raw
    else:
        # Legacy: chase the instantaneous ball yaw (+ velocity lead room) with a
        # speed-adaptive EMA. Frantic on noisy / sparse trajectories.
        if speed > 1e-6:
            crop_width_px = target_zoom * src_w
            max_lead_px = mode.max_lead_room_fraction * crop_width_px
            lead_px = (vx / speed) * (norm_speed * max_lead_px)
            target_yaw = yaw_raw + lead_px * (geom.src_hfov_deg / src_w)
        else:
            target_yaw = yaw_raw
        pan_alpha = (
            mode.pan_smoothing_min
            + (mode.pan_smoothing_max - mode.pan_smoothing_min) * norm_speed
        )
        state.smoothed_yaw = (
            target_yaw
            if state.smoothed_yaw is None
            else state.smoothed_yaw + pan_alpha * (target_yaw - state.smoothed_yaw)
        )
    state.smoothed_zoom = (
        target_zoom
        if state.smoothed_zoom is None
        else state.smoothed_zoom
        + mode.zoom_smoothing * (target_zoom - state.smoothed_zoom)
    )

    state.smoothed_yaw = _clamp(state.smoothed_yaw, yaw_min, yaw_max)
    # Clamp pitch so the view's vertical extent never samples past the source.
    view_hfov = state.smoothed_zoom * geom.src_hfov_deg
    view_vfov = view_hfov * (cfg.render_output_height / cfg.render_output_width)
    pitch_room = max(0.0, geom.pitch_limit_deg - view_vfov / 2.0)
    state.smoothed_pitch = _clamp(state.smoothed_pitch, -pitch_room, pitch_room)

    return state.smoothed_yaw, state.smoothed_pitch, view_hfov


def _point_in_polygon(px: float, py: float, polygon) -> bool:
    """Ray-casting point-in-polygon test; ``polygon`` is an (N,2) array of source pixels."""
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = float(polygon[i][0]), float(polygon[i][1])
        xj, yj = float(polygon[j][0]), float(polygon[j][1])
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _ball_field_x(px: float, py: float, src_w: int, homography) -> float:
    if homography is not None:
        from video_grouper.inference.field_geometry import pixel_to_field

        fx, _fy = pixel_to_field(px, py, homography)
        return max(0.0, min(1.0, fx))
    return max(0.0, min(1.0, px / src_w))


# ---------------------------------------------------------------------------
# Field polygon loading
# ---------------------------------------------------------------------------


def _load_field(polygon_path: str | None):
    """Return ``(polygon ndarray | None, homography ndarray | None)``."""
    if not polygon_path:
        return None, None
    try:
        with open(polygon_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning("render: field polygon %s unusable (%s)", polygon_path, e)
        return None, None
    import numpy as np

    poly = payload.get("polygon")
    polygon = np.array(poly, dtype=np.float32) if poly is not None else None
    h = payload.get("homography")
    homography = np.array(h, dtype=np.float32) if h is not None else None
    if homography is None and "keypoints" in payload:
        from video_grouper.inference.field_geometry import field_homography

        homography = field_homography(payload["keypoints"])
    return polygon, homography


def _parse_bitrate(bitrate: str) -> int:
    s = bitrate.strip().lower()
    if s.endswith("m"):
        return int(float(s[:-1]) * 1_000_000)
    if s.endswith("k"):
        return int(float(s[:-1]) * 1_000)
    return int(s)


def _frame_view(
    state,
    entry,
    geom,
    mode,
    cfg,
    yaw_min,
    yaw_max,
    homography,
    src_w,
    src_h,
    out_w,
    out_h,
) -> tuple[CylindricalViewParams, float]:
    """One frame's camera solve → ``(CylindricalViewParams, view_yaw_deg)``.

    Shared by the single-output and multi-output render loops so they stay identical:
    ball-following pan/pitch/zoom (``_tick``), then polygon world-up leveling + cap-aware
    vertical framing (``_solve_framing``) when auto-level is on.
    """
    yaw, pitch, view_hfov = _tick(
        state, entry, src_w, src_h, geom, mode, cfg, yaw_min, yaw_max, homography
    )
    if geom.world_up is not None:
        ball_row = yaw_pitch_to_pixel(yaw, pitch, src_w, src_h, geom.src_hfov_deg)[1]
        view_pitch_offset, view_hfov = _solve_framing(
            src_w, src_h, geom, cfg, yaw, pitch, ball_row, view_hfov
        )
        view_roll = leveling_roll(
            yaw, pitch, view_hfov, geom.mount_tilt_deg, geom.world_up, out_w, out_h
        )
    else:
        view_pitch_offset = cfg.render_view_pitch_offset_deg
        view_roll = cfg.render_view_roll_deg
    params = CylindricalViewParams(
        src_w=src_w,
        src_h=src_h,
        src_hfov_deg=geom.src_hfov_deg,
        out_w=out_w,
        out_h=out_h,
        view_hfov_deg=round(view_hfov * cfg.render_zoom_scale, 1),
        src_vfov_deg=-1.0,
        view_vfov_deg=-1.0,
        view_pitch_deg=round(pitch, 1),
        mount_tilt_deg=geom.mount_tilt_deg,
        view_pitch_offset_deg=round(view_pitch_offset, 2),
        view_roll_deg=round(view_roll, 2),
    )
    return params, round(yaw, 1)


# ---------------------------------------------------------------------------
# Render loop
# ---------------------------------------------------------------------------


def _render_video(
    input_path: str,
    output_path: str,
    trajectory_path: str,
    field_polygon_path: str | None,
    cfg: RenderStepConfig,
) -> None:
    """Sync helper: per-frame cylindrical render with the broadcast control logic."""
    import av

    from video_grouper.inference.field_geometry import field_lateral_yaw_extent

    mode = _resolve_mode(cfg.render_mode)
    out_w = cfg.render_output_width
    out_h = cfg.render_output_height

    with open(trajectory_path, "r", encoding="utf-8") as f:
        raw_trajectory = json.load(f)
    entries = compute_entries(raw_trajectory, mode.velocity_ema)

    polygon, homography = _load_field(field_polygon_path)

    with av.open(input_path) as in_container:
        in_video = in_container.streams.video[0]
        src_w = in_video.width
        src_h = in_video.height

        geom = _resolve_geometry(src_w, src_h, cfg, polygon)
        yaw_min, yaw_max = field_lateral_yaw_extent(polygon, src_w, geom.src_hfov_deg)
        yaw_min -= cfg.render_yaw_padding_deg
        yaw_max += cfg.render_yaw_padding_deg
        half_src = geom.src_hfov_deg / 2.0
        yaw_min = max(yaw_min, -half_src)
        yaw_max = min(yaw_max, half_src)
        logger.info(
            "render(%s): %dx%d src, yaw [%.0f,%.0f]°, base_pitch %.1f°, "
            "field_half %.1f°, vertical_tracking=%s",
            cfg.render_mode,
            src_w,
            src_h,
            yaw_min,
            yaw_max,
            geom.base_pitch_deg,
            geom.field_half_pitch_deg,
            cfg.render_vertical_tracking,
        )

        in_audio = next((s for s in in_container.streams if s.type == "audio"), None)

        state = _CameraState()
        state.fps = float(in_video.average_rate) if in_video.average_rate else 20.0
        warper = _make_warper(geom, cfg, src_w, src_h, out_w, out_h)

        with av.open(output_path, mode="w") as out_container:
            out_stream = out_container.add_stream("h264", rate=in_video.average_rate)
            out_stream.width = out_w
            out_stream.height = out_h
            out_stream.pix_fmt = "yuv420p"
            # Match source time_base — PyAV's default mis-budgets the bitrate
            # (output ~13× too large) and corrupts duration metadata.
            out_stream.codec_context.time_base = in_video.time_base
            out_stream.codec_context.bit_rate = _parse_bitrate(cfg.render_video_bitrate)
            out_stream.options = {
                "maxrate": cfg.render_video_bitrate,
                "bufsize": str(_parse_bitrate(cfg.render_video_bitrate) * 2),
            }

            out_audio = None
            audio_passthrough = False
            if in_audio is not None:
                try:
                    out_audio = out_container.add_stream(template=in_audio)
                    audio_passthrough = True
                except Exception:
                    out_audio = out_container.add_stream("aac", rate=in_audio.rate)
                    audio_passthrough = False

            frame_idx = 0
            demux_streams = (in_video, in_audio) if in_audio else (in_video,)
            for packet in in_container.demux(demux_streams):
                if packet.dts is None:
                    continue
                if packet.stream is in_video:
                    for frame in packet.decode():
                        entry = entries[frame_idx] if frame_idx < len(entries) else None
                        params, view_yaw = _frame_view(
                            state,
                            entry,
                            geom,
                            mode,
                            cfg,
                            yaw_min,
                            yaw_max,
                            homography,
                            src_w,
                            src_h,
                            out_w,
                            out_h,
                        )
                        rgb = frame.to_ndarray(format="rgb24")
                        rendered = _warp_frame(rgb, geom, cfg, params, view_yaw, warper)
                        new_frame = av.VideoFrame.from_ndarray(rendered, format="rgb24")
                        new_frame.pts = frame.pts
                        for v_packet in out_stream.encode(new_frame):
                            out_container.mux(v_packet)
                        frame_idx += 1
                elif in_audio is not None and packet.stream is in_audio:
                    if audio_passthrough:
                        packet.stream = out_audio
                        out_container.mux(packet)
                    else:
                        for a_frame in packet.decode():
                            for a_packet in out_audio.encode(a_frame):
                                out_container.mux(a_packet)

            for v_packet in out_stream.encode():
                out_container.mux(v_packet)
            if in_audio is not None and not audio_passthrough and out_audio is not None:
                for a_packet in out_audio.encode():
                    out_container.mux(a_packet)
        if warper is not None:
            warper.close()


# ---------------------------------------------------------------------------
# Render as a fan-out frame consumer (shares one decode across N camera paths)
# ---------------------------------------------------------------------------


class RenderConsumerConfig(RenderStepConfig):
    """A render variant inside a ``frame_fanout``: which trajectory to follow and where to
    write. Inherits all render tuning so each variant can differ (here all three share it)."""

    trajectory_key: str  # manifest key holding this variant's per-frame trajectory
    output_key: str  # manifest key to record this variant's output path under
    output_name: str  # output filename, written under ctx.group_dir


class RenderFrameConsumer(FrameConsumer):
    """Renders the broadcast view for one trajectory, encoding to its own output, fed
    decoded frames by the fan-out step (so the source is decoded once for all variants)."""

    config_model = RenderConsumerConfig

    @property
    def consumes(self) -> tuple[str, ...]:
        return (self.config.trajectory_key,)

    @property
    def produces(self) -> tuple[str, ...]:
        return (self.config.output_key,)

    def open(self, source: FrameSourceInfo, ctx: StepContext, manifest) -> None:
        import av

        cfg = self.config
        self._mode = _resolve_mode(cfg.render_mode)
        self._ow, self._oh = cfg.render_output_width, cfg.render_output_height
        self._sw, self._sh = source.width, source.height
        with open(manifest.get(cfg.trajectory_key), "r", encoding="utf-8") as f:
            self._entries = compute_entries(json.load(f), self._mode.velocity_ema)
        polygon, self._homography = _load_field(manifest.get("field_polygon_path"))
        self._geom = _resolve_geometry(source.width, source.height, cfg, polygon)
        from video_grouper.inference.field_geometry import field_lateral_yaw_extent

        ymin, ymax = field_lateral_yaw_extent(
            polygon, source.width, self._geom.src_hfov_deg
        )
        half = self._geom.src_hfov_deg / 2.0
        self._yaw_min = max(ymin - cfg.render_yaw_padding_deg, -half)
        self._yaw_max = min(ymax + cfg.render_yaw_padding_deg, half)
        self._state = _CameraState()
        self._state.fps = float(source.average_rate) if source.average_rate else 20.0
        self._out_path = ctx.group_dir / cfg.output_name
        self._oc = av.open(str(self._out_path), mode="w")
        st = self._oc.add_stream("h264", rate=source.average_rate)
        st.width, st.height, st.pix_fmt = self._ow, self._oh, "yuv420p"
        st.codec_context.time_base = source.time_base
        br = _parse_bitrate(cfg.render_video_bitrate)
        st.codec_context.bit_rate = br
        st.options = {"maxrate": cfg.render_video_bitrate, "bufsize": str(br * 2)}
        self._stream = st
        self._warper = _make_warper(
            self._geom, cfg, source.width, source.height, self._ow, self._oh
        )

    def consume(self, rgb, frame_pts: int, frame_idx: int) -> None:
        import av

        entry = self._entries[frame_idx] if frame_idx < len(self._entries) else None
        params, view_yaw = _frame_view(
            self._state,
            entry,
            self._geom,
            self._mode,
            self.config,
            self._yaw_min,
            self._yaw_max,
            self._homography,
            self._sw,
            self._sh,
            self._ow,
            self._oh,
        )
        rendered = _warp_frame(
            rgb, self._geom, self.config, params, view_yaw, self._warper
        )
        new_frame = av.VideoFrame.from_ndarray(rendered, format="rgb24")
        new_frame.pts = frame_pts
        for v_packet in self._stream.encode(new_frame):
            self._oc.mux(v_packet)

    def close(self, manifest) -> None:
        for v_packet in self._stream.encode():
            self._oc.mux(v_packet)
        self._oc.close()
        if getattr(self, "_warper", None) is not None:
            self._warper.close()
        manifest.put(self.config.output_key, str(self._out_path))


register_frame_consumer("render", RenderFrameConsumer, RenderConsumerConfig)


class RenderStep(PipelineStep):
    name = "render"
    config_model = RenderStepConfig
    consumes = ("input_path", "trajectory_path")
    produces = ("output_path",)
    runtime = "service"
    requires = ("av", "cv2")
    resources = ("ram_heavy",)

    async def run(self, manifest: PipelineManifest, ctx: StepContext) -> bool:
        in_path = Path(manifest.get("input_path"))
        out_path = Path(manifest.get("output_path"))
        trajectory_path = manifest.get("trajectory_path")
        # Optional: a field-detect step upstream supplies the ROI polygon used
        # for pan clamping + vertical geometry. Absent ⇒ unconstrained pan.
        field_polygon_path = manifest.get("field_polygon_path")

        await asyncio.to_thread(
            _render_video,
            str(in_path),
            str(out_path),
            trajectory_path,
            field_polygon_path,
            self.config,
        )
        logger.info("render: wrote broadcast-style output to %s", out_path)
        return True


register_step(RenderStep.name, RenderStep, RenderStepConfig)
