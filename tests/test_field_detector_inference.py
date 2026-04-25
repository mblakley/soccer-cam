"""Tests for video_grouper.inference.field_detector."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import numpy as np

from video_grouper.inference import field_detector


class TestCreateFieldSession:
    def test_use_gpu_true_lists_cuda_first(self):
        with patch.object(field_detector.ort, "InferenceSession") as mock_cls:
            field_detector.create_field_session(Path("model.onnx"), use_gpu=True)

        _args, kwargs = mock_cls.call_args
        assert kwargs["providers"] == [
            "CUDAExecutionProvider",
            "CPUExecutionProvider",
        ]

    def test_use_gpu_false_lists_cpu_only(self):
        with patch.object(field_detector.ort, "InferenceSession") as mock_cls:
            field_detector.create_field_session(Path("model.onnx"), use_gpu=False)

        _args, kwargs = mock_cls.call_args
        assert kwargs["providers"] == ["CPUExecutionProvider"]


class TestIsOnField:
    def test_none_polygon_accepts_everything(self):
        assert field_detector.is_on_field(0, 0, None) is True
        assert field_detector.is_on_field(99999, 99999, None) is True

    def test_inside_simple_rectangle(self):
        polygon = np.array([[0, 0], [100, 0], [100, 100], [0, 100]], dtype=np.float32)
        assert field_detector.is_on_field(50, 50, polygon, margin=0) is True

    def test_far_outside_rejected(self):
        polygon = np.array([[0, 0], [100, 0], [100, 100], [0, 100]], dtype=np.float32)
        assert field_detector.is_on_field(500, 500, polygon, margin=0) is False

    def test_within_margin_accepted(self):
        polygon = np.array([[0, 0], [100, 0], [100, 100], [0, 100]], dtype=np.float32)
        assert field_detector.is_on_field(110, 50, polygon, margin=20) is True


class TestBuildFieldPolygon:
    def test_too_few_keypoints_returns_none(self):
        # Only 1 near and 1 far keypoint detected.
        kpts = [
            (0.0, 1700.0, 0.9),
            (None, None, 0.1),
            (None, None, 0.1),
            (None, None, 0.1),
            (None, None, 0.1),
            (None, None, 0.1),
            (None, None, 0.1),
            (None, None, 0.1),
            (None, None, 0.1),
            (4000.0, 300.0, 0.9),
        ]
        assert field_detector.build_field_polygon(kpts) is None

    def test_full_keypoint_set_builds_polygon(self):
        kpts = [(float(x), 1700.0, 0.9) for x in range(0, 5000, 1000)] + [
            (float(x), 300.0, 0.9) for x in range(5000, 0, -1000)
        ]
        polygon = field_detector.build_field_polygon(kpts)
        assert polygon is not None
        assert polygon.shape == (10, 2)


class TestCurvedField:
    def test_center_inside(self):
        # x=PANO_CENTER_X gives y_far=310, y_near=1600.
        assert field_detector.is_on_field_curved(2048, 1000, margin=0) is True

    def test_above_far_sideline_rejected(self):
        assert field_detector.is_on_field_curved(2048, 100, margin=0) is False

    def test_below_near_sideline_rejected(self):
        assert field_detector.is_on_field_curved(2048, 1700, margin=0) is False

    def test_filter_detections_field_drops_off_field(self):
        detections = [
            {"cx": 2048, "cy": 1000},  # on field
            {"cx": 2048, "cy": 100},  # off field (above)
            {"cx": 2048, "cy": 1700},  # off field (below)
        ]
        filtered = field_detector.filter_detections_field(detections, margin=0)
        assert len(filtered) == 1
        assert filtered[0]["cy"] == 1000
