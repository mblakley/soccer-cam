"""Compatibility shim: the camera planner is PRODUCT code now.

Moved to :mod:`video_grouper.inference.camera_planner` (Mark 2026-07-10: the
planner ships; the training harness imports the same implementation the
product runs — no eval/product divergence).
"""

from video_grouper.inference.camera_planner import (  # noqa: F401
    PlannerConfig,
    plan_camera,
    save_camera_path,
    upsample_track,
)
