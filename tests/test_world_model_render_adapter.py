"""Tests for the world-model -> broadcast renderer trajectory adapter."""

from __future__ import annotations

import json

from training.world_model.render_adapter import (
    save_trajectory_json,
    track_to_trajectory,
)
from training.world_model.tbd import TBDResult, TrackPoint


def test_track_to_trajectory_basic():
    res = TBDResult(
        points=[
            TrackPoint(0, 100.0, 200.0, True),
            TrackPoint(1, 110.0, 205.0, True),
            TrackPoint(2, 120.0, 210.0, False),  # predicted through occlusion
        ]
    )
    assert track_to_trajectory(res, n_frames=5) == [
        [100.0, 200.0],
        [110.0, 205.0],
        [120.0, 210.0],
        None,
        None,
    ]


def test_frame_offset_aligns_to_video_frames():
    res = TBDResult(points=[TrackPoint(0, 5.0, 6.0, True)])
    assert track_to_trajectory(res, n_frames=4, frame_offset=2) == [
        None,
        None,
        [5.0, 6.0],
        None,
    ]


def test_exclude_predicted_becomes_null():
    res = TBDResult(
        points=[TrackPoint(0, 1.0, 2.0, True), TrackPoint(1, 3.0, 4.0, False)]
    )
    assert track_to_trajectory(res, n_frames=2, include_predicted=False) == [
        [1.0, 2.0],
        None,
    ]


def test_save_trajectory_json(tmp_path):
    res = TBDResult(points=[TrackPoint(0, 1.5, 2.5, True)])
    path = tmp_path / "traj.json"
    save_trajectory_json(res, 2, str(path))
    assert json.load(open(path)) == [[1.5, 2.5], None]
