"""``autocam`` step — drive the Once AutoCam desktop app.

Adapter around :func:`video_grouper.tray.autocam_automation.run_autocam_on_file`.
The GUI driver is synchronous and blocks for the duration of AutoCam's
processing; we marshal it onto a worker thread so it doesn't block the asyncio
loop. AutoCam needs an interactive desktop session, so this step declares
``runtime = "tray"`` and contends for the ``autocam_ui`` resource (only one
AutoCam window at a time).
"""

from __future__ import annotations

import asyncio
import logging
from typing import cast

from pydantic import BaseModel

from video_grouper.pipeline import register_step
from video_grouper.pipeline.base import PipelineStep, StepContext
from video_grouper.pipeline.manifest import PipelineManifest

logger = logging.getLogger(__name__)


class AutocamStepConfig(BaseModel):
    executable: str | None = None


def _invoke_autocam(
    executable: str | None,
    input_path: str,
    output_path: str,
    group_dir: str | None = None,
) -> bool:
    """Lazy-import the GUI driver and run AutoCam on a single file.

    Indirection point: tests patch this function directly so they don't have to
    import ``pywinauto`` (which loads UIAutomationCore.dll at import time and is
    unsafe to load in non-desktop test environments).
    """
    from video_grouper.tray.autocam_automation import run_autocam_on_file
    from video_grouper.utils.config import AutocamConfig

    legacy_cfg = AutocamConfig(enabled=True, executable=executable)
    return run_autocam_on_file(legacy_cfg, input_path, output_path, group_dir=group_dir)


class AutocamStep(PipelineStep[AutocamStepConfig]):
    name = "autocam"
    config_model = AutocamStepConfig
    consumes = ("input_path",)
    produces = ("output_path",)
    runtime = "tray"
    requires = ()
    resources = ("autocam_ui",)

    async def run(self, manifest: PipelineManifest, ctx: StepContext) -> bool:
        # Guaranteed present: the step declares consumes = ("input_path",) and
        # the runner binds both paths before invoking run().
        input_path = cast(str, manifest.get("input_path"))
        output_path = cast(str, manifest.get("output_path"))
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(
                None,
                _invoke_autocam,
                self.config.executable,
                input_path,
                output_path,
                str(ctx.group_dir),
            )
        except Exception:
            logger.exception(
                "autocam: failed to process %s -> %s", input_path, output_path
            )
            return False


register_step(AutocamStep.name, AutocamStep, AutocamStepConfig)
