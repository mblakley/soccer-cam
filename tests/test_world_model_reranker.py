"""Tests for the motion-consistency re-ranker (EXP-28).

The crux: a BRIGHT STATIC distractor beats the dim moving ball per-frame (brightness wins),
but is repelled by the static-persistence penalty so the re-ranker follows the ball — the
exact failure mode (lines/tents/benches) that a plain smoothness prior gets backwards.
"""

from __future__ import annotations

import numpy as np
import pytest

from training.world_model.eval import evaluate_recall, evaluate_recall_metric
from training.world_model.geometry import build_field_geometry
from training.world_model.reranker import (
    RerankConfig,
    action_density_prior,
    coast_occlusions,
    rerank,
    static_persistence,
)
from training.world_model.tbd import Candidate

# A real far-corner field polygon (Spencerport clip-1) -> a valid homography.
_POLY = [
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
]
NEUTRAL = build_field_geometry(None)


def test_static_persistence_high_for_fixed_low_for_moving():
    # candidate 0 fixed at origin; candidate 1 moves 4 units/frame (distinct 2-unit cells)
    frames_world = [np.array([[0.0, 0.0], [4.0 * t, 0.0]]) for t in range(10)]
    pers = static_persistence(frames_world, cell_m=2.0)
    # candidate 0 is the fixed point (in its cell every frame) -> ~1.0
    assert all(p[0] > 0.9 for p in pers)
    # candidate 1 moves through a new cell each frame -> small
    assert pers[5][1] < 0.2


def test_rerank_requires_valid_homography():
    with pytest.raises(ValueError):
        rerank([[Candidate(1.0, 1.0, 1.0)]], NEUTRAL)


def test_rerank_follows_moving_ball_over_bright_static_distractor():
    geom = build_field_geometry(np.asarray(_POLY, dtype=float))
    assert geom.valid
    frames: list[list[Candidate]] = []
    gt: list[tuple[int, float, float]] = []
    bright: dict[int, tuple[float, float]] = {}
    for t in range(12):
        # dim ball drifting smoothly across the mid-field (small per-frame world move)
        bx, by = 3200.0 + 26.0 * t, 1000.0
        # bright STATIC distractor parked at a fixed field point
        dx, dy = 4200.0, 1300.0
        cands = [
            Candidate(dx, dy, 0.9),
            Candidate(bx, by, 0.4),
        ]  # distractor first (brighter)
        frames.append(cands)
        gt.append((t, bx, by))
        bright[t] = (cands[0].x, cands[0].y)  # brightness-argmax = the distractor

    # brightness-argmax locks onto the static distractor -> misses the ball
    b_recall, _, _ = evaluate_recall_metric(bright, gt, geom, radius_m=5.0)
    assert b_recall < 0.2

    # the re-ranker's persistence penalty repels the static distractor -> follows the ball
    preds = rerank(frames, geom, config=RerankConfig())
    r_recall, _, _ = evaluate_recall_metric(
        {f: preds[f] for f in preds}, gt, geom, radius_m=5.0
    )
    assert r_recall > 0.8
    assert r_recall > b_recall + 0.6


def test_rerank_handles_empty_frames_and_misses():
    geom = build_field_geometry(np.asarray(_POLY, dtype=float))
    # a moving ball (low persistence) with one empty (fully-occluded) frame in the middle
    frames = []
    for t in range(7):
        frames.append([] if t == 3 else [Candidate(3000.0 + 40.0 * t, 1000.0, 0.6)])
    preds = rerank(frames, geom)
    assert 3 not in preds  # the empty frame coasts as a miss (no candidate to emit)
    assert (
        sum(f in preds for f in (0, 1, 2, 4, 5, 6)) >= 5
    )  # the visible ball is tracked


def test_action_density_prior_favours_the_player_cluster():
    geom = build_field_geometry(np.asarray(_POLY, dtype=float))
    # each frame: a candidate in a dense player cluster (the action) vs a far lone-player
    # candidate. Both move smoothly. The action prior should pick the cluster one.
    frames, boxes, gt = [], [], []
    for t in range(8):
        action = (3200.0 + 24.0 * t, 1000.0)  # in the cluster
        lone = (6800.0 - 24.0 * t, 1300.0)  # far lone player
        frames.append(
            [Candidate(*lone, 0.6), Candidate(*action, 0.5)]
        )  # lone is brighter
        boxes.append(
            [
                (3150.0, 980.0),
                (3260.0, 1020.0),
                (3200.0, 1060.0),  # cluster around the action
                (6800.0 - 24.0 * t, 1300.0),
            ]  # the lone player
        )
        gt.append((t, *action))
    priors = action_density_prior(frames, boxes, geom, weight=1.0)
    preds = rerank(frames, geom, priors=priors)
    res = evaluate_recall({f: preds[f] for f in preds}, gt, radius_px=80.0)
    assert res.recall_all > 0.8  # tracks the action cluster, not the far lone player


def test_coast_occlusions_fills_gap_on_straight_path():
    # ball tracked at t=0 and t=4; occluded (missing) at t=1,2,3
    preds = {0: (100.0, 200.0), 4: (140.0, 200.0)}
    out = coast_occlusions(preds)
    assert set(out) == {0, 1, 2, 3, 4}  # gap filled
    assert out[2] == (120.0, 200.0)  # midpoint of the straight coast
    assert out[1][0] == 110.0 and out[3][0] == 130.0  # linear across the gap
    # endpoints unchanged
    assert out[0] == (100.0, 200.0) and out[4] == (140.0, 200.0)


def test_coast_occlusions_leaves_trailing_miss_empty():
    preds = {0: (100.0, 200.0), 1: (110.0, 200.0)}  # no later anchor
    out = coast_occlusions(preds)
    assert set(out) == {0, 1}  # nothing to coast toward -> unchanged


def test_action_density_prior_zero_when_no_players():
    geom = build_field_geometry(np.asarray(_POLY, dtype=float))
    frames = [[Candidate(3200.0, 1000.0, 0.5)], [Candidate(3240.0, 1000.0, 0.5)]]
    priors = action_density_prior(frames, [[], []], geom)
    assert all((p == 0).all() for p in priors)  # no detections -> no prior


def test_motion_support_bonus_breaks_ties_toward_moving_blob():
    geom = build_field_geometry(np.asarray(_POLY, dtype=float))
    # two equally-bright, equally-non-static candidates each frame; only one is near a
    # motion blob — the motion-support bonus should select it.
    frames, motion, gt = [], [], []
    for t in range(8):
        on = (3200.0 + 24.0 * t, 1000.0)  # near a motion blob, drifting
        off = (5000.0 - 24.0 * t, 1200.0)  # no motion support, drifting the other way
        frames.append([Candidate(*on, 0.5), Candidate(*off, 0.5)])
        motion.append([Candidate(on[0], on[1], 1.0)])
        gt.append((t, on[0], on[1]))
    preds = rerank(frames, geom, motion=motion, config=RerankConfig(motion_w=1.0))
    res = evaluate_recall({f: preds[f] for f in preds}, gt, radius_px=60.0)
    assert res.recall_all > 0.8
