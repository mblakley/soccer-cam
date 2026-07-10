"""Compatibility shim: the selection/tracking stack is PRODUCT code now.

Moved to :mod:`video_grouper.inference.ball_tracker` (Mark 2026-07-10: single
homegrown path). Everything re-exported so the training harness, sweeps and
replay tooling exercise the exact implementation the product ships.
"""

from video_grouper.inference.ball_tracker import (  # noqa: F401
    Candidate,
    RerankConfig,
    _nearest_on_polygon,
    _restart_spots,
    _world_polygon,
    action_density_prior,
    coast_occlusions,
    kalman_smooth,
    rerank,
    static_persistence,
    track_ball,
)
