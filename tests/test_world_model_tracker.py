"""Tests for the causal continuity tracker."""

from __future__ import annotations

from training.world_model.geometry import build_field_geometry
from training.world_model.tbd import Candidate
from training.world_model.tracker import (
    MHTConfig,
    TrackerConfig,
    _static_cells,
    causal_track,
    causal_track_fused,
    multi_hypothesis_track,
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


def _static_fp_scenario():
    """A bright STATIC false-positive at (500,500) every frame, plus the dim ball
    moving along y=200. Brightness alone (argmax/causal) acquires the static FP."""
    frames = []
    for t in range(40):
        ball = Candidate(100.0 + 12 * t, 200.0, 0.6)  # dim, moving
        fp = Candidate(500.0, 500.0, 0.95)  # bright, fixed every frame
        frames.append([ball, fp])
    return frames


def test_static_cells_flags_persistent_feature_not_moving_ball():
    cells = _static_cells(_static_fp_scenario(), cell_px=40.0, thresh=0.5)
    assert (int(500 // 40), int(500 // 40)) in cells  # the fixed FP cell
    # the moving ball never sits in one cell for >=50% of the clip
    assert (int(100 // 40), int(200 // 40)) not in cells
    assert _static_cells(_static_fp_scenario(), 40.0, 0.0) == set()  # disabled


def test_static_aware_avoids_acquiring_a_bright_static_fp():
    frames = _static_fp_scenario()
    # Default: brightness wins acquisition -> locks on the static FP at (500,500).
    plain = causal_track(frames, NEUTRAL, TrackerConfig(gate0=60.0))
    assert abs(plain.points[0].x - 500.0) < 5.0
    # Static-aware: acquires the dim MOVING ball instead and follows it.
    aware = causal_track(frames, NEUTRAL, TrackerConfig(gate0=60.0, static_thresh=0.5))
    by = {p.frame_idx: p for p in aware.points}
    assert abs(by[0].x - 100.0) < 30.0  # acquired the ball, not the FP
    for t in (10, 20, 30):
        assert abs(by[t].x - (100.0 + 12 * t)) < 40.0  # tracking the ball
        assert abs(by[t].y - 200.0) < 60.0  # stayed on the ball's line, not y=500


def test_static_aware_coasts_past_fp_rather_than_following_it():
    # The ball is occluded for a stretch while the static FP keeps firing nearby.
    # A static-aware tracker must NOT snap onto the FP — it coasts.
    frames = [[Candidate(460.0, 500.0, 0.6)]]  # acquire the ball next to the FP path
    for t in range(1, 15):
        cands = [Candidate(500.0, 500.0, 0.95)]  # the persistent FP
        if t < 3:
            cands.append(Candidate(460.0 - 10 * t, 500.0, 0.6))  # ball moving away left
        frames.append(cands)
    aware = causal_track(frames, NEUTRAL, TrackerConfig(gate0=120.0, static_thresh=0.5))
    by = {p.frame_idx: p for p in aware.points}
    assert by[10].x < 460.0  # did not get pulled onto the FP at x=500


def test_multi_hypothesis_tracks_a_clean_ball():
    # On unambiguous data the beam tracker recovers the moving ball.
    frames = [[Candidate(100.0 + 20 * t, 300.0, 0.9)] for t in range(25)]
    res = multi_hypothesis_track(frames, None, NEUTRAL, MHTConfig())
    by = {p.frame_idx: p for p in res.points}
    for t in (5, 15, 24):
        assert abs(by[t].x - (100.0 + 20 * t)) < 40.0


def test_multi_hypothesis_return_all_paths():
    frames = [[Candidate(100.0 + 20 * t, 300.0, 0.9)] for t in range(10)]
    paths = multi_hypothesis_track(
        frames, None, NEUTRAL, MHTConfig(), return_all_paths=True
    )
    assert isinstance(paths, list) and len(paths) >= 1
    assert all(hasattr(p, "points") for p in paths)
