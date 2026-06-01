"""Import-time registration of all built-in pipeline steps.

Importing this module triggers each step module to register itself. The wiring
side (``VideoGrouperApp`` / the tray) imports this once at startup, mirroring
the cameras-module convention.

Each import is wrapped in try/except so a missing optional dependency for one
step doesn't poison the others. The tray bundle ships without the ONNX/cv2/av
stack, so ``detect`` / ``track`` / ``render`` will fail to import there and be
omitted, leaving ``autocam`` (its only relevant step) registered. The imports
are written as static ``from ... import`` so PyInstaller's static analyzer
detects and bundles them — a dynamic ``__import__`` would skip bundling.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

try:
    from video_grouper.pipeline.steps import autocam  # noqa: F401
except Exception as e:  # noqa: BLE001 — see module docstring
    logger.debug("pipeline: step autocam unavailable (%s: %s)", type(e).__name__, e)

try:
    from video_grouper.pipeline.steps import stitch_correct  # noqa: F401
except Exception as e:  # noqa: BLE001
    logger.debug(
        "pipeline: step stitch_correct unavailable (%s: %s)", type(e).__name__, e
    )

try:
    from video_grouper.pipeline.steps import detect  # noqa: F401
except Exception as e:  # noqa: BLE001
    logger.debug("pipeline: step detect unavailable (%s: %s)", type(e).__name__, e)

try:
    from video_grouper.pipeline.steps import track  # noqa: F401
except Exception as e:  # noqa: BLE001
    logger.debug("pipeline: step track unavailable (%s: %s)", type(e).__name__, e)

try:
    from video_grouper.pipeline.steps import render  # noqa: F401
except Exception as e:  # noqa: BLE001
    logger.debug("pipeline: step render unavailable (%s: %s)", type(e).__name__, e)
