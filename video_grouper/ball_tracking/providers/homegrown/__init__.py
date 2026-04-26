"""``homegrown`` provider — our in-house ball-tracking pipeline.

Composed of an ordered list of :class:`ProcessingStage` instances
(stitch_correct → detect → track → render by default). Each stage is
registered at import time; the provider runs them in the configured
order and threads an ``artifacts`` dict between them.
"""

from .provider import HomegrownProvider

# Import each stage module so its top-level register_stage() call runs.
from .stages import detect as _detect_stage  # noqa: F401
from .stages import field_mask as _field_mask_stage  # noqa: F401
from .stages import render as _render_stage  # noqa: F401
from .stages import stitch_correct as _stitch_correct_stage  # noqa: F401
from .stages import track as _track_stage  # noqa: F401

from video_grouper.ball_tracking import register_provider
from video_grouper.ball_tracking.config import HomegrownProviderConfig

register_provider("homegrown", HomegrownProvider, HomegrownProviderConfig)
