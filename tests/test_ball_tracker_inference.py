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


class TestBuildTrajectory:
    """build_trajectory stitches the ball's gated track fragments into one trajectory while
    dropping long stationary false-positive tracks (sprinkler / standing bystander)."""

    def test_single_moving_ball_full_coverage(self):
        tracker = BallTracker()
        for f in range(20):
            tracker.update(f, [Detection(100.0 + 10.0 * f, 200.0, 0.9, f)])
        traj = tracker.build_trajectory(20)
        assert all(p is not None for p in traj)
        assert traj[0] == [100.0, 200.0]

    def test_stitches_fragments_and_drops_stationary_fp(self):
        # A moving ball (lost frames 12-15, so it gates into two tracks at max_missing=2) plus a
        # STATIONARY false positive present every frame off to the side.
        tracker = BallTracker(gate_distance=200, max_missing=2)
        n = 30
        for f in range(n):
            dets = [Detection(1000.0, 1000.0, 0.9, f)]  # stationary FP
            if not (12 <= f <= 15):
                dets.append(Detection(100.0 + 20.0 * f, 200.0, 0.9, f))  # moving ball
            tracker.update(f, dets)
        traj = tracker.build_trajectory(n, move_px=80, stationary_len=10, interp_gap=16)
        cov = sum(1 for p in traj if p is not None)
        assert cov >= 26  # both ball fragments stitched + the short gap interpolated
        for p in traj:  # never follows the stationary FP at x=1000
            if p is not None:
                assert p[0] < 900


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
