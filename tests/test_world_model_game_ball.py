"""Tests for the game-ball role/handoff state machine."""

from __future__ import annotations

import numpy as np

from training.world_model.game_ball import RoleConfig, track_game_ball
from training.world_model.geometry import build_field_geometry
from training.world_model.tbd import Candidate
from training.world_model.tracker import TrackerConfig

# In-bounds region ~ x[300,1900], y[400,1000] (near touchline y=1000, far y=400).
_POLY = np.array(
    [
        [300, 1000],
        [700, 1000],
        [1100, 1000],
        [1500, 1000],
        [1900, 1000],
        [1700, 400],
        [1350, 400],
        [1000, 400],
        [650, 400],
        [300, 400],
    ],
    dtype=float,
)
GEOM = build_field_geometry(_POLY)
CFG = TrackerConfig(
    gate0=200.0, gate_grow=50.0, max_lost=20, frame_w=3000.0, frame_h=2000.0
)
ROLE = RoleConfig(out_of_play_frames=4, sustain_frames=4, new_ball_min_dist=400.0)


def _ball(x, y):
    return Candidate(float(x), float(y), 0.9)


def test_in_play_works_on_poly():
    assert GEOM.polygon is not None
    assert GEOM.is_in_support(np.array([[1000.0, 800.0]]), 60.0)[0]  # inside
    assert not GEOM.is_in_support(np.array([[1000.0, 1300.0]]), 60.0)[
        0
    ]  # below touchline


def test_normal_play_no_handoff():
    frames = [[_ball(1000 + 5 * t, 800)] for t in range(30)]  # in-bounds, gentle drift
    res = track_game_ball(frames, None, GEOM, CFG, ROLE)
    assert res.handoffs == []
    assert abs(res.points[-1].x - (1000 + 5 * 29)) < 60


def _A_leaves(t):
    # A: in-bounds, then walks out the bottom in 100px steps, then stays out.
    if t <= 5:
        return _ball(1000, 800)
    if t <= 10:
        return _ball(1000, 800 + 100 * (t - 5))
    return _ball(1000, 1300)


def test_handoff_when_ball_leaves_and_new_ball_sustained():
    frames = []
    for t in range(30):
        cands = [_A_leaves(t)]
        if t >= 10:
            cands.append(_ball(500, 600))  # ball B played in, in-bounds, far from A
        frames.append(cands)
    res = track_game_ball(frames, None, GEOM, CFG, ROLE)
    assert len(res.handoffs) >= 1  # role transferred to B
    # final track is on B, not the abandoned A out of bounds
    assert np.hypot(res.points[-1].x - 500, res.points[-1].y - 600) < 60


def test_no_handoff_when_ball_leaves_but_no_replacement():
    frames = [[_A_leaves(t)] for t in range(30)]  # A leaves, nobody plays a new ball in
    res = track_game_ball(frames, None, GEOM, CFG, ROLE)
    assert res.handoffs == []  # primary just stays out; no role transfer


def test_intruder_ball_while_in_play_does_not_switch():
    frames = []
    for t in range(30):
        cands = [_ball(1000, 800)]  # game ball stays in play the whole time
        if 10 <= t <= 12:  # a brief intruder ball appears in-field, far away
            cands.append(_ball(500, 600))
        frames.append(cands)
    res = track_game_ball(frames, None, GEOM, CFG, ROLE)
    assert res.handoffs == []  # gate stays shut while the game ball is in play
    assert np.hypot(res.points[-1].x - 1000, res.points[-1].y - 800) < 60
