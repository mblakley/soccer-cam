"""Tests for static-camera multi-frame field-keypoint aggregation.

`aggregate_keypoints` is the permanent fix that makes `field_detect` reliably
produce a complete 10-point polygon: each keypoint is the median over the frames
where it's confident, with a best-frame fallback for keypoints that never clear
the floor (the occlusion-prone near sideline).
"""

import numpy as np
import pytest

pytest.importorskip("onnxruntime")  # field_detector imports it at module load
pytest.importorskip("cv2")

from video_grouper.inference.field_detector import (  # noqa: E402
    aggregate_keypoints,
    build_field_polygon,
)


def _frame(kpts, scores):
    return (np.array(kpts, dtype=np.float64), np.array(scores, dtype=np.float64))


def test_median_over_confident_frames():
    # kpt 0 confident in two frames at (10,10) and (12,12) -> median (11,11).
    f1 = _frame([[10, 10]] + [[0, 0]] * 9, [0.9] + [0.0] * 9)
    f2 = _frame([[12, 12]] + [[0, 0]] * 9, [0.7] + [0.0] * 9)
    agg = aggregate_keypoints([f1, f2], score_threshold=0.5, fallback_to_best=False)
    assert agg[0][0] == 11 and agg[0][1] == 11
    assert agg[0][2] == pytest.approx(0.9)  # reported score = best across frames


def test_fallback_to_best_when_never_confident():
    # kpt 1 never clears 0.5; best frame is score 0.4 at (5,5) -> filled, score<floor.
    f1 = _frame([[0, 0], [3, 3]] + [[0, 0]] * 8, [0.9, 0.2] + [0.0] * 8)
    f2 = _frame([[0, 0], [5, 5]] + [[0, 0]] * 8, [0.9, 0.4] + [0.0] * 8)
    agg = aggregate_keypoints([f1, f2], score_threshold=0.5, fallback_to_best=True)
    assert agg[1][0] == 5 and agg[1][1] == 5
    assert agg[1][2] == pytest.approx(0.4)  # below floor -> caller can distinguish


def test_no_fallback_leaves_none():
    f1 = _frame([[0, 0]] * 10, [0.2] * 10)
    agg = aggregate_keypoints([f1], score_threshold=0.5, fallback_to_best=False)
    assert all(k[0] is None for k in agg)


def test_min_confident_frames_requires_enough():
    # kpt 0 confident in only 1 frame; min_confident=2 -> not medianed.
    f1 = _frame([[10, 10]] + [[0, 0]] * 9, [0.9] + [0.0] * 9)
    f2 = _frame([[0, 0]] * 10, [0.1] * 10)
    agg = aggregate_keypoints(
        [f1, f2], score_threshold=0.5, min_confident=2, fallback_to_best=True
    )
    # only one confident frame for kpt0 -> falls back to its best frame (10,10)
    assert agg[0][0] == 10 and agg[0][1] == 10


def test_complete_polygon_from_aggregation():
    # all 10 confident -> a complete 10-point polygon (near 0-4, far 5-9).
    kp = [[i * 10.0, 100.0 if i < 5 else 50.0] for i in range(10)]
    agg = aggregate_keypoints([_frame(kp, [0.9] * 10)], score_threshold=0.5)
    poly = build_field_polygon(agg)
    assert poly is not None and len(poly) == 10
