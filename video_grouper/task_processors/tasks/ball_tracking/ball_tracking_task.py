"""Homegrown ball-tracking task — runs the in-house ML pipeline.

Pairs with :class:`ExternalBallTrackingTask` (which spawns an external GUI
tool); both inherit from :class:`BallTrackingTaskBase`. This class owns
the dependency surface that the homegrown provider needs — PyAV for
source validation, then the ONNX/CV2 stack inside the provider stages.
That makes the import safe to skip in the tray PyInstaller target.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import av

from video_grouper.utils.ffmpeg_utils import av_open_read

from .base import BallTrackingTaskBase

logger = logging.getLogger(__name__)


class BallTrackingTask(BallTrackingTaskBase):
    """Homegrown ball-tracking via the in-house ONNX/CV2 provider stages."""

    @property
    def task_type(self) -> str:
        return "ball_tracking_homegrown"

    def _validate_video_file(self, path: str) -> bool:
        """Use PyAV to verify the source file exists, has size, and decodes."""
        if not os.path.isfile(path):
            logger.error("BALL_TRACKING: input file does not exist: %s", path)
            return False

        file_size = os.path.getsize(path)
        if file_size < 10_000:
            logger.error(
                "BALL_TRACKING: input file too small (%d bytes): %s", file_size, path
            )
            return False

        try:
            with av_open_read(path) as container:
                duration = None
                if container.duration is not None:
                    duration = container.duration / av.time_base
                else:
                    for stream in container.streams.video:
                        if stream.duration is not None and stream.time_base is not None:
                            duration = float(stream.duration * stream.time_base)
                            break
                if duration is None or duration <= 0:
                    logger.error(
                        "BALL_TRACKING: invalid duration (%s) for %s", duration, path
                    )
                    return False
                logger.info(
                    "BALL_TRACKING: input validated — duration=%.1fs, size=%.1fMB: %s",
                    duration,
                    file_size / (1024 * 1024),
                    path,
                )
                return True
        except (ValueError, av.error.FFmpegError) as e:
            logger.error("BALL_TRACKING: error validating input file: %s", e)
            return False

    async def execute(self) -> bool:
        try:
            logger.info(
                "BALL_TRACKING: processing group=%s provider=%s",
                self.group_dir.name,
                self.provider_name,
            )

            if not self._validate_video_file(self.input_path):
                return False

            # Ensure built-in providers are registered.
            from video_grouper.ball_tracking import (  # noqa: F401
                create_provider,
                register_providers,
            )
            from video_grouper.ball_tracking.base import ProviderContext

            provider = create_provider(self.provider_name, self.provider_config)
            ctx = ProviderContext(
                group_dir=self.group_dir,
                team_name=self.team_name,
                storage_path=Path(self.storage_path or self.group_dir.parent),
                ttt_config=self.ttt_config,
            )
            success = await provider.run(self.input_path, self.output_path, ctx)
            if success:
                logger.info("BALL_TRACKING: completed group=%s", self.group_dir.name)
            else:
                logger.error(
                    "BALL_TRACKING: provider returned False for group=%s",
                    self.group_dir.name,
                )
            return success
        except Exception:
            logger.exception(
                "BALL_TRACKING: error processing group=%s", self.group_dir.name
            )
            return False
