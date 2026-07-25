"""Compatibility shim: selector features are PRODUCT code now.

Moved to :mod:`video_grouper.inference.ball_selector` (Mark 2026-07-10: single
homegrown path — train-time features come from the exact module the product
runs at inference).
"""

from video_grouper.inference.ball_selector import (  # noqa: F401
    FEATURE_FAMILIES,
    FEATURE_NAMES,
    build_features,
    feature_mask,
)
