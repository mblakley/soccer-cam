"""Selection/tracking stack (product): physics Viterbi + RTS smoother basics."""

from __future__ import annotations

import numpy as np

from video_grouper.inference.ball_tracker import (
    Candidate,
    RerankConfig,
    kalman_smooth,
    rerank,
    static_persistence,
)
from video_grouper.inference.world_geometry import build_field_geometry

SRC_W, SRC_H = 1920, 1080
POLY = np.array(
    [
        [100.0, 1000.0],
        [500.0, 1010.0],
        [960.0, 1015.0],
        [1420.0, 1010.0],
        [1820.0, 1000.0],
        [1600.0, 300.0],
        [1280.0, 295.0],
        [960.0, 290.0],
        [640.0, 295.0],
        [320.0, 300.0],
    ],
    float,
)


def _geom():
    g = build_field_geometry(POLY)
    assert g.valid
    return g


def _moving_vs_static_frames(n=30):
    """A ball moving smoothly along the field + a static distractor, per frame."""
    frames = []
    for t in range(n):
        ball = Candidate(x=400.0 + 30.0 * t, y=700.0, score=0.4)
        static = Candidate(x=1200.0, y=650.0, score=0.9)  # brighter, never moves
        frames.append([ball, static])
    return frames


def test_static_persistence_flags_the_fixed_distractor():
    geom = _geom()
    frames = _moving_vs_static_frames()
    world = [
        geom.image_to_world(np.array([[c.x, c.y] for c in cs], float)) for cs in frames
    ]
    pers = static_persistence(world, cell_m=2.0)
    assert np.mean([p[1] for p in pers]) > 0.9  # the static candidate
    assert np.mean([p[0] for p in pers]) < 0.4  # the moving ball


def test_rerank_prefers_the_moving_ball_over_the_bright_static():
    geom = _geom()
    frames = _moving_vs_static_frames()
    preds = rerank(frames, geom, config=RerankConfig())
    picks_ball = sum(
        1 for t, (x, _y) in preds.items() if abs(x - (400.0 + 30.0 * t)) < 1.0
    )
    assert picks_ball >= 0.9 * len(preds)


def test_physical_transitions_forbid_teleports():
    """With phys transitions on, a distant one-frame flicker cannot be taken."""
    geom = _geom()
    frames = _moving_vs_static_frames()
    # Insert a very bright far-corner flicker mid-track.
    frames[15] = [frames[15][0], Candidate(x=1800.0, y=310.0, score=5.0)]
    cfg = RerankConfig(alpha=1.0, phys_sigma_px=5.0)
    preds = rerank(frames, geom, config=cfg)
    if 15 in preds:
        assert abs(preds[15][0] - (400.0 + 30.0 * 15)) < 200.0


def test_kalman_smooth_fills_gaps_and_dejitters():
    geom = _geom()
    preds = {
        t: (400.0 + 30.0 * t + (5.0 if t % 2 else -5.0), 700.0)
        for t in range(20)
        if t not in (8, 9, 10)
    }
    sm = kalman_smooth(preds, geom)
    assert set(sm) == set(range(20))  # occlusion coasted
    xs = [sm[t][0] for t in range(20)]
    steps = np.diff(xs)
    assert np.std(steps) < 10.0  # de-jittered vs the ±5 px input wobble


def test_offfield_gate_suppresses_offfield_distractor():
    """A bright STATIC distractor above the far touchline (off-field) must not be
    selected during in-field play when the off-field state gate is on."""
    geom = _geom()
    frames = []
    for t in range(30):
        ball = Candidate(
            x=400.0 + 30.0 * t, y=700.0, score=0.4
        )  # in-field, moving, dim
        distractor = Candidate(
            x=960.0, y=200.0, score=0.95
        )  # OFF-field, bright, static
        frames.append([ball, distractor])
    cfg = RerankConfig(offfield_gate=True, offfield_penalty=6.0, static_w=2.0)
    preds = rerank(frames, geom, config=cfg)
    at_distractor = sum(
        1 for x, y in preds.values() if abs(x - 960.0) < 5 and abs(y - 200.0) < 5
    )
    assert at_distractor == 0
    # and it still follows the in-field ball
    on_ball = sum(
        1 for t, (x, _y) in preds.items() if abs(x - (400.0 + 30.0 * t)) < 30.0
    )
    assert on_ball >= 0.8 * len(preds)
