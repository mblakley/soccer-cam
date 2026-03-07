"""Player tracking with persistent IDs using YOLO + ByteTrack.

Runs on downscaled full frames (not ROI crops) to maintain ID continuity
across the entire panorama. Produces per-player angular positions and
velocities for FOV computation.
"""

import logging
from dataclasses import dataclass

import cv2
import numpy as np

from video_grouper.ball_tracking.coordinates import (
    AngularPosition,
    AngularVelocity,
    CameraProfile,
    PixelPosition,
    pixel_bbox_to_angular_bbox,
    pixel_to_angular,
)

logger = logging.getLogger(__name__)

PERSON_CLASS_ID = 0


@dataclass
class TrackedPlayer:
    """A player detection with persistent tracking ID."""

    track_id: int
    center: AngularPosition
    pixel_center: PixelPosition
    confidence: float
    bbox_angular: tuple[float, float]  # (angular_width, angular_height)
    velocity: AngularVelocity | None


class PlayerTracker:
    """Full-frame player tracker using YOLO model.track() with ByteTrack.

    Unlike BallDetector (which runs on ROI crops), this runs on a downscaled
    full frame to maintain consistent tracking IDs across the panorama.
    """

    def __init__(
        self,
        model_path: str,
        profile: CameraProfile,
        confidence: float = 0.25,
        track_scale: float = 1.0,
        fps: float = 30.0,
    ):
        self._model_path = model_path
        self._model = None
        self.profile = profile
        self.confidence = confidence
        self.track_scale = track_scale
        self.fps = fps
        self._position_history: dict[int, AngularPosition] = {}
        self._last_frame_time: dict[int, float] = {}
        self._last_players: list[TrackedPlayer] = []
        self._frame_count = 0

    def _ensure_model(self):
        if self._model is None:
            from ultralytics import YOLO

            self._model = YOLO(str(self._model_path))
            logger.info("Loaded player tracker model from %s", self._model_path)

    def track_frame(self, frame: np.ndarray) -> list[TrackedPlayer]:
        """Run player tracking on a full frame.

        Downscales the frame, runs model.track() with ByteTrack for persistent
        IDs, and converts detections to angular coordinates with velocity.
        """
        self._ensure_model()
        frame_h, frame_w = frame.shape[:2]
        scaled_w = int(frame_w * self.track_scale)
        scaled_h = int(frame_h * self.track_scale)
        downscaled = cv2.resize(frame, (scaled_w, scaled_h))

        results = self._model.track(
            downscaled,
            conf=self.confidence,
            classes=[PERSON_CLASS_ID],
            persist=True,
            verbose=False,
        )

        players = []
        current_time = self._frame_count / self.fps

        for result in results:
            if result.boxes is None or result.boxes.id is None:
                continue

            for box, track_id_tensor in zip(result.boxes, result.boxes.id):
                track_id = int(track_id_tensor.item())
                x1, y1, x2, y2 = box.xyxy[0].tolist()

                # Map back to full-frame pixel coordinates
                scale_x = frame_w / scaled_w
                scale_y = frame_h / scaled_h
                det_cx = ((x1 + x2) / 2) * scale_x
                det_cy = ((y1 + y2) / 2) * scale_y
                det_w = (x2 - x1) * scale_x
                det_h = (y2 - y1) * scale_y

                pixel_center = PixelPosition(det_cx, det_cy)
                angular_center = pixel_to_angular(pixel_center, self.profile)
                _, angular_w, angular_h = pixel_bbox_to_angular_bbox(
                    det_cx, det_cy, det_w, det_h, self.profile
                )

                # Compute velocity from position history
                velocity = None
                if track_id in self._position_history:
                    prev = self._position_history[track_id]
                    prev_time = self._last_frame_time[track_id]
                    dt = current_time - prev_time
                    if dt > 0:
                        velocity = AngularVelocity(
                            vyaw=(angular_center.yaw - prev.yaw) / dt,
                            vpitch=(angular_center.pitch - prev.pitch) / dt,
                        )

                self._position_history[track_id] = angular_center
                self._last_frame_time[track_id] = current_time

                players.append(
                    TrackedPlayer(
                        track_id=track_id,
                        center=angular_center,
                        pixel_center=pixel_center,
                        confidence=float(box.conf[0]),
                        bbox_angular=(angular_w, angular_h),
                        velocity=velocity,
                    )
                )

        self._frame_count += 1
        self._last_players = players

        if players:
            logger.debug(
                "Tracked %d players (IDs: %s)",
                len(players),
                [p.track_id for p in players],
            )

        return players

    def get_last_players(self) -> list[TrackedPlayer]:
        """Return last known player positions for non-detection frames."""
        return self._last_players

    def reset(self):
        """Reset tracker state for a new video."""
        self._position_history.clear()
        self._last_frame_time.clear()
        self._last_players.clear()
        self._frame_count = 0
