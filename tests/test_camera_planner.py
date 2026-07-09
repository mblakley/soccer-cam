"""Camera planner: AutoCam-like aesthetics driven by OUR track (dumb-renderer split).

The planner must follow its input honestly (the ball-state machine already
cleaned it) — no gates, no reacquisition timers — while keeping the calibrated
feel: bounded lag with lead room, incremental zoom, dead-ball widening, and
hold-and-widen only when there is NO information at all."""

import numpy as np

from training.world_model.camera_planner import (
    PlannerConfig,
    plan_camera,
    upsample_track,
)

W, H = 7680, 2160


def test_follows_moving_ball_with_bounded_lag():
    traj = [(1000.0 + 12.0 * t, 1000.0) for t in range(400)]
    plan = plan_camera(traj, src_w=W, src_h=H)
    # after convergence the camera tracks within half a view width
    for t in range(200, 400):
        view_w = W * plan[t][2] / 180.0
        assert abs(plan[t][0] - traj[t][0]) < view_w / 2.0
    # and it is SMOOTH: per-frame pan bounded well below the ball step + lead
    steps = np.diff([p[0] for p in plan[200:]])
    assert np.abs(steps).max() < 40.0


def test_zoom_widens_with_speed_and_is_incremental():
    # slow-but-live (above the dead-ball threshold, below the speed norm)
    slow = [(3000.0 + 6.0 * t, 1000.0) for t in range(300)]
    fast = [(1000.0 + 30.0 * t, 1000.0) for t in range(200)]
    hf_slow = plan_camera(slow, src_w=W, src_h=H)[-1][2]
    hf_fast = plan_camera(fast, src_w=W, src_h=H)[-1][2]
    assert hf_fast > hf_slow + 2.0  # speed widens
    plan = plan_camera(fast, src_w=W, src_h=H)
    dz = np.abs(np.diff([p[2] for p in plan]))
    assert dz.max() < 1.0  # incremental zoom: no snap cuts


def test_deadball_widens_after_sustained_stillness():
    cfg = PlannerConfig()
    traj = [(4000.0 + 10.0 * t, 900.0) for t in range(100)]
    traj += [(5000.0, 900.0)] * 200  # restart hold (e.g. our OOB pin)
    plan = plan_camera(traj, src_w=W, src_h=H, config=cfg)
    assert plan[-1][2] >= cfg.deadball_hfov_deg - 0.5
    # camera stays parked at the pin, not drifting
    assert abs(plan[-1][0] - 5000.0) < 60.0


def test_missing_tail_holds_bearing_and_widens():
    cfg = PlannerConfig()
    traj = [(2000.0 + 10.0 * t, 1000.0) for t in range(100)] + [None] * 150
    plan = plan_camera(traj, src_w=W, src_h=H, config=cfg)
    held = plan[99][0]
    assert all(abs(p[0] - held) < 1e-6 for p in plan[100:])  # bearing held
    assert plan[-1][2] > plan[99][2] + 2.0  # eased wider


def test_depth_term_wider_near():
    far = plan_camera(
        [(4000.0 + 5.0 * t, 300.0) for t in range(300)],
        src_w=W,
        src_h=H,
        depth01=[0.0] * 300,
    )[-1][2]
    near = plan_camera(
        [(4000.0 + 5.0 * t, 1400.0) for t in range(300)],
        src_w=W,
        src_h=H,
        depth01=[1.0] * 300,
    )[-1][2]
    assert near > far + 2.0


def test_upsample_track_linear_and_none_outside():
    track = {0: (100.0, 50.0), 1: (180.0, 50.0)}
    ef = [8, 16]
    out = upsample_track(track, ef, 0, 24)
    assert out[0] is None and out[7] is None  # before the span
    assert out[8] == (100.0, 50.0)
    assert out[12][0] == 140.0  # linear midpoint
    assert out[16] == (180.0, 50.0)
    assert out[17] is None  # after the span
