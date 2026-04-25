"""``autocam_gui`` provider — drives the Once AutoCam desktop app.

Adapter around :func:`video_grouper.tray.autocam_automation.run_autocam_on_file`.
The GUI driver is synchronous and blocks for the duration of AutoCam's
processing; we marshal it onto a worker thread via ``run_in_executor``
so it doesn't block the asyncio loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from video_grouper.ball_tracking import register_provider
from video_grouper.ball_tracking.base import BallTrackingProvider, ProviderContext
from video_grouper.ball_tracking.config import AutocamGuiProviderConfig

logger = logging.getLogger(__name__)


def _invoke_autocam(
    executable: Optional[str], input_path: str, output_path: str
) -> bool:
    """Lazy-import the GUI driver and run AutoCam on a single file.

    Indirection point: tests patch this function directly so they don't have
    to import ``pywinauto`` (which loads UIAutomationCore.dll at import time
    and is unsafe to load in non-desktop test environments).
    """
    from video_grouper.tray.autocam_automation import run_autocam_on_file
    from video_grouper.utils.config import AutocamConfig

    legacy_cfg = AutocamConfig(enabled=True, executable=executable)
    return run_autocam_on_file(legacy_cfg, input_path, output_path)


class AutocamGuiProvider(BallTrackingProvider):
    def __init__(self, config: AutocamGuiProviderConfig):
        self.config = config

    async def run(
        self, input_path: str, output_path: str, ctx: ProviderContext
    ) -> bool:
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(
                None,
                _invoke_autocam,
                self.config.executable,
                input_path,
                output_path,
            )
        except Exception:
            logger.exception(
                "BALL_TRACKING/autocam_gui: failed to process %s -> %s",
                input_path,
                output_path,
            )
            return False


register_provider("autocam_gui", AutocamGuiProvider, AutocamGuiProviderConfig)
