"""Tests for player tracker with persistent IDs."""

from unittest.mock import MagicMock

import numpy as np
import pytest

from video_grouper.ball_tracking.coordinates import (
    AngularPosition,
    CameraProfile,
    PixelPosition,
)
from video_grouper.ball_tracking.player_tracker import PlayerTracker


class FakeTensor:
    """Lightweight mock that behaves like a torch tensor for test purposes."""

    def __init__(self, value):
        self._value = value

    def tolist(self):
        return list(self._value) if hasattr(self._value, "__iter__") else self._value

    def item(self):
        return self._value

    def __float__(self):
        return float(self._value)

    def __int__(self):
        return int(self._value)


@pytest.fixture
def profile():
    return CameraProfile.dahua_panoramic()


@pytest.fixture
def mock_model():
    return MagicMock()


@pytest.fixture
def tracker(mock_model, profile):
    """Create a PlayerTracker with a mocked YOLO model."""
    t = PlayerTracker(model_path="fake.pt", profile=profile, fps=30.0)
    t._model = mock_model  # inject mock directly, skip YOLO load
    return t


def _make_track_result(detections):
    """Create a mock model.track() result with boxes and track IDs.

    detections: list of (x1, y1, x2, y2, conf, track_id)
    """
    result = MagicMock()
    if not detections:
        result.boxes = MagicMock()
        result.boxes.id = None
        result.boxes.__iter__ = MagicMock(return_value=iter([]))
        return result

    boxes = []
    track_ids = []
    for x1, y1, x2, y2, conf, tid in detections:
        box = MagicMock()
        box.xyxy = [FakeTensor([x1, y1, x2, y2])]
        box.conf = [FakeTensor(conf)]
        boxes.append(box)
        track_ids.append(FakeTensor(tid))

    result.boxes = MagicMock()
    result.boxes.id = track_ids
    result.boxes.__iter__ = MagicMock(return_value=iter(boxes))

    return result


class TestPlayerTracker:
    def test_track_frame_returns_players(self, tracker, mock_model):
        """Basic tracking returns TrackedPlayer objects."""
        frame = np.zeros((1800, 4096, 3), dtype=np.uint8)

        result = _make_track_result([(900, 400, 1000, 500, 0.9, 1)])
        mock_model.track.return_value = [result]

        players = tracker.track_frame(frame)

        assert len(players) == 1
        assert players[0].track_id == 1
        assert players[0].confidence == pytest.approx(0.9, abs=0.01)
        assert isinstance(players[0].center, AngularPosition)
        assert isinstance(players[0].pixel_center, PixelPosition)
        assert players[0].velocity is None  # first frame, no history

    def test_track_frame_computes_velocity(self, tracker, mock_model):
        """Velocity is computed from position history on second detection."""
        frame = np.zeros((1800, 4096, 3), dtype=np.uint8)

        # Frame 0: player at x=900
        result1 = _make_track_result([(900, 400, 1000, 500, 0.9, 1)])
        mock_model.track.return_value = [result1]
        tracker.track_frame(frame)

        # Frame 1: same player moved to x=1100
        result2 = _make_track_result([(1100, 400, 1200, 500, 0.9, 1)])
        mock_model.track.return_value = [result2]
        players = tracker.track_frame(frame)

        assert len(players) == 1
        assert players[0].track_id == 1
        assert players[0].velocity is not None
        assert players[0].velocity.vyaw != 0

    def test_track_frame_multiple_players(self, tracker, mock_model):
        """Multiple players get distinct track IDs."""
        frame = np.zeros((1800, 4096, 3), dtype=np.uint8)

        result = _make_track_result(
            [
                (100, 400, 200, 500, 0.9, 1),
                (500, 400, 600, 500, 0.85, 2),
                (900, 400, 1000, 500, 0.7, 3),
            ]
        )
        mock_model.track.return_value = [result]

        players = tracker.track_frame(frame)

        assert len(players) == 3
        track_ids = {p.track_id for p in players}
        assert track_ids == {1, 2, 3}

    def test_get_last_players_returns_cached(self, tracker, mock_model):
        """get_last_players returns the previous detection results."""
        frame = np.zeros((1800, 4096, 3), dtype=np.uint8)

        result = _make_track_result([(900, 400, 1000, 500, 0.9, 1)])
        mock_model.track.return_value = [result]
        tracker.track_frame(frame)

        last = tracker.get_last_players()
        assert len(last) == 1
        assert last[0].track_id == 1

    def test_get_last_players_empty_initially(self, tracker):
        """get_last_players returns empty list before any tracking."""
        assert tracker.get_last_players() == []

    def test_track_frame_no_detections(self, tracker, mock_model):
        """Handles frames with no person detections."""
        frame = np.zeros((1800, 4096, 3), dtype=np.uint8)

        result = _make_track_result([])
        mock_model.track.return_value = [result]

        players = tracker.track_frame(frame)
        assert players == []

    def test_track_frame_calls_model_with_correct_params(self, tracker, mock_model):
        """Verifies model.track() is called with correct parameters."""
        frame = np.zeros((1800, 4096, 3), dtype=np.uint8)

        result = _make_track_result([])
        mock_model.track.return_value = [result]
        tracker.track_frame(frame)

        mock_model.track.assert_called_once()
        call_kwargs = mock_model.track.call_args
        assert call_kwargs.kwargs["classes"] == [0]
        assert call_kwargs.kwargs["persist"] is True
        assert call_kwargs.kwargs["verbose"] is False

    def test_track_frame_downscales(self, mock_model, profile):
        """Verifies frame is downscaled when track_scale < 1."""
        t = PlayerTracker(
            model_path="fake.pt", profile=profile, fps=30.0, track_scale=0.5
        )
        t._model = mock_model
        frame = np.zeros((1800, 4096, 3), dtype=np.uint8)

        result = _make_track_result([])
        mock_model.track.return_value = [result]
        t.track_frame(frame)

        # The first positional arg should be the downscaled frame
        call_args = mock_model.track.call_args
        downscaled = call_args[0][0]
        assert downscaled.shape[1] == 2048  # 4096 * 0.5
        assert downscaled.shape[0] == 900  # 1800 * 0.5

    def test_track_frame_full_res_by_default(self, tracker, mock_model):
        """Default track_scale=1.0 passes full-res frame."""
        frame = np.zeros((1800, 4096, 3), dtype=np.uint8)

        result = _make_track_result([])
        mock_model.track.return_value = [result]
        tracker.track_frame(frame)

        call_args = mock_model.track.call_args
        passed_frame = call_args[0][0]
        assert passed_frame.shape[1] == 4096
        assert passed_frame.shape[0] == 1800

    def test_bbox_angular_computed(self, tracker, mock_model):
        """Bounding box angular dimensions are computed."""
        frame = np.zeros((1800, 4096, 3), dtype=np.uint8)

        # 100x100 pixel box in downscaled coords -> 200x200 in full frame
        result = _make_track_result([(450, 400, 550, 500, 0.9, 1)])
        mock_model.track.return_value = [result]

        players = tracker.track_frame(frame)
        assert len(players) == 1
        angular_w, angular_h = players[0].bbox_angular
        assert angular_w > 0
        assert angular_h > 0

    def test_reset_clears_state(self, tracker, mock_model):
        """Reset clears position history and cached players."""
        frame = np.zeros((1800, 4096, 3), dtype=np.uint8)

        result = _make_track_result([(900, 400, 1000, 500, 0.9, 1)])
        mock_model.track.return_value = [result]
        tracker.track_frame(frame)

        tracker.reset()
        assert tracker.get_last_players() == []
