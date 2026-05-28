"""External ball-tracking task — drives the Once AutoCam GUI.

Pairs with :class:`BallTrackingTask` (homegrown ML pipeline) — both inherit
from :class:`BallTrackingTaskBase`. This class deliberately avoids the
heavy inference stack (PyAV / ONNX / OpenCV) so the tray PyInstaller
target can drop those modules and sidestep the onnxruntime/PyQt6
MSVCP140.dll initialization conflict.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .base import BallTrackingTaskBase

logger = logging.getLogger(__name__)


class ExternalBallTrackingTask(BallTrackingTaskBase):
    """Run ball-tracking by spawning an external GUI tool (autocam_gui)."""

    @property
    def task_type(self) -> str:
        return "ball_tracking_external"

    def _validate_video_file(self, path: str) -> bool:
        """Lightweight check — file exists and is non-trivially sized.

        The external tool decodes the file itself; we don't pull in PyAV
        just to pre-validate. A failed decode surfaces from the GUI driver.
        """
        if not os.path.isfile(path):
            logger.error("BALL_TRACKING: input file does not exist: %s", path)
            return False

        file_size = os.path.getsize(path)
        if file_size < 10_000:
            logger.error(
                "BALL_TRACKING: input file too small (%d bytes): %s", file_size, path
            )
            return False

        logger.info(
            "BALL_TRACKING: input ready — size=%.1fMB: %s",
            file_size / (1024 * 1024),
            path,
        )
        return True

    async def execute(self) -> bool:
        try:
            logger.info(
                "BALL_TRACKING: processing group=%s provider=%s",
                self.group_dir.name,
                self.provider_name,
            )

            if not self._validate_video_file(self.input_path):
                return False

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
