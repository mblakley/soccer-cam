"""Stitch-correction step — apply per-row dx shift to fix the dual-lens seam.

Reads the panoramic ``input_path``, applies
:func:`video_grouper.utils.stitch_remap.apply_shift_to_frame_rgb` per frame
using the configured calibration profile, writes a corrected mp4 alongside, and
rebinds ``input_path`` so downstream steps consume the corrected video.

Pass-through (no work, no error) when the profile path isn't configured or the
file isn't loadable — stitch correction is an opt-in calibration.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import cast

from pydantic import BaseModel

from video_grouper.pipeline import register_step
from video_grouper.pipeline.base import PipelineStep, StepContext
from video_grouper.pipeline.manifest import PipelineManifest

logger = logging.getLogger(__name__)


class StitchCorrectStepConfig(BaseModel):
    stitch_profile_path: str | None = None


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


class StitchCorrectStep(PipelineStep[StitchCorrectStepConfig]):
    name = "stitch_correct"
    config_model = StitchCorrectStepConfig
    consumes = ("input_path",)
    # Optional step: no declared output the runner must validate. When it
    # corrects, it records stitched_path and rebinds input_path in the manifest
    # artifact map (persisted across stages so downstream + resume see it);
    # pass-through is a no-op.
    produces = ()
    runtime = "service"
    requires = ("av",)
    resources = ("ram_heavy",)

    async def run(self, manifest: PipelineManifest, ctx: StepContext) -> bool:
        profile_path = self.config.stitch_profile_path
        if not profile_path:
            logger.info(
                "stitch_correct: no stitch_profile_path configured; passing through"
            )
            return True

        # input_path is the immutable source the runner binds before run().
        in_path = Path(cast(str, manifest.get("input_path")))
        out_path = in_path.with_name(f"{in_path.stem}.stitched.mp4")

        success = await asyncio.to_thread(
            _correct_video, str(in_path), str(out_path), profile_path
        )
        if not success:
            logger.warning(
                "stitch_correct: correction failed; downstream steps will use "
                "the uncorrected source"
            )
            return True

        manifest.put("stitched_path", str(out_path))
        manifest.put("input_path", str(out_path))
        return True


register_step(StitchCorrectStep.name, StitchCorrectStep, StitchCorrectStepConfig)
