"""Stitch-correction stage — apply per-row dx shift to fix dual-lens seam.

Reads the panoramic input video, applies
:func:`video_grouper.utils.stitch_remap.apply_shift_to_frame_rgb` per
frame using the configured calibration profile, and writes a corrected
mp4 alongside the input. Subsequent stages consume the corrected video
via the ``input_path`` artifact.

Pass-through (no work, no error) when the profile path isn't configured
or the file isn't loadable — that mirrors the existing soccer-cam
convention of treating stitch correction as an opt-in calibration.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from video_grouper.ball_tracking.base import ProviderContext

from . import register_stage
from .base import ProcessingStage

logger = logging.getLogger(__name__)


def _correct_video(input_path: str, output_path: str, profile_path: str) -> bool:
    """Sync helper: read input, apply per-row dx shift, write output.

    Returns True on success, False on any failure (caller decides what to do).
    """
    import av  # lazy: PyAV is heavy

    from video_grouper.utils.stitch_remap import (
        apply_shift_to_frame_rgb,
        build_dx_lookup,
        load_profile,
    )

    profile = load_profile(profile_path)
    if profile is None:
        logger.warning(
            "stitch_correct: profile not loadable at %s; skipping correction",
            profile_path,
        )
        return False

    try:
        with av.open(input_path) as in_container:
            in_video = in_container.streams.video[0]
            dx_lookup = build_dx_lookup(profile, in_video.width, in_video.height)
            seam_x = int(profile.seam_x * (in_video.width / profile.source_width))

            with av.open(output_path, mode="w") as out_container:
                out_video = out_container.add_stream("h264", rate=in_video.average_rate)
                out_video.width = in_video.width
                out_video.height = in_video.height
                out_video.pix_fmt = "yuv420p"

                for frame in in_container.decode(in_video):
                    rgb = frame.to_ndarray(format="rgb24")
                    corrected = apply_shift_to_frame_rgb(rgb, dx_lookup, seam_x)
                    new_frame = av.VideoFrame.from_ndarray(corrected, format="rgb24")
                    new_frame.pts = frame.pts
                    for packet in out_video.encode(new_frame):
                        out_container.mux(packet)

                for packet in out_video.encode():
                    out_container.mux(packet)
        return True
    except Exception:
        logger.exception("stitch_correct: encoding failed for %s", input_path)
        return False


class StitchCorrectStage(ProcessingStage):
    name = "stitch_correct"

    async def run(
        self, artifacts: dict[str, Any], ctx: ProviderContext
    ) -> dict[str, Any] | None:
        profile_path = self.provider_config.stitch_profile_path
        if not profile_path:
            logger.info(
                "stitch_correct: no stitch_profile_path configured; passing through"
            )
            return None

        in_path = Path(artifacts["input_path"])
        out_path = in_path.with_name(f"{in_path.stem}.stitched.mp4")

        success = await asyncio.to_thread(
            _correct_video, str(in_path), str(out_path), profile_path
        )
        if not success:
            logger.warning(
                "stitch_correct: correction failed; downstream stages will use "
                "the uncorrected source"
            )
            return None

        # Subsequent stages should consume the corrected video.
        return {
            "stitched_path": str(out_path),
            "input_path": str(out_path),
        }


register_stage(StitchCorrectStage.name, StitchCorrectStage)
