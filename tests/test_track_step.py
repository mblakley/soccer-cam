"""Tests for the track step's tunable confidence + field-location filters on raw detections."""

from __future__ import annotations

import json

import numpy as np

# Force cv2 to bootstrap NOW, at collection time. _run_tracking imports field_detector (-> cv2)
# lazily, inside the test; the autouse mock_file_system fixture patches os.path.exists, which breaks
# cv2's config-file loader. Importing it here — before any fixture runs — makes this file pass in
# isolation, not only when a cv2-importing test module happens to be collected first.
import video_grouper.inference.field_detector  # noqa: F401,E402
from video_grouper.pipeline.steps.track import _run_tracking  # noqa: E402


def test_run_tracking_applies_conf_and_field_filters(tmp_path):
    # Per frame: a moving in-field ball (conf 0.9), low-confidence noise (0.1), and an off-field
    # false positive (conf 0.9, far away). The track step must keep only the ball.
    dets = []
    for f in range(20):
        dets.append(
            {"frame_idx": f, "cx": 200.0 + 10.0 * f, "cy": 250.0, "conf": 0.9}
        )  # ball
        dets.append(
            {"frame_idx": f, "cx": 205.0 + 10.0 * f, "cy": 250.0, "conf": 0.1}
        )  # low conf
        dets.append(
            {"frame_idx": f, "cx": 5000.0, "cy": 5000.0, "conf": 0.9}
        )  # off-field FP
    det_path = tmp_path / "detections.json"
    traj_path = tmp_path / "trajectory.json"
    json.dump(dets, open(det_path, "w"))

    poly = np.array([[100, 100], [600, 100], [600, 400], [100, 400]], dtype=np.float32)
    populated = _run_tracking(
        str(det_path),
        str(traj_path),
        gate_distance=200.0,
        max_missing=15,
        conf_threshold=0.45,
        field_polygon=poly,
        field_margin=0.0,
    )
    traj = json.load(open(traj_path))
    assert populated >= 15  # the ball is tracked across most frames
    for p in traj:
        if p is not None:
            assert p[0] < 1500  # never the off-field FP at x=5000
            assert abs(p[1] - 250.0) < 120  # stays on the ball's row, not the noise/FP


def test_run_tracking_no_polygon_keeps_all_in_field(tmp_path):
    dets = [
        {"frame_idx": f, "cx": 100.0 + 10.0 * f, "cy": 200.0, "conf": 0.9}
        for f in range(15)
    ]
    det_path = tmp_path / "d.json"
    traj_path = tmp_path / "t.json"
    json.dump(dets, open(det_path, "w"))
    populated = _run_tracking(
        str(det_path),
        str(traj_path),
        gate_distance=200.0,
        max_missing=15,
        conf_threshold=0.45,
    )
    assert populated >= 13  # no field polygon ⇒ confidence filter only
