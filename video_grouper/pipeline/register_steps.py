"""Import-time registration of all built-in pipeline steps.

Importing this module triggers each step module to register itself. The wiring
side (``VideoGrouperApp`` / the tray) imports this once at startup, mirroring
the cameras-module convention.

Each import is wrapped in try/except so a missing optional dependency for one
step doesn't poison the others. In the tray bundle (which excludes the inference
stack) only ``detect`` actually fails to import — it pulls in
``onnxruntime``/``cv2`` at module top — and is omitted. ``stitch_correct`` and
``render`` import ``av`` lazily (inside functions) and ``track`` is numpy-only,
so all three still register in the tray; they're gated OUT at runtime instead,
by their ``runtime="service"`` (the tray hands them off) and ``meta.available``.
The imports are static ``from ... import`` so PyInstaller's analyzer detects and
bundles them — a dynamic ``__import__`` would skip bundling.
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
    from video_grouper.pipeline.steps import field_detect  # noqa: F401
except Exception as e:  # noqa: BLE001
    logger.debug(
        "pipeline: step field_detect unavailable (%s: %s)", type(e).__name__, e
    )

try:
    from video_grouper.pipeline.steps import ball_detect  # noqa: F401
except Exception as e:  # noqa: BLE001
    logger.debug("pipeline: step ball_detect unavailable (%s: %s)", type(e).__name__, e)

try:
    from video_grouper.pipeline.steps import track  # noqa: F401
except Exception as e:  # noqa: BLE001
    logger.debug("pipeline: step track unavailable (%s: %s)", type(e).__name__, e)

try:
    from video_grouper.pipeline.steps import render  # noqa: F401
except Exception as e:  # noqa: BLE001
    logger.debug("pipeline: step render unavailable (%s: %s)", type(e).__name__, e)

try:
    # Imports render too (the built-in "render" frame consumer), so import after it.
    from video_grouper.pipeline.steps import fanout  # noqa: F401
except Exception as e:  # noqa: BLE001
    logger.debug("pipeline: step fanout unavailable (%s: %s)", type(e).__name__, e)
