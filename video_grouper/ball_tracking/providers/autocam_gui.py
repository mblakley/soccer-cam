"""``autocam_gui`` provider — drives the Once AutoCam desktop app.

Adapter around :func:`video_grouper.tray.autocam_automation.run_autocam_on_file`.
The GUI driver is synchronous and blocks for the duration of AutoCam's
processing; we marshal it onto a worker thread via ``run_in_executor``
so it doesn't block the asyncio loop.
"""

from __future__ import annotations

import asyncio
import logging

from video_grouper.ball_tracking import register_provider
from video_grouper.ball_tracking.base import BallTrackingProvider, ProviderContext
from video_grouper.ball_tracking.config import AutocamGuiProviderConfig

logger = logging.getLogger(__name__)


class AutocamGuiProvider(BallTrackingProvider):
    def __init__(self, config: AutocamGuiProviderConfig):
        self.config = config

    async def run(
        self, input_path: str, output_path: str, ctx: ProviderContext
    ) -> bool:
        # Build the legacy AutocamConfig the GUI driver expects.
        # Imports are local so unit tests can stub the driver without
        # pulling in pywinauto / win32gui at module-import time.
        from video_grouper.tray.autocam_automation import run_autocam_on_file
        from video_grouper.utils.config import AutocamConfig

        legacy_cfg = AutocamConfig(enabled=True, executable=self.config.executable)
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(
                None, run_autocam_on_file, legacy_cfg, input_path, output_path
            )
        except Exception:
            logger.exception(
                "BALL_TRACKING/autocam_gui: failed to process %s -> %s",
                input_path,
                output_path,
            )
            return False


register_provider("autocam_gui", AutocamGuiProvider)
