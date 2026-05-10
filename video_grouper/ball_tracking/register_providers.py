"""Import-time registration of all built-in ball-tracking providers.

Importing this module triggers each provider module to register itself.
The wiring side (e.g. ``VideoGrouperApp``) imports this once at startup,
mirroring the cameras-module convention.

Each provider import is wrapped in try/except so a missing optional
dependency for one provider doesn't poison the others. The tray bundle
ships only ``autocam_gui`` (its TRAY_EXCLUDES drops ONNX/cv2 — the
homegrown provider's stack), and tray code paths must be able to call
this module without dragging the homegrown chain in. Service installs
on a developer machine with a broken cv2 install also keep working for
autocam_gui flows.

Imports are written as static ``from ... import`` so PyInstaller's
static analyzer detects them and bundles the provider modules. A
dynamic ``__import__("...")`` would skip bundling, leaving the install
with an empty provider registry at runtime.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

try:
    from video_grouper.ball_tracking.providers import autocam_gui  # noqa: F401
except Exception as e:  # noqa: BLE001 — see module docstring
    logger.debug(
        "ball_tracking: provider autocam_gui unavailable (%s: %s)",
        type(e).__name__,
        e,
    )

try:
    from video_grouper.ball_tracking.providers import homegrown  # noqa: F401
except Exception as e:  # noqa: BLE001 — see module docstring
    logger.debug(
        "ball_tracking: provider homegrown unavailable (%s: %s)",
        type(e).__name__,
        e,
    )
