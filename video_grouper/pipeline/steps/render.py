"""Render step — broadcast-style virtual camera following the trajectory.

Reads the panoramic ``input_path`` + ``trajectory_path``, smooths the
trajectory with EMA, and writes the ``output_path`` mp4 by cropping a
virtual-camera window centred on the EMA-smoothed ball position.

Deliberately straightforward: a fixed crop size, simple EMA smoothing, pan-only
(no dewarp). Lead-room / zone-based zoom refinements layer on top later.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from pydantic import BaseModel

from video_grouper.pipeline import register_step
from video_grouper.pipeline.base import PipelineStep, StepContext
from video_grouper.pipeline.manifest import PipelineManifest

logger = logging.getLogger(__name__)


class RenderStepConfig(BaseModel):
    render_ema: float = 0.975
    render_lead_room: float = 0.15
    render_output_width: int = 1920
    render_output_height: int = 1080
    render_fov_deg: float = 50.0


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

        # Vertical centre stays fixed (broadcast cameras don't tilt much for a
        # flat field). Horizontal pans with the ball.
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


class RenderStep(PipelineStep):
    name = "render"
    config_model = RenderStepConfig
    consumes = ("input_path", "trajectory_path")
    produces = ("output_path",)
    runtime = "service"
    requires = ("av",)
    resources = ("ram_heavy",)

    async def run(self, manifest: PipelineManifest, ctx: StepContext) -> bool:
        in_path = Path(manifest.get("input_path"))
        out_path = Path(manifest.get("output_path"))
        trajectory_path = manifest.get("trajectory_path")

        await asyncio.to_thread(
            _render_video,
            str(in_path),
            str(out_path),
            trajectory_path,
            self.config.render_output_width,
            self.config.render_output_height,
            self.config.render_ema,
        )
        logger.info("render: wrote broadcast-style output to %s", out_path)
        return True


register_step(RenderStep.name, RenderStep, RenderStepConfig)
