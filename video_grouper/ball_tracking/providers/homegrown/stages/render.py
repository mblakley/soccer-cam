"""Render stage — broadcast-style virtual camera following the trajectory.

Reads the panoramic source + ``trajectory.json``, smooths the trajectory
with EMA, and writes a 1920×1080 mp4 by rendering a virtual perspective
camera through cylindrical projection of the source. The pan target is
the EMA-smoothed ball pixel mapped to a yaw via the equirectangular
source model in :mod:`video_grouper.inference.cylindrical_view`.

This phase implements the rendering layer only — the intelligent control
logic (lead room, zone-based zoom, dead-ball overrides, broadcast vs
coach modes) from ``docs/VIRTUAL_CAMERA.md`` lands in subsequent commits.
For now: pan with EMA-smoothed yaw, fixed view FOV, fixed pitch, hold
last position when ball is missing.

Heavy deps (``cv2``, ``av``) are imported lazily.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from video_grouper.ball_tracking.base import ProviderContext
from video_grouper.inference.cylindrical_view import (
    CylindricalViewParams,
    cylindrical_remap,
    pixel_to_yaw_pitch,
)

from . import register_stage
from .base import ProcessingStage

logger = logging.getLogger(__name__)


def _smooth_yaw(yaws: list[float | None], ema: float) -> list[float | None]:
    """EMA-smooth a yaw series, holding last value when missing."""
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
    trajectory: list[list[float] | None],
    src_w: int,
    src_h: int,
    src_hfov_deg: float,
) -> list[float | None]:
    """Project each (x, y) pixel into a yaw angle, preserving ``None`` gaps."""
    yaws: list[float | None] = []
    for point in trajectory:
        if point is None:
            yaws.append(None)
            continue
        yaw, _pitch = pixel_to_yaw_pitch(
            float(point[0]), float(point[1]), src_w, src_h, src_hfov_deg
        )
        yaws.append(yaw)
    return yaws


def _render_video(
    input_path: str,
    output_path: str,
    trajectory_path: str,
    out_width: int,
    out_height: int,
    ema: float,
    src_hfov_deg: float,
    src_vfov_deg: float,
    view_hfov_deg: float,
    view_vfov_deg: float,
    view_pitch_deg: float,
) -> None:
    """Sync helper: cylindrical-render the source around the EMA-smoothed yaw."""
    import av
    import cv2

    with open(trajectory_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    with av.open(input_path) as in_container:
        in_video = in_container.streams.video[0]
        src_w = in_video.width
        src_h = in_video.height

        params = CylindricalViewParams(
            src_w=src_w,
            src_h=src_h,
            src_hfov_deg=src_hfov_deg,
            src_vfov_deg=src_vfov_deg,
            out_w=out_width,
            out_h=out_height,
            view_hfov_deg=view_hfov_deg,
            view_vfov_deg=view_vfov_deg,
            view_pitch_deg=view_pitch_deg,
        )

        yaws = _trajectory_to_yaws(raw, src_w, src_h, src_hfov_deg)
        smoothed = _smooth_yaw(yaws, ema)

        with av.open(output_path, mode="w") as out_container:
            out_stream = out_container.add_stream("h264", rate=in_video.average_rate)
            out_stream.width = out_width
            out_stream.height = out_height
            out_stream.pix_fmt = "yuv420p"

            fallback_yaw = 0.0  # straight-ahead until we get a fix

            for frame_idx, frame in enumerate(in_container.decode(in_video)):
                yaw = (
                    smoothed[frame_idx]
                    if frame_idx < len(smoothed) and smoothed[frame_idx] is not None
                    else fallback_yaw
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
                for packet in out_stream.encode(new_frame):
                    out_container.mux(packet)

            for packet in out_stream.encode():
                out_container.mux(packet)


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
            cfg.render_output_width,
            cfg.render_output_height,
            cfg.render_ema,
            cfg.render_src_hfov_deg,
            cfg.render_src_vfov_deg,
            cfg.render_fov_deg,
            cfg.render_view_vfov_deg,
            cfg.render_pitch_deg,
        )
        logger.info("render: wrote broadcast-style output to %s", out_path)
        return None  # output_path is already in artifacts


register_stage(RenderStage.name, RenderStage)
