"""Tests for fixed-camera static-feature suppression."""

from __future__ import annotations

from training.world_model.measurements import suppress_static_candidates
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


def test_empty_input():
    assert suppress_static_candidates([]) == ([], set())
