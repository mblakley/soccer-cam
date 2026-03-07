"""Conservative confidence-driven virtual PTZ camera controller.

Key principle: a stable camera on the wrong spot is better than a
jittery camera chasing noise. The play region is always a reasonable
thing to show the viewer.
"""

import math
from dataclasses import dataclass

from video_grouper.ball_tracking.coordinates import AngularPosition


@dataclass
class CameraState:
    """Virtual camera state for a single frame."""

    yaw: float
    pitch: float
    fov: float  # field of view in radians
    frame_idx: int


class CameraController:
    """Confidence-driven virtual PTZ camera controller.

    High confidence (>0.7): follow ball with lead, normal FOV
    Medium confidence (0.3-0.7): blend ball + play region, widen FOV
    Low confidence (<0.3): follow play region, wide FOV, max smoothing
    """

    def __init__(
        self,
        base_fov: float = math.radians(60),
        wide_fov: float = math.radians(80),
        smooth_alpha_high: float = 0.85,
        smooth_alpha_medium: float = 0.95,
        smooth_alpha_low: float = 0.98,
        lead_factor: float = 0.3,
        lock_threshold: float = 0.7,
        blend_threshold: float = 0.3,
    ):
        self.base_fov = base_fov
        self.wide_fov = wide_fov
        self.smooth_alpha_high = smooth_alpha_high
        self.smooth_alpha_medium = smooth_alpha_medium
        self.smooth_alpha_low = smooth_alpha_low
        self.lead_factor = lead_factor
        self.lock_threshold = lock_threshold
        self.blend_threshold = blend_threshold

        self._yaw = 0.0
        self._pitch = 0.0
        self._fov = base_fov
        self._frame_idx = 0

    def reset(self):
        """Reset for a new video."""
        self._yaw = 0.0
        self._pitch = 0.0
        self._fov = self.base_fov
        self._frame_idx = 0

    def update(
        self,
        target: AngularPosition,
        confidence: float,
        velocity_yaw: float = 0.0,
        velocity_pitch: float = 0.0,
    ) -> CameraState:
        """Compute the virtual camera position for this frame.

        Args:
            target: Blended target position from tracker (already ball/play/blend)
            confidence: Tracker confidence (0-1)
            velocity_yaw: Ball velocity in yaw (rad/s) for lead calculation
            velocity_pitch: Ball velocity in pitch (rad/s) for lead calculation

        Returns:
            CameraState for this frame
        """
        # Compute target with lead (only when confident)
        if confidence >= self.lock_threshold:
            target_yaw = target.yaw + velocity_yaw * self.lead_factor
            target_pitch = target.pitch + velocity_pitch * self.lead_factor
            alpha = self.smooth_alpha_high
            target_fov = self.base_fov
        elif confidence >= self.blend_threshold:
            target_yaw = target.yaw
            target_pitch = target.pitch
            alpha = self.smooth_alpha_medium
            blend = (confidence - self.blend_threshold) / (
                self.lock_threshold - self.blend_threshold
            )
            target_fov = self.wide_fov + (self.base_fov - self.wide_fov) * blend
        else:
            target_yaw = target.yaw
            target_pitch = target.pitch
            alpha = self.smooth_alpha_low
            target_fov = self.wide_fov

        # Exponential smoothing
        self._yaw = self._yaw * alpha + target_yaw * (1 - alpha)
        self._pitch = self._pitch * alpha + target_pitch * (1 - alpha)
        self._fov = self._fov * alpha + target_fov * (1 - alpha)

        self._frame_idx += 1

        return CameraState(
            yaw=self._yaw,
            pitch=self._pitch,
            fov=self._fov,
            frame_idx=self._frame_idx,
        )
