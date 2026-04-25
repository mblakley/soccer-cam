"""HomegrownProvider — runs an ordered list of :class:`ProcessingStage`."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from video_grouper.ball_tracking.base import BallTrackingProvider, ProviderContext
from video_grouper.ball_tracking.config import HomegrownProviderConfig

from .stages import create_stage, list_stages

logger = logging.getLogger(__name__)


class HomegrownProvider(BallTrackingProvider):
    def __init__(self, config: HomegrownProviderConfig):
        self.config = config

    async def run(
        self, input_path: str, output_path: str, ctx: ProviderContext
    ) -> bool:
        # Threaded artifacts dict — stages mutate / replace keys as they go.
        # Conventional keys:
        #   input_path:      panoramic source seen by the next stage
        #   output_path:     where render must finally write
        #   stitched_path:   set by stitch_correct (and copied to input_path)
        #   detections_path: per-frame detections JSON written by detect
        #   trajectory_path: smoothed (x, y) per-frame JSON written by track
        artifacts: dict[str, Any] = {
            "input_path": input_path,
            "output_path": output_path,
            "group_dir": str(ctx.group_dir),
        }

        for stage_name in self.config.enabled_stages:
            try:
                stage = create_stage(stage_name, self.config)
            except KeyError:
                logger.error(
                    "BALL_TRACKING/homegrown: unknown stage %r; available: %s",
                    stage_name,
                    ", ".join(sorted(list_stages())) or "(none)",
                )
                return False

            logger.info("BALL_TRACKING/homegrown: running stage %s", stage_name)
            try:
                result = await stage.run(artifacts, ctx)
            except Exception:
                logger.exception("BALL_TRACKING/homegrown: stage %s failed", stage_name)
                return False

            if result:
                artifacts.update(result)

        # The last stage (render) must have written to output_path.
        out = Path(output_path)
        if not out.exists() or out.stat().st_size == 0:
            logger.error(
                "BALL_TRACKING/homegrown: pipeline finished but output %s "
                "is missing or empty",
                output_path,
            )
            return False
        return True
