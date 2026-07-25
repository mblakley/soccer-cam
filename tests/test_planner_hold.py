"""W2 stoppage-HOLD: trajectory/2 seam + LIVE/HOLD/REACQUIRE planner FSM.

Two hard guarantees under test here:

1. ``enable_hold=False`` (the default) is BIT-IDENTICAL to the pre-W2 planner —
   ``_reference_plan_camera`` below is a verbatim copy of the implementation
   BEFORE the FSM was added, and a seeded battery of random trajectories must
   produce exactly equal floats, with and without the trajectory/2 channels
   passed.
2. The FSM semantics of the design doc (training/docs/W2_STOPPAGE_HOLD_DESIGN.md
   section 3): entry after exactly ``hold_entry_frames`` consecutive votes,
   anchor = the OUTPUT campath position ``hold_anchor_lookback`` frames before
   the entry run began, exit after ``hold_exit_frames`` tracked frames with a
   linear ``reacq_ramp_frames`` pan-gain ramp, mid-ramp votes returning to the
   SAME anchor, zoom still easing during HOLD, and sub-threshold blips never
   holding.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from video_grouper.inference.camera_planner import (
    PlannerConfig,
    parse_trajectory_artifact,
    plan_camera,
    upsample_disp,
)

W, H = 7680, 2160


# ---------------------------------------------------------------------------
# Verbatim copy of plan_camera as of the pre-W2 implementation (branch
# feat/operator-w2-stoppage-hold before this change). DO NOT EDIT: it is the
# bit-identity oracle for enable_hold=False.
# ---------------------------------------------------------------------------


def _reference_plan_camera(
    trajectory,
    *,
    src_w,
    src_h,
    depth01=None,
    config=None,
):
    cfg = config or PlannerConfig()
    n = len(trajectory)
    out = []
    if n == 0:
        return out

    # seed on the first known position (or frame centre)
    first = next((p for p in trajectory if p is not None), None)
    cx, cy = (
        (float(first[0]), float(first[1]))
        if first is not None
        else (src_w / 2.0, src_h / 2.0)
    )
    hfov = cfg.zoom_base_deg * cfg.zoom_scale
    vx = vy = 0.0
    prev = None
    slow_run = 0

    for t in range(n):
        p = trajectory[t]
        if p is not None:
            x, y = float(p[0]), float(p[1])
            if prev is not None:
                vx = cfg.velocity_ema * (x - prev[0]) + (1 - cfg.velocity_ema) * vx
                vy = cfg.velocity_ema * (y - prev[1]) + (1 - cfg.velocity_ema) * vy
            prev = (x, y)
            speed = float(np.hypot(vx, vy))
            slow = speed / (src_w / 180.0) < cfg.deadball_speed_degf
            slow_run = slow_run + 1 if slow else 0

            d = 0.5
            if depth01 is not None:
                dv = depth01[t]
                if dv is not None:
                    d = float(dv)
            speed_degf = speed / (src_w / 180.0)
            target_hfov = cfg.zoom_base_deg + (
                min(speed_degf / cfg.zoom_speed_norm_degf, 1.0)
                * cfg.zoom_speed_gain_deg
            )
            target_hfov += d * cfg.zoom_depth_gain_deg
            target_hfov = float(
                np.clip(target_hfov, cfg.zoom_min_deg, cfg.zoom_max_deg)
            )
            target_hfov *= cfg.zoom_scale
            if slow_run >= cfg.deadball_frames:
                target_hfov = max(target_hfov, cfg.deadball_hfov_deg)

            view_w_px = src_w * (hfov / 180.0)
            lead_cap = cfg.max_lead_room_fraction * view_w_px
            tx = x + float(np.clip(vx * cfg.lead_frames, -lead_cap, lead_cap))
            ty = y + float(np.clip(vy * cfg.lead_frames, -lead_cap, lead_cap))

            err = float(np.hypot(tx - cx, ty - cy))
            resp = min(1.0, err / max(view_w_px / 2.0, 1.0))
            a_pan = cfg.pan_smoothing_min + resp * (
                cfg.pan_smoothing_max - cfg.pan_smoothing_min
            )
            cx += a_pan * (tx - cx)
            cy += cfg.pitch_smoothing * (ty - cy)
        else:
            prev = None
            vx = vy = 0.0
            target_hfov = cfg.missing_hfov_deg
        hfov += cfg.zoom_smoothing * (target_hfov - hfov)
        out.append((float(cx), float(cy), float(hfov)))
    return out


# ---------------------------------------------------------------------------
# Regression: enable_hold=False is bit-identical to the pre-W2 planner
# ---------------------------------------------------------------------------


def _random_case(rng):
    n = int(rng.integers(50, 400))
    src_w = int(rng.choice([3840, 7680]))
    src_h = int(rng.choice([1080, 2160]))
    x = float(rng.uniform(0.2, 0.8)) * src_w
    y = float(rng.uniform(0.2, 0.8)) * src_h
    traj = []
    t = 0
    while t < n:
        if rng.random() < 0.08:  # a None run (miss/blanked gap)
            run = int(rng.integers(1, 30))
            traj.extend([None] * run)
            t += run
            continue
        x += float(rng.normal(0.0, 20.0))
        y += float(rng.normal(0.0, 8.0))
        traj.append((x, y))
        t += 1
    traj = traj[:n]
    if rng.random() < 0.5:
        depth01 = None
    else:
        depth01 = [
            None if rng.random() < 0.1 else float(rng.uniform(0.0, 1.0))
            for _ in range(n)
        ]
    return traj, depth01, src_w, src_h


def test_enable_hold_false_bit_identical_battery():
    """Fixed-seed battery of random trajectories: the shipped default path must
    produce EXACTLY the pre-change floats — with and without trajectory/2
    channels supplied, and under a non-default aesthetic config."""
    rng = np.random.default_rng(20260725)
    configs = [None, PlannerConfig(zoom_scale=0.95, lead_frames=5.0, fps=30.0)]
    for case in range(25):
        traj, depth01, src_w, src_h = _random_case(rng)
        cfg = configs[case % len(configs)]
        want = _reference_plan_camera(
            traj, src_w=src_w, src_h=src_h, depth01=depth01, config=cfg
        )
        got = plan_camera(traj, src_w=src_w, src_h=src_h, depth01=depth01, config=cfg)
        assert got == want  # exact float equality, every frame
        # trajectory/2 channels present but enable_hold=False: still identical
        states = [
            str(rng.choice(["T", "C", "M"])) if p is not None else "M" for p in traj
        ]
        disp = [
            None if rng.random() < 0.3 else float(rng.uniform(0.0, 400.0)) for _ in traj
        ]
        got2 = plan_camera(
            traj,
            src_w=src_w,
            src_h=src_h,
            depth01=depth01,
            states=states,
            disp=disp,
            config=cfg,
        )
        assert got2 == want


def test_enable_hold_all_live_matches_legacy():
    """A fast all-'T' trajectory never votes: the FSM stays in LIVE and its math
    must replicate the legacy planner exactly (same floats)."""
    traj = [(1000.0 + 20.0 * t, 1000.0 + 2.0 * t) for t in range(300)]
    states = ["T"] * 300
    cfg = PlannerConfig(enable_hold=True)
    want = _reference_plan_camera(traj, src_w=W, src_h=H)
    got = plan_camera(traj, src_w=W, src_h=H, states=states, config=cfg)
    assert got == want


# ---------------------------------------------------------------------------
# FSM semantics on synthetic dense trajectories
# ---------------------------------------------------------------------------


def _live_then_coast(n_live=300, n_coast=100, speed=15.0):
    """Fast tracked ball, then a Kalman-style coast: points keep moving but the
    state flips to 'C' (the tracker lost the ball and is coasting)."""
    traj = [(1000.0 + speed * t, 1000.0) for t in range(n_live + n_coast)]
    states = ["T"] * n_live + ["C"] * n_coast
    return traj, states


def test_hold_entered_exactly_after_entry_frames_and_anchor_lookback():
    traj, states = _live_then_coast()
    cfg = PlannerConfig(enable_hold=True)
    legacy = _reference_plan_camera(traj, src_w=W, src_h=H)
    hold = plan_camera(traj, src_w=W, src_h=H, states=states, config=cfg)
    # C-votes start at t=300 -> the 20th consecutive vote lands at t=319:
    # LIVE (identical to legacy) through 318, HOLD from 319.
    entry = 300 + cfg.hold_entry_frames - 1
    assert hold[:entry] == legacy[:entry]
    # anchor = OUTPUT campath position hold_anchor_lookback frames BEFORE the
    # entry run began (run start 300 -> frame 280) — the last-GOOD position
    ax, ay, _ = hold[300 - cfg.hold_anchor_lookback]
    for t in range(entry, len(hold)):
        assert hold[t][0] == ax and hold[t][1] == ay
    # and the frame before entry was still following (not yet frozen)
    assert hold[entry - 1][0] != ax


def test_hold_entry_clamps_lookback_to_start():
    # votes from t=0 (all 'C'): entry at t=19, run start 0, lookback clamps to 0
    traj = [(2000.0 + 10.0 * t, 900.0) for t in range(80)]
    states = ["C"] * 80
    cfg = PlannerConfig(enable_hold=True)
    hold = plan_camera(traj, src_w=W, src_h=H, states=states, config=cfg)
    ax, ay, _ = hold[0]
    for t in range(19, 80):
        assert hold[t][0] == ax and hold[t][1] == ay


def _hold_exit_scenario():
    """LIVE -> HOLD (C-run) -> stationary 'T' ball far from the anchor.

    hold_speed_thresh=0 turns the slow voter off so votes come ONLY from the
    state channel, letting the exit/ramp run on a stationary ball."""
    traj = [(1000.0 + 30.0 * t, 1000.0) for t in range(100)]
    traj += [(4000.0, 1000.0)] * 40  # coasted, ball lost
    traj += [(5000.0, 1200.0)] * 100  # reacquired, dead-ball stationary
    states = ["T"] * 100 + ["C"] * 40 + ["T"] * 100
    cfg = PlannerConfig(enable_hold=True, hold_speed_thresh=0.0)
    return traj, states, cfg


def test_hold_exit_after_exit_frames_and_linear_ramp():
    traj, states, cfg = _hold_exit_scenario()
    hold = plan_camera(traj, src_w=W, src_h=H, states=states, config=cfg)
    # entry: votes 100..119 -> HOLD at 119; anchor = campath @ 80
    ax, ay, _ = hold[100 - cfg.hold_anchor_lookback]
    assert hold[119][0] == ax and hold[119][1] == ay
    # 'T' resumes at 140; the 8th consecutive T frame (147) starts REACQUIRE —
    # frames 140..146 are still frozen at the anchor
    for t in range(119, 147):
        assert hold[t][0] == ax and hold[t][1] == ay
    assert hold[147][0] != ax  # ramp frame 1: the pan moves again
    # hand-replicated ramp: gain = min(k/reacq_ramp_frames, 1) on the standard
    # error-adaptive pan; velocity EMA restarted at 0 (stationary ball keeps it
    # 0 -> no lead), slow_run long since >= deadball_frames -> zoom target 52.
    cx, cy, hf = hold[146]
    for i, t in enumerate(range(147, 175)):
        view_w = W * (hf / 180.0)
        tx, ty = 5000.0, 1200.0
        err = math.hypot(tx - cx, ty - cy)
        resp = min(1.0, err / max(view_w / 2.0, 1.0))
        a_pan = cfg.pan_smoothing_min + resp * (
            cfg.pan_smoothing_max - cfg.pan_smoothing_min
        )
        gain = min((i + 1) / cfg.reacq_ramp_frames, 1.0)
        cx = cx + gain * a_pan * (tx - cx)
        cy = cy + gain * cfg.pitch_smoothing * (ty - cy)
        hf = hf + cfg.zoom_smoothing * (cfg.deadball_hfov_deg - hf)
        assert hold[t][0] == pytest.approx(cx, rel=1e-9)
        assert hold[t][1] == pytest.approx(cy, rel=1e-9)
        assert hold[t][2] == pytest.approx(hf, rel=1e-9)


def test_mid_ramp_vote_returns_to_same_anchor():
    traj, states, cfg = _hold_exit_scenario()
    states = list(states)
    states[150] = "C"  # 4th ramp frame: the exit condition collapses
    hold = plan_camera(traj, src_w=W, src_h=H, states=states, config=cfg)
    ax, ay, _ = hold[100 - cfg.hold_anchor_lookback]
    assert hold[149][0] != ax  # was ramping
    # back to HOLD at the SAME anchor; T resumes 151 -> 8th T at 158 re-ramps
    for t in range(150, 158):
        assert hold[t][0] == ax and hold[t][1] == ay
    assert hold[158][0] != ax


def test_zoom_still_eases_during_hold_and_widens_on_missing():
    # slow-ish live ball (above both speed thresholds), then coast -> HOLD,
    # then a missing tail while still holding
    n_live, n_coast, n_miss = 200, 80, 80
    traj = [(2000.0 + 6.0 * t, 800.0) for t in range(n_live + n_coast)]
    traj += [None] * n_miss
    states = ["T"] * n_live + ["C"] * n_coast + ["M"] * n_miss
    cfg = PlannerConfig(enable_hold=True)
    hold = plan_camera(traj, src_w=W, src_h=H, states=states, config=cfg)
    entry = n_live + cfg.hold_entry_frames - 1  # 219
    ax = hold[n_live - cfg.hold_anchor_lookback][0]
    # pan frozen through coast AND missing tail (M votes keep it in HOLD)
    for t in range(entry, len(hold)):
        assert hold[t][0] == ax
    # zoom keeps easing toward the dead-ball width during HOLD...
    hfovs = [hold[t][2] for t in range(entry, n_live + n_coast)]
    assert all(b > a for a, b in zip(hfovs, hfovs[1:], strict=False))
    assert hfovs[-1] < cfg.deadball_hfov_deg + 1e-6
    # ...and keeps widening toward missing_hfov once the input disappears
    assert hold[-1][2] > hfovs[-1]


def test_short_blips_below_entry_threshold_never_hold():
    n = 500
    traj = [(1000.0 + 12.0 * t, 1000.0) for t in range(n)]
    states = ["T"] * n
    for t in range(100, 119):  # 19 < hold_entry_frames
        states[t] = "C"
    for t in range(200, 219):
        states[t] = "C"
    states[300] = "C"  # single-frame blip
    cfg = PlannerConfig(enable_hold=True)
    hold = plan_camera(traj, src_w=W, src_h=H, states=states, config=cfg)
    legacy = _reference_plan_camera(traj, src_w=W, src_h=H)
    assert hold == legacy  # never held -> LIVE math, bit-identical


def test_dispersion_voter_votes_below_threshold():
    """Low candidate dispersion votes HOLD (the scramble signature): a fast,
    fully tracked ball still holds when the cloud collapses below the
    threshold for long enough."""
    n = 300
    traj = [(1000.0 + 20.0 * t, 1000.0) for t in range(n)]
    states = ["T"] * n
    disp = [400.0] * n
    for t in range(150, 200):
        disp[t] = 60.0  # < hold_dispersion_px=180 -> votes
    cfg = PlannerConfig(enable_hold=True)
    hold = plan_camera(traj, src_w=W, src_h=H, states=states, disp=disp, config=cfg)
    entry = 150 + cfg.hold_entry_frames - 1
    ax = hold[150 - cfg.hold_anchor_lookback][0]
    assert hold[entry][0] == ax
    # and with dispersion ABOVE threshold everywhere: no hold at all
    live = plan_camera(
        traj, src_w=W, src_h=H, states=states, disp=[400.0] * n, config=cfg
    )
    assert live == _reference_plan_camera(traj, src_w=W, src_h=H)


def test_hold_channel_misalignment_hard_fails():
    traj = [(1000.0, 1000.0)] * 10
    cfg = PlannerConfig(enable_hold=True)
    with pytest.raises(ValueError, match="states"):
        plan_camera(traj, src_w=W, src_h=H, states=["T"] * 9, config=cfg)
    with pytest.raises(ValueError, match="disp"):
        plan_camera(
            traj, src_w=W, src_h=H, states=["T"] * 10, disp=[1.0] * 9, config=cfg
        )


# ---------------------------------------------------------------------------
# trajectory/2 artifact parsing (the seam's reader half)
# ---------------------------------------------------------------------------


def test_parse_trajectory_v2_roundtrip():
    art = parse_trajectory_artifact(
        {
            "schema": "trajectory/2",
            "g_start": 40,
            "fps": 20.0,
            "points": [[1.0, 2.0], None, [3.0, 4.0]],
            "state": ["T", "M", "C"],
            "conf": [0.9, 0.0, 0.0],
            "disp": [12.5, None, 40.0],
        }
    )
    assert art["g_start"] == 40 and art["fps"] == 20.0
    assert art["points"] == [(1.0, 2.0), None, (3.0, 4.0)]
    assert art["state"] == ["T", "M", "C"]
    assert art["conf"] == [0.9, 0.0, 0.0]
    assert art["disp"] == [12.5, None, 40.0]


def test_parse_trajectory_v1_and_bare_list_neutral_default():
    for obj in (
        {
            "schema": "trajectory/1",
            "g_start": 0,
            "fps": 20.0,
            "points": [[1.0, 2.0], None, [3.0, 4.0]],
        },
        [[1.0, 2.0], None, [3.0, 4.0]],  # legacy bare ball_select output
    ):
        art = parse_trajectory_artifact(obj)
        assert art["points"] == [(1.0, 2.0), None, (3.0, 4.0)]
        assert art["state"] == ["T", "M", "T"]  # neutral element: all-T, M at nulls
        assert art["conf"] == [1.0, 0.0, 1.0]
        assert art["disp"] is None


def test_parse_trajectory_rejects_unknown_schema_and_misalignment():
    with pytest.raises(ValueError, match="schema"):
        parse_trajectory_artifact({"schema": "camera_path/1", "points": []})
    with pytest.raises(ValueError, match="misaligned"):
        parse_trajectory_artifact(
            {
                "schema": "trajectory/2",
                "g_start": 0,
                "fps": 20.0,
                "points": [[1.0, 2.0]],
                "state": ["T", "T"],
                "conf": [1.0],
            }
        )


def test_upsample_disp_interpolates_and_masks():
    disp = [10.0, None, 30.0]  # grid frame 16 had no candidates
    ef = [8, 16, 24]
    points = [None] * 30
    for g in range(8, 25):
        points[g] = (float(g), 0.0)
    points[20] = None  # a blanked dense frame must not carry disp
    out = upsample_disp(disp, ef, 0, 30, points=points)
    assert out[0] is None and out[7] is None and out[25] is None
    assert out[8] == 10.0 and out[24] == 30.0
    assert out[16] == pytest.approx(20.0)  # interpolated across the None grid frame
    assert out[12] == pytest.approx(15.0)
    assert out[20] is None
