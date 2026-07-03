"""RANK diagnostic (sweep_tracker.rank_table): the GT ball's score-rank among its
frame's candidates, split near/far — the evidence that decides re-ranker vs detector
work. Uses a synthetic trapezoid field whose homography fits exactly (parallel
touchlines + linear spacing = affine)."""

import numpy as np

from training.cli.sweep_tracker import rank_table
from training.world_model.geometry import build_field_geometry
from training.world_model.tbd import Candidate


def _geom():
    near_x = np.linspace(100.0, 1900.0, 5)
    far_x = np.linspace(1600.0, 400.0, 5)  # far touchline runs right -> left
    poly = np.concatenate(
        [
            np.column_stack([near_x, np.full(5, 1000.0)]),
            np.column_stack([far_x, np.full(5, 200.0)]),
        ]
    )
    geom = build_field_geometry(poly)
    assert geom.valid, "synthetic polygon must yield a valid homography"
    return geom


def _cand(x, y, score):
    return Candidate(x=x, y=y, score=score, size_px=None)


def test_rank_and_bands():
    geom = _geom()
    near_gt = (1000.0, 900.0)  # near touchline -> big expected diameter
    far_gt = (1000.0, 250.0)  # far touchline -> small expected diameter
    exp_near = float(geom.expected_ball_diameter_px(np.asarray([near_gt]))[0])
    exp_far = float(geom.expected_ball_diameter_px(np.asarray([far_gt]))[0])
    far_px = (exp_near + exp_far) / 2  # split threshold between the two bands

    frames = [
        # frame 0: near ball is the 2nd-highest score (rank 2)
        [_cand(500.0, 950.0, 0.9), _cand(*near_gt, 0.5)],
        # frame 1: far ball is 3rd of three (rank 3)
        [_cand(300.0, 260.0, 0.8), _cand(1700.0, 260.0, 0.5), _cand(*far_gt, 0.2)],
        # frame 2: no candidate anywhere near the far GT -> absent
        [_cand(300.0, 950.0, 0.9)],
    ]
    balls = {0: near_gt, 1: far_gt, 2: far_gt}
    ranks = rank_table(frames, [0, 1, 2], balls, geom, far_px, stride=1)

    assert ranks["near"] == [2]
    assert sorted(ranks["far"], key=lambda r: (r is None, r)) == [3, None]


def test_empty_frame_counts_absent():
    geom = _geom()
    gt = (1000.0, 250.0)
    ranks = rank_table([[]], [0], {0: gt}, geom, far_px=1e9, stride=1)
    assert ranks["far"] == [None]


def test_top_ranked_ball():
    geom = _geom()
    gt = (1000.0, 900.0)
    frames = [[_cand(*gt, 0.9), _cand(200.0, 950.0, 0.1)]]
    ranks = rank_table(frames, [0], {0: gt}, geom, far_px=0.0, stride=1)
    assert ranks["near"] == [1]
