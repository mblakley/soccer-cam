"""Import-time registration of all built-in ball-tracking providers.

Importing this module triggers each provider module to register itself.
The wiring side (e.g. ``VideoGrouperApp``) imports this once at startup,
mirroring the cameras-module convention.

Each provider is imported in its own try/except so a missing optional
dependency for one provider doesn't poison the others. The tray bundle
ships only ``autocam_gui`` (its TRAY_EXCLUDES drops ONNX/cv2 — the
homegrown provider's stack), and tray code paths must be able to call
this module without dragging the homegrown chain in. Service installs
on a developer machine with a broken cv2 install also keep working for
autocam_gui flows.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _try_register(module_path: str) -> None:
    """Import a provider module, logging any failure but not raising.

    A bundled native dep (cv2, onnxruntime) can fail at import time with
    OSError / FileNotFoundError when its DLLs are missing or broken,
    which would shadow the working provider modules if we let it
    propagate. We accept that quietly and let the registry's later
    ``create_provider(name)`` call surface the missing-provider error
    at the actual call site, where it's actionable.
    """
    try:
        __import__(module_path)
    except Exception as e:  # noqa: BLE001 — see docstring
        logger.debug(
            "ball_tracking: provider %s unavailable (%s: %s)",
            module_path,
            type(e).__name__,
            e,
        )


_try_register("video_grouper.ball_tracking.providers.autocam_gui")
_try_register("video_grouper.ball_tracking.providers.homegrown")
