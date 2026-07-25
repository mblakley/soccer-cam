"""Tests for fixed-camera static-feature suppression."""

from __future__ import annotations

from training.world_model.measurements import (
    mask_person_candidates,
    suppress_static_candidates,
)
from training.world_model.tbd import Candidate


def test_suppresses_static_keeps_moving():
    # A static bright candidate at (100,100) every frame + a moving candidate.
    fl = [
        [Candidate(100.0, 100.0, 0.9), Candidate(200.0 + 20 * t, 500.0, 0.6)]
        for t in range(20)
    ]
    out, static = suppress_static_candidates(fl, cell_px=40.0, occupancy_frac=0.25)
    assert len(static) >= 1
    # the static candidate is gone from every frame...
    for cands in out:
        assert all(not (abs(c.x - 100) < 1 and abs(c.y - 100) < 1) for c in cands)
    # ...and exactly the 20 moving candidates survive.
    assert sum(len(c) for c in out) == 20


def test_keeps_all_when_nothing_static():
    fl = [[Candidate(10.0 + 50 * t, 50.0, 0.8)] for t in range(10)]  # always moving
    out, static = suppress_static_candidates(fl, cell_px=40.0, occupancy_frac=0.25)
    assert static == set()
    assert sum(len(c) for c in out) == 10


def test_restart_static_ball_survives_but_background_suppressed():
    # Whole-clip background line at (100,100); the ball sits still at (500,500)
    # for the first 30 of 100 frames (a restart wait) then moves away. Background
    # is suppressed; the briefly-static restart ball survives (it's <50% of clip).
    fl = []
    for t in range(100):
        cands = [Candidate(100.0, 100.0, 0.9)]
        if t < 30:
            cands.append(Candidate(500.0, 500.0, 0.7))  # waiting at the restart
        else:
            cands.append(Candidate(500.0 + 20 * (t - 30), 500.0, 0.7))  # then moving
        fl.append(cands)
    out, _ = suppress_static_candidates(fl)  # default frac=0.5
    for cands in out:  # background gone everywhere
        assert all(not (abs(c.x - 100) < 1 and abs(c.y - 100) < 1) for c in cands)
    # restart-static ball present in the early (waiting) frames
    assert any(abs(c.x - 500) < 1 and abs(c.y - 500) < 1 for c in out[0])


def test_motion_protects_a_static_ball_but_suppresses_a_static_line():
    # A nearly-static ball at (500,500) with players (motion) moving around it, plus
    # a static background "line" at (100,100) with NO motion. With motion given, the
    # ball is protected (action nearby) while the line is still suppressed.
    appearance = []
    motion = []
    for _ in range(40):
        appearance.append([Candidate(500.0, 500.0, 0.7), Candidate(100.0, 100.0, 0.9)])
        motion.append(
            [Candidate(510.0, 520.0, 1.0), Candidate(490.0, 510.0, 1.0)]
        )  # near the ball only
    out, static = suppress_static_candidates(appearance, motion=motion)
    assert any(
        abs(c.x - 500) < 1 and abs(c.y - 500) < 1 for c in out[0]
    )  # ball survives
    assert all(
        not (abs(c.x - 100) < 1 and abs(c.y - 100) < 1) for c in out[0]
    )  # line gone
    # Without motion, the static ball would be wrongly suppressed too:
    out2, _ = suppress_static_candidates(appearance)
    assert all(not (abs(c.x - 500) < 1 and abs(c.y - 500) < 1) for c in out2[0])


def test_empty_input():
    assert suppress_static_candidates([]) == ([], set())


def test_mask_person_candidates_drops_candidate_on_a_person():
    # A bright distractor at (600,500) sits inside a person box; the ball at
    # (100,200) is in open space. The mask drops the distractor, keeps the ball.
    frames = [[Candidate(100.0, 200.0, 0.6), Candidate(600.0, 500.0, 0.95)]]
    boxes = [[(560.0, 440.0, 640.0, 600.0)]]  # person around the distractor
    out = mask_person_candidates(frames, boxes)
    assert any(abs(c.x - 100.0) < 1 for c in out[0])  # ball kept
    assert all(not (abs(c.x - 600.0) < 1) for c in out[0])  # distractor dropped


def test_mask_person_expand_catches_edge_candidate():
    # A candidate just outside the raw box is caught with the 0.3 expand.
    frames = [[Candidate(105.0, 100.0, 0.9)]]
    boxes = [[(0.0, 0.0, 100.0, 100.0)]]  # raw box ends at x=100; expand adds 30
    assert mask_person_candidates(frames, boxes, expand=0.3)[0] == []
    assert len(mask_person_candidates(frames, boxes, expand=0.0)[0]) == 1


def test_mask_person_no_boxes_keeps_all():
    frames = [[Candidate(1.0, 1.0, 0.9), Candidate(2.0, 2.0, 0.8)]]
    assert mask_person_candidates(frames, [[]]) == frames
