"""Stage C+D: Kalman tracker with strong physics gates and play-region fallback.

Runs every frame (pure math, <0.1ms). Maintains a single ball track with
confidence decay, physics constraints, and multi-frame confirmation.
The default mode is play-region following; ball detection upgrades this.
"""

import math

from filterpy.kalman import KalmanFilter
import numpy as np

from video_grouper.ball_tracking.coordinates import (
    AngularPosition,
    AngularVelocity,
    max_angular_velocity,
)


class TrackState:
    """State of the ball track at a single frame."""

    def __init__(
        self,
        position: AngularPosition,
        velocity: AngularVelocity,
        confidence: float,
        source: str,
        frame_idx: int,
    ):
        self.position = position
        self.velocity = velocity
        self.confidence = confidence
        self.source = source  # "ball", "play_region", or "blend"
        self.frame_idx = frame_idx


class BallTracker:
    """Kalman-based ball tracker with physics gates and play-region fallback.

    Design philosophy: "Follow play cheaply, lock ball when easy."
    The play region is the default; ball detection is an upgrade.
    """

    def __init__(
        self,
        fps: float = 30.0,
        max_speed_mps: float = 36.0,
        field_width_m: float = 100.0,
        fov_h: float = math.pi,
        confidence_decay: float = 0.92,
        confirmation_frames: int = 3,
        lock_threshold: float = 0.7,
        blend_threshold: float = 0.3,
    ):
        self.fps = fps
        self.dt = 1.0 / fps
        self.max_angular_vel = max_angular_velocity(max_speed_mps, field_width_m, fov_h)
        self.confidence_decay = confidence_decay
        self.confirmation_frames = confirmation_frames
        self.lock_threshold = lock_threshold
        self.blend_threshold = blend_threshold

        # Kalman filter: state = [yaw, pitch, vyaw, vpitch, ayaw, apitch]
        self._kf = self._init_kalman()
        self._confidence = 0.0
        self._frames_since_detection = 0
        self._candidate_buffer: list[AngularPosition] = []
        self._confirmed = False
        self._frame_idx = 0

    def _init_kalman(self) -> KalmanFilter:
        kf = KalmanFilter(dim_x=6, dim_z=2)
        dt = self.dt

        # State transition: constant acceleration model
        kf.F = np.array(
            [
                [1, 0, dt, 0, 0.5 * dt**2, 0],
                [0, 1, 0, dt, 0, 0.5 * dt**2],
                [0, 0, 1, 0, dt, 0],
                [0, 0, 0, 1, 0, dt],
                [0, 0, 0, 0, 1, 0],
                [0, 0, 0, 0, 0, 1],
            ],
            dtype=float,
        )

        # Measurement: we observe [yaw, pitch]
        kf.H = np.array(
            [
                [1, 0, 0, 0, 0, 0],
                [0, 1, 0, 0, 0, 0],
            ],
            dtype=float,
        )

        # Process noise
        q = 0.01
        kf.Q = np.eye(6) * q
        kf.Q[0, 0] = q * 0.1
        kf.Q[1, 1] = q * 0.1
        kf.Q[4, 4] = q * 10
        kf.Q[5, 5] = q * 10

        # Measurement noise
        kf.R = np.eye(2) * 0.001

        # Initial covariance
        kf.P *= 10.0

        return kf

    def reset(self):
        """Reset tracker for a new video."""
        self._kf = self._init_kalman()
        self._confidence = 0.0
        self._frames_since_detection = 0
        self._candidate_buffer = []
        self._confirmed = False
        self._frame_idx = 0

    @property
    def position(self) -> AngularPosition:
        return AngularPosition(
            yaw=float(self._kf.x[0].item()), pitch=float(self._kf.x[1].item())
        )

    @property
    def velocity(self) -> AngularVelocity:
        return AngularVelocity(
            vyaw=float(self._kf.x[2].item()), vpitch=float(self._kf.x[3].item())
        )

    @property
    def confidence(self) -> float:
        return self._confidence

    @property
    def is_locked(self) -> bool:
        return self._confirmed and self._confidence >= self.lock_threshold

    def _passes_physics_gate(self, detection: AngularPosition) -> bool:
        """Check if a detection is physically plausible given current state."""
        if not self._confirmed:
            return True  # no prior to check against

        predicted = self.position
        distance = predicted.distance_to(detection)
        max_distance = self.max_angular_vel * self.dt * 3  # 3x tolerance

        if distance > max_distance:
            return False

        # Direction continuity: reject 180-degree reversals without deceleration
        if self.velocity.speed > 0.01:
            dx = detection.yaw - predicted.yaw
            dy = detection.pitch - predicted.pitch
            dot = dx * self.velocity.vyaw + dy * self.velocity.vpitch
            if dot < -0.5 * self.velocity.speed * distance and distance > 0:
                return False

        return True

    def predict(self):
        """Predict step -- run every frame regardless of detection."""
        self._kf.predict()
        self._frames_since_detection += 1
        self._confidence *= self.confidence_decay
        self._confidence = max(0.0, self._confidence)
        self._frame_idx += 1

    def update(self, detection: AngularPosition, det_confidence: float):
        """Update step -- called when detector provides a measurement.

        Applies physics gates and multi-frame confirmation before accepting.
        """
        if not self._passes_physics_gate(detection):
            return

        if not self._confirmed:
            # Multi-frame confirmation: buffer candidates
            self._candidate_buffer.append(detection)
            if len(self._candidate_buffer) >= self.confirmation_frames:
                # Check if candidates are spatially consistent
                max_spread = max(
                    self._candidate_buffer[0].distance_to(c)
                    for c in self._candidate_buffer[1:]
                )
                if (
                    max_spread
                    < self.max_angular_vel * self.dt * self.confirmation_frames * 2
                ):
                    self._confirmed = True
                    avg_yaw = sum(c.yaw for c in self._candidate_buffer) / len(
                        self._candidate_buffer
                    )
                    avg_pitch = sum(c.pitch for c in self._candidate_buffer) / len(
                        self._candidate_buffer
                    )
                    self._kf.x[0] = avg_yaw
                    self._kf.x[1] = avg_pitch
                    self._confidence = det_confidence
                else:
                    self._candidate_buffer = [detection]
            return

        # Normal update
        z = np.array([detection.yaw, detection.pitch])
        self._kf.update(z)
        self._frames_since_detection = 0
        self._confidence = min(1.0, self._confidence + (1.0 - self._confidence) * 0.3)
        self._confidence = max(self._confidence, det_confidence)

    def get_state(self, play_region: AngularPosition) -> TrackState:
        """Get the current track state, blending with play region based on confidence.

        This is the key output: when confident, follow ball; when not, follow play region.
        """
        ball_pos = self.position

        if self._confidence >= self.lock_threshold:
            source = "ball"
            output = ball_pos
        elif self._confidence >= self.blend_threshold:
            source = "blend"
            alpha = (self._confidence - self.blend_threshold) / (
                self.lock_threshold - self.blend_threshold
            )
            output = AngularPosition(
                yaw=ball_pos.yaw * alpha + play_region.yaw * (1 - alpha),
                pitch=ball_pos.pitch * alpha + play_region.pitch * (1 - alpha),
            )
        else:
            source = "play_region"
            output = play_region

        return TrackState(
            position=output,
            velocity=self.velocity,
            confidence=self._confidence,
            source=source,
            frame_idx=self._frame_idx,
        )

    @property
    def detection_frequency(self) -> int:
        """Suggested detection frequency (run detector every N frames).

        High confidence: every 5th frame (tracker fills gaps)
        Medium: every 2nd frame
        Low/lost: every frame
        """
        if self._confidence >= self.lock_threshold:
            return 5
        elif self._confidence >= self.blend_threshold:
            return 2
        return 1

    @property
    def max_rois(self) -> int:
        """Suggested max ROIs to search per detection frame.

        High confidence: 1-2 (tracker prior + 1 motion)
        Medium: 3-4
        Low/lost: all motion candidates + play region
        """
        if self._confidence >= self.lock_threshold:
            return 2
        elif self._confidence >= self.blend_threshold:
            return 4
        return 8
