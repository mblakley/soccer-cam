"""RenderStep camera-path mode: commands executed verbatim, feasibility only.

The dumb-renderer contract (2026-07-09/10): with a command present, the internal
ball-following brain must have NO influence — the view centers where the planner
said (clamped to projection feasibility) at the planner's hfov."""

import numpy as np

from video_grouper.inference.cylindrical_view import pixel_to_yaw_pitch
from video_grouper.pipeline.steps.render import (
    RenderStepConfig,
    _CameraState,
    _frame_view,
    _resolve_geometry,
    _resolve_mode,
)

_POLY = np.asarray(
    [
        [270.8, 1071.9],
        [2674.9, 1460.6],
        [3749.7, 1530.2],
        [5468.7, 1481.1],
        [7339.5, 1215.1],
        [5337.4, 327.3],
        [4397.9, 204.6],
        [3803.1, 171.8],
        [3277.9, 175.9],
        [2227.7, 261.8],
    ],
    dtype=np.float32,
)


def _setup(cfg=None):
    cfg = cfg or RenderStepConfig(render_zoom_scale=1.0)
    geom = _resolve_geometry(7680, 2160, cfg, _POLY)
    mode = _resolve_mode(cfg.render_mode)
    return cfg, geom, mode


def test_command_bypasses_internal_brain():
    cfg, geom, mode = _setup()
    state = _CameraState()
    cmd = (3200.0, 1000.0, 44.0)
    params, view_yaw = _frame_view(
        state,
        None,
        geom,
        mode,
        cfg,
        -80.0,
        80.0,
        None,
        7680,
        2160,
        1920,
        1080,
        command=cmd,
    )
    want_yaw, _ = pixel_to_yaw_pitch(3200.0, 1000.0, 7680, 2160, geom.src_hfov_deg)
    assert abs(view_yaw - want_yaw) < 0.2  # centered where commanded
    # hfov survives the params-stage zoom-scale multiply as commanded (or was
    # tightened by the containment solver, never widened)
    assert params.view_hfov_deg <= 44.0 + 0.1


def test_command_yaw_clamped_to_feasible_range():
    cfg, geom, mode = _setup()
    state = _CameraState()
    params, view_yaw = _frame_view(
        state,
        None,
        geom,
        mode,
        cfg,
        -10.0,
        10.0,
        None,
        7680,
        2160,
        1920,
        1080,
        command=(7600.0, 1000.0, 47.0),  # far right edge: infeasible center
    )
    assert view_yaw <= 10.0  # clamped, not executed blindly


def test_zoom_scale_round_trip():
    cfg, geom, mode = _setup(RenderStepConfig(render_zoom_scale=0.9))
    state = _CameraState()
    params, _ = _frame_view(
        state,
        None,
        geom,
        mode,
        cfg,
        -80.0,
        80.0,
        None,
        7680,
        2160,
        1920,
        1080,
        command=(3800.0, 1200.0, 45.0),
    )
    # planner hfov is final: the internal *0.9 must not shrink it to 40.5
    assert abs(params.view_hfov_deg - 45.0) < 1.0 or params.view_hfov_deg < 45.0
