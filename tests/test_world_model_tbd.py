"""Tests for the track-before-detect world-model decoder."""

from __future__ import annotations

import cv2
import numpy as np

from training.world_model.geometry import (
    _touchline_world_points,
    build_field_geometry,
)
from training.world_model.tbd import Candidate, TBDConfig, run_tbd

NEUTRAL = build_field_geometry(None)


# --- a synthetic valid geometry (same setup as the geometry tests) ---
_L, _Wm = 95.0, 60.0
_WORLD_CORNERS = np.array([[0, 0], [_L, 0], [_L, _Wm], [0, _Wm]], dtype=np.float32)
_IMAGE_CORNERS = np.array(
    [[300, 1500], [3800, 1500], [2600, 600], [1500, 600]], dtype=np.float32
)
_H = cv2.getPerspectiveTransform(_WORLD_CORNERS, _IMAGE_CORNERS)


def _w2i(pts) -> np.ndarray:
    p = np.asarray(pts, dtype=np.float64).reshape(-1, 1, 2)
    return cv2.perspectiveTransform(p, _H).reshape(-1, 2)


def _valid_geom():
    poly = _w2i(_touchline_world_points(_L, _Wm))
    geom = build_field_geometry(poly, field_length_m=_L, field_width_m=_Wm)
    assert geom.valid
    return geom


def test_clean_trajectory_recovered():
    frames = [[Candidate(100 + 10 * t, 500.0, 0.6)] for t in range(10)]
    res = run_tbd(frames, NEUTRAL)
    assert len(res.points) == 10
    for t, p in enumerate(res.points):
        assert p.detected
        assert abs(p.x - (100 + 10 * t)) < 1e-6
        assert abs(p.y - 500.0) < 1e-6


def test_follows_smooth_ball_over_higher_scoring_erratic_distractor():
    # Ball: smooth line, modest score. Distractor: HIGHER score but oscillates
    # 600px in y each frame (> teleport cap) so it cannot form its own track.
    frames = []
    for t in range(10):
        ball = Candidate(100 + 10 * t, 500.0, 0.6)
        dy = 250.0 if t % 2 == 0 else 850.0
        distractor = Candidate(100 + 10 * t, dy, 0.95)
        frames.append([ball, distractor])
    res = run_tbd(frames, NEUTRAL)
    assert len(res.points) == 10
    # The MAP path tracks the smooth ball, not the brighter erratic distractor.
    for t, p in enumerate(res.points):
        assert abs(p.y - 500.0) < 50.0
        assert abs(p.x - (100 + 10 * t)) < 1e-6


def test_rejects_higher_scoring_off_field_distractor_by_geometry():
    # Context over appearance: an identical-looking ball OFF the field, brighter
    # and present every frame, must be rejected on geometry, not pixels.
    geom = _valid_geom()
    on_field = _w2i([[_L / 2, _Wm / 2]])[0]
    off_field = np.array([60.0, 60.0])  # image top-left, well outside the polygon
    assert geom.is_in_support(on_field.reshape(1, 2))[0]
    assert not geom.is_in_support(off_field.reshape(1, 2))[0]

    frames = []
    for t in range(10):
        ball = Candidate(on_field[0] + 4 * t, on_field[1], 0.5)
        distractor = Candidate(off_field[0], off_field[1], 0.95)  # brighter, static
        frames.append([ball, distractor])
    res = run_tbd(frames, geom)
    assert len(res.points) == 10
    for t, p in enumerate(res.points):
        # Track stays on the on-field ball, far from the off-field distractor.
        assert abs(p.x - (on_field[0] + 4 * t)) < 60.0
        assert np.hypot(p.x - off_field[0], p.y - off_field[1]) > 200.0


def test_bridges_occlusion_with_physics_prediction():
    # Ball visible 0-2 and 5-9; frames 3-4 are fully occluded (no candidates).
    frames = []
    for t in range(10):
        if t in (3, 4):
            frames.append([])  # occlusion
        else:
            frames.append([Candidate(100 + 10 * t, 500.0, 0.6)])
    res = run_tbd(frames, NEUTRAL)
    by_frame = {p.frame_idx: p for p in res.points}
    # All 10 frames covered; the gap is bridged by constant-velocity prediction.
    assert set(by_frame) == set(range(10))
    for t in (3, 4):
        assert not by_frame[t].detected
        assert abs(by_frame[t].x - (100 + 10 * t)) < 25.0
        assert abs(by_frame[t].y - 500.0) < 25.0
    # Re-acquisition lands back on the true ball.
    assert by_frame[5].detected
    assert abs(by_frame[5].x - 150.0) < 1e-6


def test_does_not_jump_to_distractor_during_occlusion():
    # During the ball's occlusion, an off-path distractor appears; the track must
    # coast (predict) rather than snap to the distractor.
    frames = []
    for t in range(10):
        if t in (4, 5):
            frames.append([Candidate(2000.0, 500.0, 0.9)])  # far-away distractor
        else:
            frames.append([Candidate(100 + 10 * t, 500.0, 0.6)])
    res = run_tbd(frames, NEUTRAL)
    by_frame = {p.frame_idx: p for p in res.points}
    for t in (4, 5):
        # Stayed near the predicted ball line, not at x=2000.
        assert abs(by_frame[t].x - (100 + 10 * t)) < 60.0
        assert not by_frame[t].detected


def test_empty_input_returns_empty():
    assert run_tbd([], NEUTRAL).points == []
    assert run_tbd([[], [], []], NEUTRAL).points == []


def test_config_is_tunable():
    cfg = TBDConfig(max_speed_px=50.0)
    frames = [[Candidate(100 + 10 * t, 500.0, 0.6)] for t in range(5)]
    res = run_tbd(frames, NEUTRAL, cfg)
    assert len(res.points) == 5


def test_occlusion_prediction_stays_in_frame_with_decay_and_clamp():
    # Ball seen moving fast, then a long occlusion. Without decay+clamp the CV
    # prediction would extrapolate off to infinity; with them it stays in-frame.
    cfg = TBDConfig(
        occlusion_decay=0.7,
        frame_w=1000.0,
        frame_h=600.0,
        max_speed_px=2000.0,
        teleport_px=2000.0,
    )
    frames = [[Candidate(100.0, 300.0, 0.9)], [Candidate(300.0, 300.0, 0.9)]]
    frames += [[] for _ in range(30)]  # long occlusion
    res = run_tbd(frames, NEUTRAL, cfg)
    by = {p.frame_idx: p for p in res.points}
    assert len(res.points) == 32
    for p in res.points:  # every predicted position stays within the clamp box
        assert -1.0 <= p.x <= 1001.0
        assert -1.0 <= p.y <= 601.0
    assert not by[31].detected  # deep in occlusion
    assert by[31].x < 1000.0  # decayed to a stop, did not run away
