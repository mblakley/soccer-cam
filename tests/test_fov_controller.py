"""Tests for FOV controller based on player spread."""

import math

import pytest

from video_grouper.ball_tracking.coordinates import AngularPosition, AngularVelocity
from video_grouper.ball_tracking.fov_controller import FovController
from video_grouper.ball_tracking.player_tracker import TrackedPlayer, PixelPosition


def _make_player(
    track_id: int, yaw: float, pitch: float, velocity_speed: float = 0.0
) -> TrackedPlayer:
    """Create a TrackedPlayer at given angular position."""
    velocity = None
    if velocity_speed > 0:
        velocity = AngularVelocity(vyaw=velocity_speed, vpitch=0.0)
    return TrackedPlayer(
        track_id=track_id,
        center=AngularPosition(yaw=yaw, pitch=pitch),
        pixel_center=PixelPosition(x=0, y=0),
        confidence=0.9,
        bbox_angular=(0.05, 0.1),
        velocity=velocity,
    )


@pytest.fixture
def ctrl():
    return FovController(
        min_fov_deg=25.0,
        max_fov_deg=60.0,
        default_fov_deg=45.0,
        padding=1.2,
        relevance_radius=0.5,
        smooth_alpha=0.0,  # No smoothing for easier testing
    )


@pytest.fixture
def ctrl_smoothed():
    return FovController(
        min_fov_deg=25.0,
        max_fov_deg=60.0,
        default_fov_deg=45.0,
        padding=1.2,
        smooth_alpha=0.9,
    )


class TestFovBasicComputation:
    def test_two_players_compute_fov(self, ctrl):
        """Two players spread apart produce FOV based on their spread."""
        ball = AngularPosition(yaw=0.0, pitch=0.0)
        players = [
            _make_player(1, yaw=-0.2, pitch=0.0),
            _make_player(2, yaw=0.2, pitch=0.0),
        ]
        state = ctrl.update(players, ball, ball, ball_confidence=0.8)

        # Spread = 0.4 rad, padded = 0.48 rad = ~27.5 deg
        expected_deg = math.degrees(0.4 * 1.2)
        assert state.target_fov_deg == pytest.approx(expected_deg, abs=1.0)
        assert state.active_player_count == 2

    def test_fewer_than_two_players_uses_default(self, ctrl):
        """With < 2 relevant players, use default FOV."""
        ball = AngularPosition(yaw=0.0, pitch=0.0)
        players = [_make_player(1, yaw=0.0, pitch=0.0)]
        state = ctrl.update(players, ball, ball, ball_confidence=0.8)

        assert state.target_fov_deg == 45.0

    def test_no_players_uses_default(self, ctrl):
        """With no players, use default FOV."""
        ball = AngularPosition(yaw=0.0, pitch=0.0)
        state = ctrl.update([], ball, ball, ball_confidence=0.8)

        assert state.target_fov_deg == 45.0
        assert state.active_player_count == 0

    def test_spread_yaw_and_pitch_reported(self, ctrl):
        """Spread values are reported in the state."""
        ball = AngularPosition(yaw=0.0, pitch=0.0)
        players = [
            _make_player(1, yaw=-0.15, pitch=-0.05),
            _make_player(2, yaw=0.15, pitch=0.05),
        ]
        state = ctrl.update(players, ball, ball, ball_confidence=0.8)

        assert state.spread_yaw == pytest.approx(0.3, abs=0.01)
        assert state.spread_pitch == pytest.approx(0.1, abs=0.01)


class TestFovClamping:
    def test_clamp_to_min(self, ctrl):
        """FOV is clamped to minimum when players are very close."""
        ball = AngularPosition(yaw=0.0, pitch=0.0)
        # Very close players -> tiny spread
        players = [
            _make_player(1, yaw=-0.01, pitch=0.0),
            _make_player(2, yaw=0.01, pitch=0.0),
        ]
        state = ctrl.update(players, ball, ball, ball_confidence=0.8)

        assert state.target_fov_deg >= 25.0

    def test_clamp_to_max(self, ctrl):
        """FOV is clamped to maximum when players are very spread."""
        ball = AngularPosition(yaw=0.0, pitch=0.0)
        # Very spread players
        players = [
            _make_player(1, yaw=-0.8, pitch=0.0, velocity_speed=0.1),
            _make_player(2, yaw=0.8, pitch=0.0, velocity_speed=0.1),
        ]
        state = ctrl.update(players, ball, ball, ball_confidence=0.8)

        assert state.target_fov_deg <= 60.0


class TestFovRelevanceFilter:
    def test_distant_players_excluded(self, ctrl):
        """Players beyond relevance radius are excluded."""
        ball = AngularPosition(yaw=0.0, pitch=0.0)
        players = [
            _make_player(1, yaw=0.1, pitch=0.0),  # near ball
            _make_player(2, yaw=1.5, pitch=0.0),  # far away (> 0.5 rad)
        ]
        state = ctrl.update(players, ball, ball, ball_confidence=0.8)

        # Only 1 relevant player -> default FOV
        assert state.target_fov_deg == 45.0

    def test_all_near_players_included(self, ctrl):
        """All players within radius are included."""
        ball = AngularPosition(yaw=0.0, pitch=0.0)
        players = [
            _make_player(1, yaw=-0.3, pitch=0.0),
            _make_player(2, yaw=0.3, pitch=0.0),
            _make_player(3, yaw=0.0, pitch=0.2),
        ]
        state = ctrl.update(players, ball, ball, ball_confidence=0.8)

        assert state.active_player_count == 3


class TestFovBallInclusion:
    def test_ball_expands_bbox(self, ctrl):
        """Ball position expands the bounding box when outside player cluster."""
        play_region = AngularPosition(yaw=0.0, pitch=0.0)

        # Players clustered left, ball kicked right
        players = [
            _make_player(1, yaw=-0.2, pitch=0.0),
            _make_player(2, yaw=-0.1, pitch=0.0),
        ]
        ball = AngularPosition(yaw=0.3, pitch=0.0)

        state = ctrl.update(players, ball, play_region, ball_confidence=0.8)

        # Spread should include ball at 0.3, so spread = 0.3 - (-0.2) = 0.5
        assert state.spread_yaw >= 0.4  # at least players-to-ball distance


class TestFovFocusPoint:
    def test_uses_ball_when_confident(self, ctrl):
        """Focus point is ball position when confidence is high."""
        ball = AngularPosition(yaw=0.5, pitch=0.0)
        play_region = AngularPosition(yaw=-0.5, pitch=0.0)

        # Players near ball, far from play_region
        players = [
            _make_player(1, yaw=0.3, pitch=0.0),
            _make_player(2, yaw=0.7, pitch=0.0),
        ]

        state = ctrl.update(players, ball, play_region, ball_confidence=0.8)

        # Should find 2 relevant players (near ball)
        assert state.active_player_count == 2

    def test_uses_play_region_when_low_confidence(self, ctrl):
        """Focus point falls back to play region when ball confidence is low."""
        ball = AngularPosition(yaw=0.5, pitch=0.0)
        play_region = AngularPosition(yaw=0.0, pitch=0.0)

        # Players near play_region, far from ball
        players = [
            _make_player(1, yaw=-0.1, pitch=0.0),
            _make_player(2, yaw=0.1, pitch=0.0),
        ]

        state = ctrl.update(players, ball, play_region, ball_confidence=0.1)

        # Should find 2 relevant players (near play_region)
        assert state.active_player_count == 2


class TestFovSmoothing:
    def test_ema_smoothing(self, ctrl_smoothed):
        """EMA smoothing prevents sudden FOV changes."""
        ball = AngularPosition(yaw=0.0, pitch=0.0)

        # First update with wide spread
        players_wide = [
            _make_player(1, yaw=-0.4, pitch=0.0),
            _make_player(2, yaw=0.4, pitch=0.0),
        ]
        state1 = ctrl_smoothed.update(players_wide, ball, ball, ball_confidence=0.8)

        # Smoothed should be between default and target (moving toward target)
        assert state1.smoothed_fov_deg != state1.target_fov_deg

    def test_predict_holds_last_state(self, ctrl):
        """predict() returns the last computed state."""
        ball = AngularPosition(yaw=0.0, pitch=0.0)
        players = [
            _make_player(1, yaw=-0.2, pitch=0.0),
            _make_player(2, yaw=0.2, pitch=0.0),
        ]
        state = ctrl.update(players, ball, ball, ball_confidence=0.8)
        predicted = ctrl.predict()

        assert predicted.smoothed_fov_deg == state.smoothed_fov_deg
        assert predicted.active_player_count == state.active_player_count


class TestFovVelocityWeighting:
    def test_moving_players_prioritized(self):
        """Moving players define the active zone over stationary ones."""
        ctrl = FovController(
            min_fov_deg=25.0,
            max_fov_deg=60.0,
            default_fov_deg=45.0,
            padding=1.2,
            smooth_alpha=0.0,
            velocity_threshold=0.02,
        )
        ball = AngularPosition(yaw=0.0, pitch=0.0)

        # 3 moving players clustered tight, 2 stationary ones spread wide
        players = [
            _make_player(1, yaw=-0.1, pitch=0.0, velocity_speed=0.05),
            _make_player(2, yaw=0.1, pitch=0.0, velocity_speed=0.05),
            _make_player(3, yaw=0.0, pitch=0.1, velocity_speed=0.05),
            _make_player(4, yaw=-0.4, pitch=0.0, velocity_speed=0.0),
            _make_player(5, yaw=0.4, pitch=0.0, velocity_speed=0.0),
        ]
        state = ctrl.update(players, ball, ball, ball_confidence=0.8)

        # Should use the 3 moving players, not the 2 stationary spread ones
        assert state.active_player_count == 3
        # Spread should be based on moving players (0.2 yaw spread, not 0.8)
        assert state.spread_yaw < 0.3


class TestFovReset:
    def test_reset_returns_to_default(self, ctrl):
        """Reset restores default FOV state."""
        ball = AngularPosition(yaw=0.0, pitch=0.0)
        players = [
            _make_player(1, yaw=-0.3, pitch=0.0),
            _make_player(2, yaw=0.3, pitch=0.0),
        ]
        ctrl.update(players, ball, ball, ball_confidence=0.8)
        ctrl.reset()

        state = ctrl.predict()
        assert state.smoothed_fov_deg == 45.0
        assert state.active_player_count == 0


class TestFovAspectRatio:
    def test_vertical_spread_scaled_by_aspect(self, ctrl):
        """Vertical spread is scaled by aspect ratio to determine FOV."""
        ball = AngularPosition(yaw=0.0, pitch=0.0)

        # Tall spread: pitch spread > yaw spread
        players = [
            _make_player(1, yaw=0.0, pitch=-0.3),
            _make_player(2, yaw=0.0, pitch=0.3),
        ]
        state = ctrl.update(players, ball, ball, ball_confidence=0.8)

        # pitch spread = 0.6, scaled by 16/9 * padding = 0.6 * 16/9 * 1.2 = ~1.28 rad
        # This exceeds max_fov, so should be clamped
        assert state.target_fov_deg == 60.0  # clamped to max
