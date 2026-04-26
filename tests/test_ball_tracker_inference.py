"""Tests for video_grouper.inference.ball_tracker."""

from __future__ import annotations

from video_grouper.inference.ball_tracker import BallTracker, Detection


class TestStraightLineBall:
    """A ball moving in a straight line should produce one valid track."""

    def _build_detections(self, n_frames: int = 20) -> list[list[Detection]]:
        # Ball moves +10 px/frame in x, +5 px/frame in y, starting at (100, 200).
        return [
            [
                Detection(
                    x=100.0 + 10.0 * frame,
                    y=200.0 + 5.0 * frame,
                    confidence=0.8,
                    frame_idx=frame,
                )
            ]
            for frame in range(n_frames)
        ]

    def test_single_track_emerges(self):
        tracker = BallTracker()
        for frame_idx, dets in enumerate(self._build_detections()):
            tracker.update(frame_idx, dets)

        tracks = tracker.get_tracks()
        assert len(tracks) == 1
        assert tracks[0].length == 20

    def test_best_track_returns_the_only_track(self):
        tracker = BallTracker()
        for frame_idx, dets in enumerate(self._build_detections()):
            tracker.update(frame_idx, dets)

        best = tracker.get_best_track()
        assert best is not None
        assert best.length == 20

    def test_positions_match_input(self):
        tracker = BallTracker()
        for frame_idx, dets in enumerate(self._build_detections()):
            tracker.update(frame_idx, dets)

        best = tracker.get_best_track()
        assert best is not None
        # Detections are stored unmodified — the smoothed Kalman state is
        # separate from the raw observations.
        assert best.detections[0].x == 100.0
        assert best.detections[-1].x == 100.0 + 10.0 * 19


class TestTrackTermination:
    def test_track_dies_after_max_missing_frames(self):
        tracker = BallTracker(max_missing=3)
        # 5 frames of detections, then 5 frames of nothing.
        for frame_idx in range(5):
            tracker.update(
                frame_idx,
                [Detection(x=100.0, y=100.0, confidence=0.9, frame_idx=frame_idx)],
            )
        for frame_idx in range(5, 10):
            tracker.update(frame_idx, [])

        # The original track should be marked inactive after >3 missing frames.
        track = tracker.tracks[0]
        assert track.active is False


class TestEmptyInput:
    def test_no_detections_yields_no_tracks(self):
        tracker = BallTracker()
        for frame_idx in range(5):
            tracker.update(frame_idx, [])

        assert tracker.get_tracks() == []
        assert tracker.get_best_track() is None


class TestShortTrackFiltering:
    def test_min_track_length_filters_out_short_tracks(self):
        tracker = BallTracker(min_track_length=5, max_missing=0)
        # A 2-detection track that immediately dies (max_missing=0 means 1 miss kills it).
        tracker.update(0, [Detection(x=100.0, y=100.0, confidence=0.9, frame_idx=0)])
        tracker.update(1, [Detection(x=110.0, y=100.0, confidence=0.9, frame_idx=1)])
        for frame_idx in range(2, 10):
            tracker.update(frame_idx, [])

        # Track exists but is below min length, so get_tracks() filters it out.
        assert tracker.get_tracks() == []
        assert tracker.get_best_track() is None


class TestStatesContainVelocity:
    """The Track.states field captures full Kalman state per frame."""

    def test_states_grow_each_active_frame(self):
        tracker = BallTracker()
        # 8 frames of detections: state should grow to 8 entries.
        for frame_idx in range(8):
            tracker.update(
                frame_idx,
                [
                    Detection(
                        x=100.0 + 10.0 * frame_idx,
                        y=200.0,
                        confidence=0.9,
                        frame_idx=frame_idx,
                    )
                ],
            )

        best = tracker.get_best_track()
        assert best is not None
        assert len(best.states) == 8
        # Each state row is (frame_idx, x, y, vx, vy)
        for i, row in enumerate(best.states):
            assert row[0] == i
            assert len(row) == 5

    def test_velocity_converges_to_input(self):
        tracker = BallTracker()
        # Constant velocity: +10 px/frame in x, 0 in y. Kalman estimate
        # should converge near vx=10, vy=0 after several updates.
        for frame_idx in range(15):
            tracker.update(
                frame_idx,
                [
                    Detection(
                        x=100.0 + 10.0 * frame_idx,
                        y=500.0,
                        confidence=0.9,
                        frame_idx=frame_idx,
                    )
                ],
            )

        best = tracker.get_best_track()
        assert best is not None
        last_state = best.states[-1]
        _frame_idx, _x, _y, vx, vy = last_state
        # Constant-velocity Kalman should land within ~1 px/frame of truth.
        assert abs(vx - 10.0) < 1.0
        assert abs(vy) < 1.0
