"""Compatibility shim: field-plane geometry is PRODUCT code now.

Moved to :mod:`video_grouper.inference.world_geometry` (Mark 2026-07-10: single
homegrown path — the training harness imports the same implementation the
product runs).
"""

from video_grouper.inference.world_geometry import (  # noqa: F401
    DEFAULT_FALLBACK_BALL_PX,
    DEFAULT_FIELD_LENGTH_M,
    DEFAULT_FIELD_WIDTH_M,
    MAX_REPROJ_ERROR_PX,
    MIN_POLYGON_AREA_PX,
    SOCCER_BALL_DIAMETER_M,
    FieldGeometry,
    _apply_homography,
    _touchline_world_points,
    build_field_geometry,
)
