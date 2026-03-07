"""Canonical yaw/pitch coordinate system for ball tracking.

All detection, tracking, and camera control operates in angular coordinates
(radians) so the same logic works for:
- Cylindrical 180-degree Dahua footage
- Future stitched equirectangular panorama
- Any camera resolution or crop
"""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class CameraProfile:
    """Camera projection parameters."""

    width: int
    height: int
    fov_h: float  # horizontal field of view in radians
    fov_v: float  # vertical field of view in radians

    @classmethod
    def dahua_panoramic(cls) -> "CameraProfile":
        """Dahua 180-degree panoramic camera (4096x1800, cylindrical projection)."""
        return cls(
            width=4096,
            height=1800,
            fov_h=math.pi,  # 180 degrees
            fov_v=math.pi * (80 / 180),  # ~80 degrees vertical
        )


@dataclass(frozen=True)
class AngularPosition:
    """Position in canonical yaw/pitch coordinates (radians).

    yaw: horizontal angle, 0 = center, negative = left, positive = right
    pitch: vertical angle, 0 = center, negative = up, positive = down
    """

    yaw: float
    pitch: float

    def distance_to(self, other: "AngularPosition") -> float:
        """Angular distance to another position (radians)."""
        return math.sqrt((self.yaw - other.yaw) ** 2 + (self.pitch - other.pitch) ** 2)


@dataclass(frozen=True)
class AngularVelocity:
    """Velocity in canonical coordinates (radians per second)."""

    vyaw: float
    vpitch: float

    @property
    def speed(self) -> float:
        """Angular speed magnitude (radians/second)."""
        return math.sqrt(self.vyaw**2 + self.vpitch**2)


@dataclass(frozen=True)
class PixelPosition:
    """Position in pixel coordinates."""

    x: float
    y: float


def pixel_to_angular(pixel: PixelPosition, profile: CameraProfile) -> AngularPosition:
    """Convert pixel coordinates to yaw/pitch angular coordinates.

    For cylindrical projection:
        yaw = (x / width) * fov_h - fov_h / 2
        pitch = (y / height) * fov_v - fov_v / 2
    """
    yaw = (pixel.x / profile.width) * profile.fov_h - profile.fov_h / 2
    pitch = (pixel.y / profile.height) * profile.fov_v - profile.fov_v / 2
    return AngularPosition(yaw=yaw, pitch=pitch)


def angular_to_pixel(angular: AngularPosition, profile: CameraProfile) -> PixelPosition:
    """Convert yaw/pitch angular coordinates to pixel coordinates.

    Inverse of pixel_to_angular for cylindrical projection.
    """
    x = (angular.yaw + profile.fov_h / 2) / profile.fov_h * profile.width
    y = (angular.pitch + profile.fov_v / 2) / profile.fov_v * profile.height
    return PixelPosition(x=x, y=y)


def pixel_bbox_to_angular_bbox(
    cx: float,
    cy: float,
    w: float,
    h: float,
    profile: CameraProfile,
) -> tuple[AngularPosition, float, float]:
    """Convert a pixel bounding box (center_x, center_y, width, height) to angular.

    Returns (center_angular, angular_width, angular_height).
    """
    center = pixel_to_angular(PixelPosition(cx, cy), profile)
    angular_w = (w / profile.width) * profile.fov_h
    angular_h = (h / profile.height) * profile.fov_v
    return center, angular_w, angular_h


def angular_bbox_to_pixel_bbox(
    center: AngularPosition,
    angular_w: float,
    angular_h: float,
    profile: CameraProfile,
) -> tuple[float, float, float, float]:
    """Convert angular bounding box to pixel (center_x, center_y, width, height)."""
    pixel_center = angular_to_pixel(center, profile)
    w = (angular_w / profile.fov_h) * profile.width
    h = (angular_h / profile.fov_v) * profile.height
    return pixel_center.x, pixel_center.y, w, h


def max_angular_velocity(
    max_speed_mps: float = 36.0, field_width_m: float = 100.0, fov_h: float = math.pi
) -> float:
    """Convert a real-world max ball speed to max angular velocity.

    Args:
        max_speed_mps: Maximum ball speed in meters/second (default 36 m/s = 130 km/h)
        field_width_m: Approximate field width visible in the frame (meters)
        fov_h: Horizontal field of view (radians)

    Returns:
        Maximum angular velocity in radians/second
    """
    # radians per meter at the field plane
    rad_per_meter = fov_h / field_width_m
    return max_speed_mps * rad_per_meter
