"""Render stage — broadcast/coach virtual camera over cylindrical projection.

Implements the control logic from ``docs/VIRTUAL_CAMERA.md`` (lead room
from velocity, asymmetric pan smoothing, zone-based zoom, dead-ball
overrides, broadcast vs coach modes) on top of the cylindrical renderer
in :mod:`video_grouper.inference.cylindrical_view`. The pipeline per
output frame is:

1. Read the smoothed Kalman state ``(x, y, vx, vy)`` for this frame from
   the trajectory; hold the last value when missing.
2. Project the ball pixel into normalized field coords via the field
   homography (when available; falls back to ``x / src_w``).
3. Classify the field zone (left_box / left_third / midfield /
   right_third / right_box) and look up the mode's zone-base zoom.
4. Add a speed-bias to the zoom (faster ball → wider view per the spec).
5. If the ball has been near-stationary for ``deadball_frame_count``
   frames, apply the mode's dead-ball zoom override for this zone.
6. Compute a lead-room offset = ``lead_factor * max_lead_room`` in the
   direction of motion; add to the pan target.
7. Asymmetric pan smoothing — pan EMA alpha lerps from
   ``pan_smoothing_min`` (slow ball) to ``pan_smoothing_max`` (fast).
8. Zoom EMA at ``zoom_smoothing`` (much smaller than pan alpha — zoom
   moves slower than pan per the spec).
9. Clamp the smoothed yaw to the field polygon's lateral extent (Phase 3).
10. Cylindrical remap with ``view_hfov_deg = smoothed_zoom * src_hfov``.

Heavy deps (``cv2``, ``av``) are imported lazily.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from video_grouper.ball_tracking.base import ProviderContext
from video_grouper.inference.cylindrical_view import (
    CylindricalViewParams,
    cylindrical_remap,
    pixel_to_yaw_pitch,
)
from video_grouper.inference.field_geometry import (
    field_lateral_yaw_extent,
    pixel_to_field,
)

from . import register_stage
from .base import ProcessingStage

logger = logging.getLogger(__name__)


# ---------- Camera modes (spec §"Camera Modes" parameter override table) ----------


@dataclass(frozen=True)
class CameraMode:
    """Tunables that change how aggressively the camera tracks the ball.

    All "zoom" values are crop-width fractions of the source horizontal
    FOV (per the spec). The cylindrical renderer multiplies by
    ``src_hfov_deg`` to get a view ``hfov`` in degrees.
    """

    # Field-zone boundaries (normalized to [0, 1] across field width)
    zone_box_boundary: float = 0.10
    zone_third_boundary: float = 0.33

    # Per-zone base zoom (crop width fraction)
    zoom_box: float = 0.25
    zoom_third: float = 0.35
    zoom_midfield: float = 0.45

    # Speed-derived bias on top of the zone base
    zoom_speed_bias_max: float = 0.10

    # Lead room (pan offset in direction of motion), as fraction of crop width
    max_lead_room_fraction: float = 0.20

    # Asymmetric pan smoothing — alpha from slow to fast
    pan_smoothing_min: float = 0.04
    pan_smoothing_max: float = 0.12
    zoom_smoothing: float = 0.03

    # Dead-ball overrides per zone (crop width fraction)
    deadball_box_zoom: float = 0.25
    deadball_third_zoom: float = 0.35
    deadball_midfield_zoom: float = 0.50

    # Stationary detection
    deadball_speed_threshold_px_per_frame: float = 4.0
    deadball_frame_count: int = 15

    # Speed normalization constant — speed at which lead/zoom-bias hit max
    max_expected_speed_px_per_frame: float = 100.0


BROADCAST_MODE = CameraMode()

COACH_MODE = CameraMode(
    zoom_box=0.40,
    zoom_third=0.50,
    zoom_midfield=0.55,
    zoom_speed_bias_max=0.05,
    max_lead_room_fraction=0.08,
    pan_smoothing_min=0.03,
    pan_smoothing_max=0.08,
    zoom_smoothing=0.02,
    deadball_box_zoom=0.50,
    deadball_third_zoom=0.55,
    deadball_midfield_zoom=0.55,
)


def _resolve_mode(name: str) -> CameraMode:
    if name == "coach":
        return COACH_MODE
    if name == "broadcast":
        return BROADCAST_MODE
    raise ValueError(f"render_mode must be 'broadcast' or 'coach', got {name!r}")


# ---------- Pure helpers (unit-tested) ----------


def _classify_zone(field_x: float, mode: CameraMode) -> str:
    """Map a normalized field-x position into one of five zones."""
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
    """Zone-specific zoom override applied when the ball is near-stationary."""
    if zone in ("left_box", "right_box"):
        return mode.deadball_box_zoom
    if zone in ("left_third", "right_third"):
        return mode.deadball_third_zoom
    return mode.deadball_midfield_zoom


def _ball_field_x(px: float, py: float, src_w: int, homography) -> float:
    """Project ball pixel to normalized field x; falls back to x/src_w."""
    if homography is not None:
        fx, _fy = pixel_to_field(px, py, homography)
        return max(0.0, min(1.0, fx))
    return max(0.0, min(1.0, px / src_w))


def _normalized_speed(vx: float, vy: float, max_expected: float) -> float:
    return max(0.0, min(1.0, math.hypot(vx, vy) / max_expected))


def _smooth_yaw(yaws: list[float | None], ema: float) -> list[float | None]:
    """EMA-smooth a yaw series, holding last value when missing.

    Kept as a legacy helper for tests; the per-frame loop now does
    asymmetric smoothing inline.
    """
    smoothed: list[float | None] = []
    last: float | None = None
    for y in yaws:
        if y is None:
            smoothed.append(last)
            continue
        if last is None:
            last = y
        else:
            last = last * ema + y * (1.0 - ema)
        smoothed.append(last)
    return smoothed


def _trajectory_to_yaws(
    trajectory, src_w: int, src_h: int, src_hfov_deg: float
) -> list[float | None]:
    """Project each ``(x, y)`` row into a yaw angle, preserving ``None`` gaps.

    Accepts both schemas: legacy ``[x, y]`` lists and the new
    ``{"x", "y", "vx", "vy"}`` dicts.
    """
    yaws: list[float | None] = []
    for entry in trajectory:
        if entry is None:
            yaws.append(None)
            continue
        if isinstance(entry, dict):
            px, py = entry["x"], entry["y"]
        else:
            px, py = entry[0], entry[1]
        yaw, _pitch = pixel_to_yaw_pitch(
            float(px), float(py), src_w, src_h, src_hfov_deg
        )
        yaws.append(yaw)
    return yaws


def _load_polygon(polygon_path: str | None):
    """Read field_polygon.json and return the polygon ndarray (or None)."""
    if not polygon_path:
        return None
    try:
        with open(polygon_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except FileNotFoundError:
        logger.warning("render: field_polygon_path %s not found", polygon_path)
        return None
    poly = payload.get("polygon")
    if poly is None:
        return None
    import numpy as np

    return np.array(poly, dtype=np.float32)


def _load_homography(polygon_path: str | None):
    """Read field_polygon.json and return the homography ndarray (or None)."""
    if not polygon_path:
        return None
    try:
        with open(polygon_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except FileNotFoundError:
        return None
    h = payload.get("homography")
    if h is None:
        return None
    import numpy as np

    return np.array(h, dtype=np.float32)


def _yaw_bounds(
    polygon, src_w: int, src_hfov_deg: float, padding_deg: float
) -> tuple[float, float]:
    """Field yaw extent ± padding, clamped to source's representable range."""
    yaw_min, yaw_max = field_lateral_yaw_extent(polygon, src_w, src_hfov_deg)
    yaw_min -= padding_deg
    yaw_max += padding_deg
    half_src = src_hfov_deg / 2.0
    return max(yaw_min, -half_src), min(yaw_max, half_src)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _trajectory_entry(entry) -> tuple[float, float, float, float] | None:
    """Normalize a trajectory.json entry into ``(x, y, vx, vy)`` or None."""
    if entry is None:
        return None
    if isinstance(entry, dict):
        return (
            float(entry["x"]),
            float(entry["y"]),
            float(entry.get("vx", 0.0)),
            float(entry.get("vy", 0.0)),
        )
    # Legacy [x, y] list
    return float(entry[0]), float(entry[1]), 0.0, 0.0


# ---------- Per-frame state machine ----------


@dataclass
class _CameraState:
    """Mutable state carried across the per-frame render loop."""

    smoothed_yaw: float | None = None
    smoothed_zoom: float | None = None
    last_target_zoom: float | None = None
    stationary_frames: int = 0


def _tick(
    state: _CameraState,
    entry: tuple[float, float, float, float] | None,
    src_w: int,
    src_h: int,
    src_hfov_deg: float,
    homography,
    mode: CameraMode,
    yaw_min: float,
    yaw_max: float,
) -> tuple[float, float]:
    """Advance the camera state by one frame.

    Returns the ``(yaw_deg, view_hfov_deg)`` to render this frame at.
    Updates ``state`` in place.
    """
    if entry is None:
        # Hold last frame's smoothed yaw + zoom; nothing to update.
        # Defaults if we've never had an entry yet.
        if state.smoothed_yaw is None:
            state.smoothed_yaw = _clamp(0.0, yaw_min, yaw_max)
        if state.smoothed_zoom is None:
            state.smoothed_zoom = mode.zoom_midfield
        view_hfov = state.smoothed_zoom * src_hfov_deg
        return state.smoothed_yaw, view_hfov

    px, py, vx, vy = entry
    speed = math.hypot(vx, vy)
    norm_speed = _normalized_speed(vx, vy, mode.max_expected_speed_px_per_frame)

    # Stationary tracking → dead-ball detection
    if speed < mode.deadball_speed_threshold_px_per_frame:
        state.stationary_frames += 1
    else:
        state.stationary_frames = 0
    is_dead_ball = state.stationary_frames >= mode.deadball_frame_count

    # Field-zone classification
    field_x = _ball_field_x(px, py, src_w, homography)
    zone = _classify_zone(field_x, mode)

    # Target zoom
    if is_dead_ball:
        target_zoom = _deadball_zone_zoom(zone, mode)
    else:
        target_zoom = (
            _zone_base_zoom(zone, mode) + norm_speed * mode.zoom_speed_bias_max
        )
    state.last_target_zoom = target_zoom

    # Pan target with lead-room offset
    yaw_raw, _pitch = pixel_to_yaw_pitch(px, py, src_w, src_h, src_hfov_deg)
    if speed > 1e-6:
        # Lead in pixel space → convert to yaw delta via the equirectangular
        # source mapping (yaw_per_pixel = src_hfov / src_w).
        crop_width_px = target_zoom * src_w
        max_lead_px = mode.max_lead_room_fraction * crop_width_px
        lead_px = (vx / speed) * (norm_speed * max_lead_px)
        yaw_lead_delta = lead_px * (src_hfov_deg / src_w)
        target_yaw = yaw_raw + yaw_lead_delta
    else:
        target_yaw = yaw_raw

    # Asymmetric pan smoothing
    pan_alpha = (
        mode.pan_smoothing_min
        + (mode.pan_smoothing_max - mode.pan_smoothing_min) * norm_speed
    )

    if state.smoothed_yaw is None:
        state.smoothed_yaw = target_yaw
    else:
        state.smoothed_yaw = state.smoothed_yaw + pan_alpha * (
            target_yaw - state.smoothed_yaw
        )
    if state.smoothed_zoom is None:
        state.smoothed_zoom = target_zoom
    else:
        state.smoothed_zoom = state.smoothed_zoom + mode.zoom_smoothing * (
            target_zoom - state.smoothed_zoom
        )

    state.smoothed_yaw = _clamp(state.smoothed_yaw, yaw_min, yaw_max)

    view_hfov = state.smoothed_zoom * src_hfov_deg
    return state.smoothed_yaw, view_hfov


# ---------- Render loop ----------


def _parse_bitrate(bitrate: str) -> int:
    """Parse '8M' / '500k' / '2000000' into bits/sec."""
    s = bitrate.strip().lower()
    if s.endswith("m"):
        return int(float(s[:-1]) * 1_000_000)
    if s.endswith("k"):
        return int(float(s[:-1]) * 1_000)
    return int(s)


def _render_video(
    input_path: str,
    output_path: str,
    trajectory_path: str,
    field_polygon_path: str | None,
    out_width: int,
    out_height: int,
    mode_name: str,
    src_hfov_deg: float,
    src_vfov_deg: float,
    view_vfov_deg: float,
    view_pitch_deg: float,
    yaw_padding_deg: float,
    video_bitrate: str,
) -> None:
    """Sync helper: per-frame cylindrical render with full control logic."""
    import av
    import cv2

    mode = _resolve_mode(mode_name)
    with open(trajectory_path, "r", encoding="utf-8") as f:
        raw_trajectory = json.load(f)

    polygon = _load_polygon(field_polygon_path)
    homography = _load_homography(field_polygon_path)

    with av.open(input_path) as in_container:
        in_video = in_container.streams.video[0]
        src_w = in_video.width
        src_h = in_video.height

        # Source audio (if present) is copied through to the output container.
        in_audio = None
        for s in in_container.streams:
            if s.type == "audio":
                in_audio = s
                break

        yaw_min, yaw_max = _yaw_bounds(polygon, src_w, src_hfov_deg, yaw_padding_deg)
        if polygon is not None:
            logger.info(
                "render(%s): yaw bounds [%.1f°, %.1f°] from field polygon "
                "(padding %.1f°)",
                mode_name,
                yaw_min,
                yaw_max,
                yaw_padding_deg,
            )
        else:
            logger.info("render(%s): no field polygon — yaw unconstrained", mode_name)

        state = _CameraState()

        with av.open(output_path, mode="w") as out_container:
            out_stream = out_container.add_stream("h264", rate=in_video.average_rate)
            out_stream.width = out_width
            out_stream.height = out_height
            out_stream.pix_fmt = "yuv420p"
            out_stream.bit_rate = _parse_bitrate(video_bitrate)

            out_audio = None
            if in_audio is not None:
                # Copy audio (no re-encode) when codec is mp4-friendly; transcode
                # to AAC otherwise. PyAV's add_stream(template=...) does an
                # in-place stream copy.
                try:
                    out_audio = out_container.add_stream(template=in_audio)
                    audio_passthrough = True
                    logger.info(
                        "render(%s): copying audio stream (codec=%s)",
                        mode_name,
                        in_audio.codec_context.codec.name,
                    )
                except Exception:
                    out_audio = out_container.add_stream("aac", rate=in_audio.rate)
                    audio_passthrough = False
                    logger.info(
                        "render(%s): transcoding audio to AAC (source codec=%s)",
                        mode_name,
                        in_audio.codec_context.codec.name,
                    )
            else:
                audio_passthrough = False
                logger.info("render(%s): no audio stream in source", mode_name)

            frame_idx = 0
            for packet in in_container.demux(
                (in_video, in_audio) if in_audio else (in_video,)
            ):
                if packet.dts is None:
                    continue

                if packet.stream is in_video:
                    for frame in packet.decode():
                        entry = (
                            _trajectory_entry(raw_trajectory[frame_idx])
                            if frame_idx < len(raw_trajectory)
                            else None
                        )
                        yaw, view_hfov = _tick(
                            state,
                            entry,
                            src_w,
                            src_h,
                            src_hfov_deg,
                            homography,
                            mode,
                            yaw_min,
                            yaw_max,
                        )

                        params = CylindricalViewParams(
                            src_w=src_w,
                            src_h=src_h,
                            src_hfov_deg=src_hfov_deg,
                            src_vfov_deg=src_vfov_deg,
                            out_w=out_width,
                            out_h=out_height,
                            view_hfov_deg=view_hfov,
                            view_vfov_deg=view_vfov_deg,
                            view_pitch_deg=view_pitch_deg,
                        )
                        map_x, map_y = cylindrical_remap(params, view_yaw_deg=yaw)

                        rgb = frame.to_ndarray(format="rgb24")
                        rendered = cv2.remap(
                            rgb,
                            map_x,
                            map_y,
                            interpolation=cv2.INTER_LINEAR,
                            borderMode=cv2.BORDER_CONSTANT,
                            borderValue=0,
                        )

                        new_frame = av.VideoFrame.from_ndarray(rendered, format="rgb24")
                        new_frame.pts = frame.pts
                        for v_packet in out_stream.encode(new_frame):
                            out_container.mux(v_packet)
                        frame_idx += 1

                elif in_audio is not None and packet.stream is in_audio:
                    if audio_passthrough:
                        # Reassign packet to the output audio stream so PyAV
                        # remuxes it into the output container untouched.
                        packet.stream = out_audio
                        out_container.mux(packet)
                    else:
                        for a_frame in packet.decode():
                            for a_packet in out_audio.encode(a_frame):
                                out_container.mux(a_packet)

            # Flush video encoder + audio (if transcoding)
            for v_packet in out_stream.encode():
                out_container.mux(v_packet)
            if in_audio is not None and not audio_passthrough and out_audio is not None:
                for a_packet in out_audio.encode():
                    out_container.mux(a_packet)


class RenderStage(ProcessingStage):
    name = "render"

    async def run(
        self, artifacts: dict[str, Any], ctx: ProviderContext
    ) -> dict[str, Any] | None:
        trajectory_path = artifacts.get("trajectory_path")
        if not trajectory_path:
            raise RuntimeError(
                "render: trajectory_path missing — was the track stage skipped?"
            )

        in_path = Path(artifacts["input_path"])
        out_path = Path(artifacts["output_path"])

        cfg = self.provider_config
        await asyncio.to_thread(
            _render_video,
            str(in_path),
            str(out_path),
            trajectory_path,
            artifacts.get("field_polygon_path"),
            cfg.render_output_width,
            cfg.render_output_height,
            cfg.render_mode,
            cfg.render_src_hfov_deg,
            cfg.render_src_vfov_deg,
            cfg.render_view_vfov_deg,
            cfg.render_pitch_deg,
            cfg.render_yaw_padding_deg,
            cfg.render_video_bitrate,
        )
        logger.info("render: wrote broadcast-style output to %s", out_path)
        return None  # output_path is already in artifacts


register_stage(RenderStage.name, RenderStage)
