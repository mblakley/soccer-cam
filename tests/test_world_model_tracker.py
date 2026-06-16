"""Tests for the causal continuity tracker."""

from __future__ import annotations

from training.world_model.geometry import build_field_geometry
from training.world_model.tbd import Candidate
from training.world_model.tracker import (
    TrackerConfig,
    causal_track,
    causal_track_fused,
)

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


def test_action_prior_pulls_toward_action_during_appearance_gap():
    # Appearance shows the ball moving RIGHT, then drops out. During the gap the
    # ball actually turned DOWN; action (motion) clusters there. The action prior
    # should pull the track down toward the action, where a blind coast drifts right.
    appearance: list[list[Candidate]] = [
        [Candidate(100.0, 300.0, 0.9)],
        [Candidate(115.0, 300.0, 0.9)],
        [Candidate(130.0, 300.0, 0.9)],
    ]
    action: list[list[Candidate]] = [[], [], []]
    for t in range(3, 10):
        appearance.append([])  # appearance gap
        ay = 300.0 + 15 * (t - 2)  # the ball turned downward
        action.append(
            [
                Candidate(130.0 + dx, ay + dy, 1.0)
                for dx in (-20, 0, 20)
                for dy in (-10, 10)
            ]
        )
    common = {"gate0": 80.0, "max_lost": 20, "frame_w": 2000.0, "frame_h": 2000.0}
    fused = causal_track_fused(
        appearance, action, None, TrackerConfig(action_pull=0.6, **common)
    )
    blind = causal_track_fused(
        appearance,
        [[] for _ in appearance],
        None,
        TrackerConfig(action_pull=0.0, **common),
    )
    f9 = {p.frame_idx: p for p in fused.points}[9]
    b9 = {p.frame_idx: p for p in blind.points}[9]
    assert f9.y > b9.y + 50.0  # action pulled it down toward the play
    assert f9.x < b9.x  # and it didn't drift as far right as the blind coast
