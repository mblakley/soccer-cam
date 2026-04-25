"""Render stage — broadcast-style virtual camera following the trajectory.

Reads the panoramic source + ``trajectory.json``, smooths the trajectory
with EMA, and writes a 1920×1080 mp4 by cropping a virtual-camera
window centred on the EMA-smoothed ball position.

This is a deliberately straightforward implementation: a fixed crop
size, simple EMA smoothing, and pan-only (no dewarp). The plan calls
out the sophisticated trajectory-following renderer (lead-room,
zone-based zoom, hold-on-out-of-bounds) as a follow-up "once we see
real outputs" — those refinements layer on top of this foundation.

Heavy deps (``cv2``, ``av``) are imported lazily.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from video_grouper.ball_tracking.base import ProviderContext

from . import register_stage
from .base import ProcessingStage

logger = logging.getLogger(__name__)


def _smooth_trajectory(
    trajectory: list[list[float] | None], ema: float
) -> list[tuple[float, float] | None]:
    """EMA-smooth the trajectory, holding the last position when missing."""
    smoothed: list[tuple[float, float] | None] = []
    last: tuple[float, float] | None = None
    for point in trajectory:
        if point is None:
            smoothed.append(last)
            continue
        x, y = float(point[0]), float(point[1])
        if last is None:
            last = (x, y)
        else:
            last = (last[0] * ema + x * (1 - ema), last[1] * ema + y * (1 - ema))
        smoothed.append(last)
    return smoothed


def _render_video(
    input_path: str,
    output_path: str,
    trajectory_path: str,
    out_width: int,
    out_height: int,
    ema: float,
) -> None:
    """Sync helper: pan-crop the source around the EMA-smoothed ball."""
    import av

    with open(trajectory_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    smoothed = _smooth_trajectory(raw, ema)

    with av.open(input_path) as in_container:
        in_video = in_container.streams.video[0]
        src_w = in_video.width
        src_h = in_video.height

        # Vertical centre stays fixed (broadcast cameras don't tilt much
        # for a flat field). Horizontal pans with the ball.
        cy_target = src_h // 2

        with av.open(output_path, mode="w") as out_container:
            out_stream = out_container.add_stream("h264", rate=in_video.average_rate)
            out_stream.width = out_width
            out_stream.height = out_height
            out_stream.pix_fmt = "yuv420p"

            half_w = out_width // 2
            half_h = out_height // 2

            # Default centre when we have no trajectory yet.
            fallback = (src_w / 2.0, float(cy_target))

            for frame_idx, frame in enumerate(in_container.decode(in_video)):
                pos = (
                    smoothed[frame_idx]
                    if frame_idx < len(smoothed) and smoothed[frame_idx] is not None
                    else fallback
                )
                cx, _cy = pos

                # Clamp so the crop stays inside the source frame.
                left = max(0, min(int(round(cx)) - half_w, src_w - out_width))
                top = max(0, min(cy_target - half_h, src_h - out_height))

                rgb = frame.to_ndarray(format="rgb24")
                cropped = rgb[top : top + out_height, left : left + out_width]

                new_frame = av.VideoFrame.from_ndarray(cropped, format="rgb24")
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

        await asyncio.to_thread(
            _render_video,
            str(in_path),
            str(out_path),
            trajectory_path,
            self.provider_config.render_output_width,
            self.provider_config.render_output_height,
            self.provider_config.render_ema,
        )
        logger.info("render: wrote broadcast-style output to %s", out_path)
        return None  # output_path is already in artifacts


register_stage(RenderStage.name, RenderStage)
