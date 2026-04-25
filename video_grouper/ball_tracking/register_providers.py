"""Import-time registration of all built-in ball-tracking providers.

Importing this module triggers each provider module to register itself.
The wiring side (e.g. ``VideoGrouperApp``) imports this once at startup,
mirroring the cameras-module convention.
"""

from __future__ import annotations

# noqa: F401 — imports are for side effects (registration on import)
from video_grouper.ball_tracking.providers import autocam_gui  # noqa: F401
from video_grouper.ball_tracking.providers import homegrown  # noqa: F401
