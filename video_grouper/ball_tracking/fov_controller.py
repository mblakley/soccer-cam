"""Dynamic FOV controller based on player spread.

Determines zoom level by computing the angular bounding box of active players
near the ball/play region, following the Veo/Trace/Hudl pattern:
- Compact play (corner kick) -> zoom in
- Spread play (transition) -> zoom out
- Smooth transitions, never snap zoom
"""

import math
from dataclasses import dataclass

from video_grouper.ball_tracking.coordinates import AngularPosition
from video_grouper.ball_tracking.player_tracker import TrackedPlayer


@dataclass
class FovState:
    """FOV computation result for a single frame."""

    target_fov_deg: float
    smoothed_fov_deg: float
    active_player_count: int
    spread_yaw: float  # angular spread in radians
    spread_pitch: float


class FovController:
    """Computes dynamic FOV from tracked player positions.

    Filters players to those near the focus point (ball or play region),
    computes their angular bounding box, and smoothly adjusts FOV to
    frame the active play area with padding.
    """

    def __init__(
        self,
        min_fov_deg: float = 25.0,
        max_fov_deg: float = 60.0,
        default_fov_deg: float = 45.0,
        padding: float = 1.2,
        relevance_radius: float = 0.5,
        smooth_alpha: float = 0.95,
        aspect_ratio: float = 16 / 9,
        velocity_threshold: float = 0.02,
        ball_confidence_threshold: float = 0.3,
    ):
        self.min_fov_deg = min_fov_deg
        self.max_fov_deg = max_fov_deg
        self.default_fov_deg = default_fov_deg
        self.padding = padding
        self.relevance_radius = relevance_radius
        self.smooth_alpha = smooth_alpha
        self.aspect_ratio = aspect_ratio
        self.velocity_threshold = velocity_threshold
        self.ball_confidence_threshold = ball_confidence_threshold

        self._smoothed_fov = math.radians(default_fov_deg)
        self._last_state = FovState(
            target_fov_deg=default_fov_deg,
            smoothed_fov_deg=default_fov_deg,
            active_player_count=0,
            spread_yaw=0.0,
            spread_pitch=0.0,
        )

    def update(
        self,
        players: list[TrackedPlayer],
        ball_position: AngularPosition | None,
        play_region: AngularPosition,
        ball_confidence: float,
    ) -> FovState:
        """Compute FOV from current player positions.

        Args:
            players: Tracked players with positions and velocities
            ball_position: Ball angular position (None if not tracked)
            play_region: Motion-based play region centroid
            ball_confidence: Ball tracker confidence (0-1)
        """
        # Determine focus point
        if ball_position and ball_confidence >= self.ball_confidence_threshold:
            focus = ball_position
        else:
            focus = play_region

        # Filter to relevant players near focus
        relevant = [
            p for p in players if focus.distance_to(p.center) <= self.relevance_radius
        ]

        # Prioritize moving players when we have velocity data
        active = [
            p
            for p in relevant
            if p.velocity is not None and p.velocity.speed >= self.velocity_threshold
        ]
        # Fall back to all relevant players if few are moving
        if len(active) < 2:
            active = relevant

        if len(active) >= 2:
            # Compute angular bounding box of active players
            yaws = [p.center.yaw for p in active]
            pitches = [p.center.pitch for p in active]

            min_yaw, max_yaw = min(yaws), max(yaws)
            min_pitch, max_pitch = min(pitches), max(pitches)

            # Expand to include ball if tracked
            if ball_position and ball_confidence >= self.ball_confidence_threshold:
                min_yaw = min(min_yaw, ball_position.yaw)
                max_yaw = max(max_yaw, ball_position.yaw)
                min_pitch = min(min_pitch, ball_position.pitch)
                max_pitch = max(max_pitch, ball_position.pitch)

            spread_yaw = max_yaw - min_yaw
            spread_pitch = max_pitch - min_pitch

            # Apply padding
            padded_yaw = spread_yaw * self.padding
            padded_pitch = spread_pitch * self.padding

            # FOV = max of horizontal spread and vertical spread scaled by aspect ratio
            target_fov_rad = max(padded_yaw, padded_pitch * self.aspect_ratio)
            target_fov_deg = math.degrees(target_fov_rad)
        else:
            spread_yaw = 0.0
            spread_pitch = 0.0
            target_fov_deg = self.default_fov_deg

        # Clamp
        target_fov_deg = max(self.min_fov_deg, min(self.max_fov_deg, target_fov_deg))

        # EMA smooth
        target_fov_rad = math.radians(target_fov_deg)
        self._smoothed_fov = self._smoothed_fov * self.smooth_alpha + target_fov_rad * (
            1 - self.smooth_alpha
        )
        smoothed_deg = math.degrees(self._smoothed_fov)
        smoothed_deg = max(self.min_fov_deg, min(self.max_fov_deg, smoothed_deg))

        self._last_state = FovState(
            target_fov_deg=target_fov_deg,
            smoothed_fov_deg=smoothed_deg,
            active_player_count=len(active),
            spread_yaw=spread_yaw,
            spread_pitch=spread_pitch,
        )
        return self._last_state

    def predict(self) -> FovState:
        """Return last FOV state for non-detection frames."""
        return self._last_state

    def reset(self):
        """Reset for a new video."""
        self._smoothed_fov = math.radians(self.default_fov_deg)
        self._last_state = FovState(
            target_fov_deg=self.default_fov_deg,
            smoothed_fov_deg=self.default_fov_deg,
            active_player_count=0,
            spread_yaw=0.0,
            spread_pitch=0.0,
        )
