"""Tests for video_grouper.inference.ball_detector."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from video_grouper.inference import ball_detector


class TestCreateSession:
    def test_use_gpu_true_lists_cuda_first(self):
        with patch.object(ball_detector.ort, "InferenceSession") as mock_cls:
            mock_cls.return_value.get_providers.return_value = ["CPUExecutionProvider"]
            ball_detector.create_session(Path("model.onnx"), use_gpu=True)

        _args, kwargs = mock_cls.call_args
        assert kwargs["providers"] == [
            "CUDAExecutionProvider",
            "CPUExecutionProvider",
        ]

    def test_use_gpu_false_lists_cpu_only(self):
        with patch.object(ball_detector.ort, "InferenceSession") as mock_cls:
            mock_cls.return_value.get_providers.return_value = ["CPUExecutionProvider"]
            ball_detector.create_session(Path("model.onnx"), use_gpu=False)

        _args, kwargs = mock_cls.call_args
        assert kwargs["providers"] == ["CPUExecutionProvider"]

    def test_returns_session_instance(self):
        with patch.object(ball_detector.ort, "InferenceSession") as mock_cls:
            sess = MagicMock()
            sess.get_providers.return_value = ["CPUExecutionProvider"]
            mock_cls.return_value = sess

            result = ball_detector.create_session(Path("model.onnx"))

        assert result is sess


class TestPanoToTile:
    def test_center_in_top_left_tile(self):
        labels = ball_detector.pano_to_tile(cx=100.0, cy=100.0, w=20.0, h=20.0)
        rows = {(label["row"], label["col"]) for label in labels}
        assert (0, 0) in rows

    def test_center_outside_all_tiles_returns_empty(self):
        labels = ball_detector.pano_to_tile(
            cx=ball_detector.PANO_W + 100,
            cy=ball_detector.PANO_H + 100,
            w=20,
            h=20,
        )
        assert labels == []

    def test_normalized_coords_within_unit_range(self):
        labels = ball_detector.pano_to_tile(cx=100.0, cy=100.0, w=20.0, h=20.0)
        for label in labels:
            assert 0.0 <= label["cx_norm"] < 1.0
            assert 0.0 <= label["cy_norm"] < 1.0
            assert 0.0 < label["w_norm"] < 1.0
            assert 0.0 < label["h_norm"] < 1.0


class TestModuleHasNoHeavyImports:
    """Top-level imports must stay dep-light so PyInstaller doesn't pull
    torch / ultralytics / scipy into the bundled exes."""

    def test_no_torch_or_ultralytics_in_module(self):
        import sys

        # Reload the module to be sure we're inspecting fresh state.
        ball_detector_mod = sys.modules["video_grouper.inference.ball_detector"]
        attrs = set(dir(ball_detector_mod))
        assert "torch" not in attrs
        assert "ultralytics" not in attrs
        assert "filterpy" not in attrs
        assert "scipy" not in attrs
