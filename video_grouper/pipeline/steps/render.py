"""Render step — execute the planner's camera path over a cylindrical projection.

The DUMB half of the dumb-renderer split (2026-07-10, single homegrown path):
ALL camera intelligence — pan, zoom, lead room, dead-ball behavior — lives
upstream in the ``plan_camera`` step; this step EXECUTES the ``camera_path/1``
command stream ``{center_px, hfov_deg}`` per frame and enforces ONLY projection
feasibility: yaw clamped to the field's lateral extent, pitch clamped to the
source's vertical FOV, cap-aware vertical framing (``_solve_framing``), and
polygon world-up leveling. A flat 2D crop of the stitched ~180° panorama curves
goal lines and stretches players near the frame edge; the cylindrical render
projects the source onto a virtual cylinder and renders a perspective view from
inside it, so straight lines stay straight at every pan.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel

if TYPE_CHECKING:
    # Type-only: av is imported lazily inside functions (see NOTE below) so the
    # module stays importable in the tray bundle, which excludes av.
    from av.audio.frame import AudioFrame
    from av.audio.stream import AudioStream
    from av.video.frame import VideoFrame

# NOTE: cylindrical_view is numpy-only and safe to import at module top, but
# field_geometry imports cv2 — keep it LAZY (inside the functions that use it)
# so this module still imports in the tray bundle (which excludes cv2/av); the
# step is gated out there at runtime by runtime="service" + requires=().
from video_grouper.inference.cylindrical_view import (
    CylindricalViewParams,
    LeveledPano,
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

# Per-frame viewport log. One JSON line per rendered frame, in Once AutoCam's
# format ({"xy": [cx, cy], "f": frame, "t": seconds}) so our broadcast camera
# aim can be diffed against AutoCam's the same way. ``xy`` is the source-pixel
# the centre of the output frame maps back to. On its own child logger so an
# operator can route or silence the per-frame volume without touching the
# step's normal logging.
viewport_logger = logging.getLogger(__name__ + ".viewport")


class RenderStepConfig(BaseModel):
    render_output_width: int = 1920
    render_output_height: int = 1080
    # Source optics. The stitched Reolink/Dahua panorama is ~180° horizontal.
    render_src_hfov_deg: float = 180.0
    # Projection. "cylindrical" (default): build the world-up leveling warp ONCE as a
    # constant panorama and crop a command-following window per frame (cheap, no per-frame
    # reprojection). "pinhole": full per-frame rectilinear reprojection (straightens
    # lines, ~4x costlier). Both level the field via the same polygon world-up;
    # cylindrical needs that world-up (else falls back to pinhole for the frame).
    render_projection: str = "cylindrical"
    # Warp backend. "cv2" (default): CPU remap -- universally portable, no extra deps.
    # "opencl": zero-copy pyopencl kernel (constant leveling map + crop box, computed on the
    # GPU) -- faster on integrated GPUs and frees the CPU for the (bottleneck) decode; needs
    # pyopencl + an OpenCL device and the cylindrical projection. Falls back to cv2 if the
    # OpenCL backend is unavailable.
    render_backend: str = "cv2"
    # Camera mount tilt fallback when render_auto_level is off or no field polygon is
    # available; otherwise the tilt is DERIVED from the field polygon per install.
    render_mount_tilt_deg: float = 0.0
    # Residual roll trim (deg) about the optical axis. Fallback only — with auto-level
    # on, the per-frame leveling roll is derived from the field polygon instead.
    render_view_roll_deg: float = 0.0
    # Auto-leveling: when on AND a field polygon is available, derive the camera mount
    # tilt + per-frame leveling roll from the polygon's world-up (field-plane normal),
    # so world-horizontal lines read horizontal at every pan.
    render_auto_level: bool = True
    # Vertical framing offset applied after leveling (positive = look down / subject
    # higher in frame). Fallback when auto-level is off.
    render_view_pitch_offset_deg: float = 0.0
    # Cap-aware vertical framing: aim to put the command's aim-point at this fraction
    # from the top, clamped so the view never samples past the source edge.
    render_target_ball_frac: float = 0.58
    render_cap_margin_deg: float = 1.5
    # Allowed black top-cap (deg of source above the top edge). 0 = strict no-cap.
    # A positive value lets the camera aim up into a bounded cap rather than dumping
    # foreground when the ball is far upfield — matching a side-mounted broadcast
    # camera that keeps a small sky cap (AutoCam's own renders show the same cap).
    render_top_cap_deg: float = 8.0
    # Manifest key of the camera_path/1 artifact (from the plan_camera step). The
    # command stream is REQUIRED — a configured key with no artifact in the manifest
    # is a hard error, never a silent fallback.
    render_camera_path_key: str = "camera_path_path"
    # Uniform scale on the final view HFOV. The planner PRE-APPLIES this calibrated
    # scale to its commands, so the command's hfov is divided by it before the
    # params-stage multiply (round-trip: the planner's hfov is final).
    render_zoom_scale: float = 0.90
    # Geometry fallbacks when no field polygon is available.
    render_view_pitch_deg: float = 0.0  # fallback field-centre pitch
    render_field_half_pitch_deg: float = 19.0  # fallback field half-height in pitch
    # Pan clamp padding beyond the field's lateral extent.
    render_yaw_padding_deg: float = 8.0
    render_video_bitrate: str = "8M"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


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
    # constant world-up cylindrical map (cylindrical projection), or None
    leveled_pano: LeveledPano | None = None
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
        # A warper is only built when leveled_pano is non-None (see _make_warper).
        assert geom.leveled_pano is not None
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

        def solve(idx, target, c0=c0, c1=c1):
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
# Field polygon loading
# ---------------------------------------------------------------------------


def _load_field(polygon_path: str | None):
    """Return the field polygon ndarray, or ``None`` when absent/unusable."""
    if not polygon_path:
        return None
    try:
        with open(polygon_path, encoding="utf-8") as f:
            payload = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning("render: field polygon %s unusable (%s)", polygon_path, e)
        return None
    import numpy as np

    poly = payload.get("polygon")
    return np.array(poly, dtype=np.float32) if poly is not None else None


def _polygon_or_full_frame(polygon, src_w: int, src_h: int):
    """The render geometry requires a polygon; absent one, the field IS the frame.

    A full-frame rectangle is the neutral default: centred base pitch, full
    vertical extent, full lateral pan range, derived mount tilt ≈ 0 (its
    world-up is straight up), and the off-field rejection keeps everything —
    one code path whether or not an upstream field_detect found a real field.
    """
    if polygon is not None and len(polygon) > 0:
        return polygon
    import numpy as np

    logger.info(
        "render: no field polygon supplied; defaulting to the full %dx%d frame",
        src_w,
        src_h,
    )
    return np.array(
        [[0.0, 0.0], [src_w, 0.0], [src_w, src_h], [0.0, src_h]], dtype=np.float32
    )


def _parse_bitrate(bitrate: str) -> int:
    s = bitrate.strip().lower()
    if s.endswith("m"):
        return int(float(s[:-1]) * 1_000_000)
    if s.endswith("k"):
        return int(float(s[:-1]) * 1_000)
    return int(s)


def _frame_view(
    command: tuple[float, float, float],
    geom,
    cfg,
    yaw_min,
    yaw_max,
    src_w,
    src_h,
    out_w,
    out_h,
) -> tuple[CylindricalViewParams, float]:
    """One frame's camera solve → ``(CylindricalViewParams, view_yaw_deg)``.

    ``command`` = a camera_path/1 row ``(center_x_px, center_y_px, hfov_deg)`` from
    the upstream planner. The renderer executes the command and enforces ONLY
    projection feasibility: yaw/pitch clamps, cap-aware vertical framing
    (``_solve_framing``), and polygon world-up leveling. The command's hfov is
    final: the planner pre-applies the calibrated zoom scale, so it is divided
    back out of the params-stage multiply.
    """
    cx, cy, cmd_hfov = command
    yaw, pitch = pixel_to_yaw_pitch(
        float(cx), float(cy), src_w, src_h, geom.src_hfov_deg
    )
    yaw = max(yaw_min, min(yaw_max, yaw))
    lim = float(getattr(geom, "pitch_limit_deg", 90.0))
    pitch = max(-lim, min(lim, pitch))
    view_hfov = float(cmd_hfov) / max(cfg.render_zoom_scale, 1e-6)
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


def _load_commands(camera_path_file: str) -> tuple[list, int]:
    """Load a camera_path/1 artifact -> (command rows, g_start)."""
    with open(camera_path_file, encoding="utf-8") as f:
        art = json.load(f)
    commands = art["frames"]
    if not commands:
        raise RuntimeError(f"render: camera path {camera_path_file} has no commands")
    return commands, int(art.get("g_start", 0))


def _command_for(commands: list, g0: int, frame_idx: int) -> tuple[float, float, float]:
    """The command for a source frame; frames outside the planned span hold the
    nearest end command (a steady wide hold beats inventing camera motion)."""
    i = min(max(frame_idx - g0, 0), len(commands) - 1)
    c = commands[i]
    return (float(c[0]), float(c[1]), float(c[2]))


# ---------------------------------------------------------------------------
# Render loop
# ---------------------------------------------------------------------------


def _render_video(
    input_path: str,
    output_path: str,
    camera_path_file: str,
    field_polygon_path: str | None,
    cfg: RenderStepConfig,
) -> None:
    """Sync helper: execute the planner's per-frame commands over the cylindrical
    projection (feasibility clamps only) and encode the broadcast output."""
    import av

    from video_grouper.inference.field_geometry import field_lateral_yaw_extent

    out_w = cfg.render_output_width
    out_h = cfg.render_output_height
    commands, cmd_g0 = _load_commands(camera_path_file)
    polygon = _load_field(field_polygon_path)

    with av.open(input_path) as in_container:
        in_video = in_container.streams.video[0]
        src_w = in_video.width
        src_h = in_video.height

        polygon = _polygon_or_full_frame(polygon, src_w, src_h)
        geom = _resolve_geometry(src_w, src_h, cfg, polygon)
        yaw_min, yaw_max = field_lateral_yaw_extent(polygon, src_w, geom.src_hfov_deg)
        yaw_min -= cfg.render_yaw_padding_deg
        yaw_max += cfg.render_yaw_padding_deg
        half_src = geom.src_hfov_deg / 2.0
        yaw_min = max(yaw_min, -half_src)
        yaw_max = min(yaw_max, half_src)
        fps = float(in_video.average_rate) if in_video.average_rate else 20.0
        logger.info(
            "render: %dx%d src, %d camera commands (g_start %d), yaw [%.0f,%.0f]°",
            src_w,
            src_h,
            len(commands),
            cmd_g0,
            yaw_min,
            yaw_max,
        )

        # The s.type == "audio" filter guarantees an AudioStream; av types the
        # generic streams iterator as the base Stream, so narrow it for the
        # audio-specific attributes used below (.rate, template add_stream).
        in_audio = cast(
            "AudioStream | None",
            next((s for s in in_container.streams if s.type == "audio"), None),
        )

        warper = _make_warper(geom, cfg, src_w, src_h, out_w, out_h)

        with av.open(output_path, mode="w") as out_container:
            out_stream = out_container.add_stream("h264", rate=in_video.average_rate)
            out_stream.width = out_w
            out_stream.height = out_h
            out_stream.pix_fmt = "yuv420p"
            # Match source time_base — PyAV's default mis-budgets the bitrate
            # (output ~13× too large) and corrupts duration metadata. A decoded
            # video stream always carries a time_base (av types it Optional).
            assert in_video.time_base is not None
            out_stream.codec_context.time_base = in_video.time_base
            out_stream.codec_context.bit_rate = _parse_bitrate(cfg.render_video_bitrate)
            out_stream.options = {
                "maxrate": cfg.render_video_bitrate,
                "bufsize": str(_parse_bitrate(cfg.render_video_bitrate) * 2),
            }

            out_audio: AudioStream | None = None
            audio_passthrough = False
            if in_audio is not None:
                try:
                    # add_stream(template=...) is a real (deprecated) PyAV API that
                    # routes to add_stream_from_template, but av's stubs only type
                    # add_stream with a required positional codec_name — so the
                    # keyword-only template form matches no overload. Stub gap.
                    out_audio = out_container.add_stream(template=in_audio)  # type: ignore[call-overload]
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
                    # Mixed-stream demux yields generic Packet[Stream], so decode()
                    # returns the frame-type union; this branch only sees the video
                    # stream's packets, so the frames are VideoFrames.
                    for frame in cast("list[VideoFrame]", packet.decode()):
                        params, view_yaw = _frame_view(
                            _command_for(commands, cmd_g0, frame_idx),
                            geom,
                            cfg,
                            yaw_min,
                            yaw_max,
                            src_w,
                            src_h,
                            out_w,
                            out_h,
                        )
                        # AutoCam-format per-frame viewport line: where the
                        # centre of this output frame points in source pixels.
                        cx, cy = yaw_pitch_to_pixel(
                            view_yaw,
                            params.view_pitch_deg + params.view_pitch_offset_deg,
                            src_w,
                            src_h,
                            params.src_hfov_deg,
                        )
                        t = (
                            float(frame.pts * in_video.time_base)
                            if frame.pts is not None and in_video.time_base
                            else frame_idx / fps
                        )
                        viewport_logger.info(
                            '{"xy": [%d, %d], "f": %d, "t": %.2f}',
                            round(cx),
                            round(cy),
                            frame_idx + 1,
                            t,
                        )

                        rgb = frame.to_ndarray(format="rgb24")
                        rendered = _warp_frame(rgb, geom, cfg, params, view_yaw, warper)
                        new_frame = av.VideoFrame.from_ndarray(rendered, format="rgb24")
                        new_frame.pts = frame.pts
                        for v_packet in out_stream.encode(new_frame):
                            out_container.mux(v_packet)
                        frame_idx += 1
                elif in_audio is not None and packet.stream is in_audio:
                    # out_audio is set whenever in_audio is not None (above), so it
                    # is non-None here; assert to narrow it for mypy.
                    assert out_audio is not None
                    if audio_passthrough:
                        packet.stream = out_audio
                        out_container.mux(packet)
                    else:
                        # Same mixed-demux union as the video branch; these packets
                        # belong to the audio stream, so they decode to AudioFrames.
                        for a_frame in cast("list[AudioFrame]", packet.decode()):
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
    """A render variant inside a ``frame_fanout``: which camera path to execute and
    where to write. Inherits all render tuning so each variant can differ."""

    camera_path_key: str  # manifest key holding this variant's camera_path/1 artifact
    output_key: str  # manifest key to record this variant's output path under
    output_name: str  # output filename, written under ctx.group_dir


class RenderFrameConsumer(FrameConsumer[RenderConsumerConfig]):
    """Renders one camera path, encoding to its own output, fed decoded frames by
    the fan-out step (so the source is decoded once for all variants)."""

    config_model = RenderConsumerConfig

    # consumes/produces are config-driven per instance, so they override the
    # base's writeable class attribute with a read-only property. mypy flags the
    # writeable->property override; it's the intended design here.
    @property
    def consumes(self) -> tuple[str, ...]:  # type: ignore[override]
        return (self.config.camera_path_key,)

    @property
    def produces(self) -> tuple[str, ...]:  # type: ignore[override]
        return (self.config.output_key,)

    def open(self, source: FrameSourceInfo, ctx: StepContext, manifest) -> None:
        import av

        cfg = self.config
        self._ow, self._oh = cfg.render_output_width, cfg.render_output_height
        self._sw, self._sh = source.width, source.height
        camera_path_file = manifest.get(cfg.camera_path_key)
        if not camera_path_file:
            raise RuntimeError(
                f"render: no camera_path/1 artifact under manifest key "
                f"{cfg.camera_path_key!r} — run plan_camera first."
            )
        self._commands, self._cmd_g0 = _load_commands(cast(str, camera_path_file))
        polygon = _load_field(manifest.get("field_polygon_path"))
        polygon = _polygon_or_full_frame(polygon, source.width, source.height)
        self._geom = _resolve_geometry(source.width, source.height, cfg, polygon)
        from video_grouper.inference.field_geometry import field_lateral_yaw_extent

        ymin, ymax = field_lateral_yaw_extent(
            polygon, source.width, self._geom.src_hfov_deg
        )
        half = self._geom.src_hfov_deg / 2.0
        self._yaw_min = max(ymin - cfg.render_yaw_padding_deg, -half)
        self._yaw_max = min(ymax + cfg.render_yaw_padding_deg, half)
        self._fps = float(source.average_rate) if source.average_rate else 20.0
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

    def consume(self, rgb, frame_pts: int | None, frame_idx: int) -> None:
        import av

        params, view_yaw = _frame_view(
            _command_for(self._commands, self._cmd_g0, frame_idx),
            self._geom,
            self.config,
            self._yaw_min,
            self._yaw_max,
            self._sw,
            self._sh,
            self._ow,
            self._oh,
        )
        cx, cy = yaw_pitch_to_pixel(
            view_yaw,
            params.view_pitch_deg + params.view_pitch_offset_deg,
            self._sw,
            self._sh,
            params.src_hfov_deg,
        )
        viewport_logger.info(
            '{"xy": [%d, %d], "f": %d, "t": %.2f}',
            round(cx),
            round(cy),
            frame_idx + 1,
            frame_idx / self._fps,
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


class RenderStep(PipelineStep[RenderStepConfig]):
    name = "render"
    config_model = RenderStepConfig
    consumes = ("input_path", "camera_path_path")
    produces = ("output_path",)
    runtime = "service"
    requires = ("av", "cv2")
    resources = ("ram_heavy",)

    async def run(self, manifest: PipelineManifest, ctx: StepContext) -> bool:
        in_path = Path(cast(str, manifest.get("input_path")))
        out_path = Path(cast(str, manifest.get("output_path")))
        # Optional: a field-detect step upstream supplies the ROI polygon used
        # for pan clamping + vertical geometry. Absent ⇒ unconstrained pan.
        field_polygon_path = manifest.get("field_polygon_path")
        # The camera path is REQUIRED — the renderer has no camera brain of its
        # own. A missing artifact is a hard error, never a silent fallback.
        camera_path_file = manifest.get(self.config.render_camera_path_key)
        if not camera_path_file:
            raise RuntimeError(
                f"render: no camera_path/1 artifact under manifest key "
                f"{self.config.render_camera_path_key!r} — run plan_camera first."
            )

        await asyncio.to_thread(
            _render_video,
            str(in_path),
            str(out_path),
            cast(str, camera_path_file),
            field_polygon_path,
            self.config,
        )
        logger.info("render: wrote broadcast-style output to %s", out_path)
        return True


register_step(RenderStep.name, RenderStep, RenderStepConfig)
