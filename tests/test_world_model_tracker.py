"""Tests for the causal continuity tracker."""

from __future__ import annotations

from training.world_model.geometry import build_field_geometry
from training.world_model.tbd import Candidate
from training.world_model.tracker import TrackerConfig, causal_track

NEUTRAL = build_field_geometry(None)


def test_follows_ball_not_brighter_distractor_in_gate():
    # The thesis: continuity beats appearance. A brighter distractor sits 45px off
    # the ball's smooth path (inside the gate); the tracker must pick the closer
    # ball, not the brighter distractor (which is what argmax would grab).
    frames = [[Candidate(100.0, 300.0, 0.9)]]  # frame 0: acquire the ball
    for t in range(1, 20):
        ball = Candidate(100.0 + 15 * t, 300.0, 0.7)
        distractor = Candidate(100.0 + 15 * t + 45, 300.0, 0.95)  # brighter, off-path
        frames.append([ball, distractor])
    res = causal_track(frames, NEUTRAL, TrackerConfig(gate0=90.0))
    by = {p.frame_idx: p for p in res.points}
    for t in range(1, 20):
        ball_x = 100.0 + 15 * t
        assert abs(by[t].x - ball_x) < 20.0  # on the ball
        assert abs(by[t].x - (ball_x + 45)) > 25.0  # not on the distractor


def test_reacquires_after_occlusion_gap():
    frames = [[Candidate(100.0 + 15 * t, 300.0, 0.9)] for t in range(5)]
    frames += [[] for _ in range(10)]  # occlusion 5..14
    frames += [[Candidate(100.0 + 15 * t, 300.0, 0.9)] for t in range(15, 20)]
    res = causal_track(frames, NEUTRAL, TrackerConfig(max_lost=8))
    by = {p.frame_idx: p for p in res.points}
    assert not by[8].detected  # coasting in the gap
    assert by[15].detected  # re-acquired when the ball reappears
    assert abs(by[15].x - (100.0 + 15 * 15)) < 30.0


def test_coasting_prediction_stays_in_frame():
    cfg = TrackerConfig(
        vel_decay=0.7, frame_w=1000.0, frame_h=600.0, gate0=50.0, max_lost=100
    )
    frames = [[Candidate(100.0, 300.0, 0.9)], [Candidate(300.0, 300.0, 0.9)]]
    frames += [[] for _ in range(40)]  # long occlusion, no candidates
    res = causal_track(frames, NEUTRAL, cfg)
    for p in res.points:
        assert -1.0 <= p.x <= 1001.0
        assert -1.0 <= p.y <= 601.0


def test_empty_until_acquisition():
    # No candidate clears acq_score until frame 2 -> track starts at frame 2.
    frames = [[Candidate(10.0, 10.0, 0.1)], [], [Candidate(50.0, 50.0, 0.9)]]
    res = causal_track(frames, NEUTRAL, TrackerConfig(acq_score=0.5))
    assert res.points[0].frame_idx == 2
    assert res.points[0].detected
