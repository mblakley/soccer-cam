"""Stage B: Tiny ROI ball detector using YOLO26n.

Runs the trained model on a small number of ROI crops (1-4 per frame),
not the entire panoramic frame. Adaptive detection frequency based on
tracker confidence.
"""

import logging
from pathlib import Path

import cv2
import numpy as np

from video_grouper.ball_tracking.coordinates import (
    AngularPosition,
    CameraProfile,
    PixelPosition,
    pixel_to_angular,
)

logger = logging.getLogger(__name__)


class Detection:
    """A ball detection from the YOLO model."""

    def __init__(
        self,
        center: AngularPosition,
        pixel_center: PixelPosition,
        confidence: float,
        bbox_wh: tuple[float, float],
    ):
        self.center = center
        self.pixel_center = pixel_center
        self.confidence = confidence
        self.bbox_wh = bbox_wh  # (width, height) in pixels


class BallDetector:
    """YOLO26n ball detector that runs on ROI crops.

    The detector does NOT run on the whole frame. It receives a list of
    ROI rectangles from the candidate generator and tracker, crops them
    from the full-res frame, and runs YOLO on each crop.
    """

    def __init__(
        self,
        model_path: Path,
        profile: CameraProfile,
        confidence: float = 0.25,
        imgsz: int = 640,
        device: str = "cpu",
        model=None,
    ):
        self.profile = profile
        self.confidence = confidence
        self.imgsz = imgsz
        self._model = model
        self._model_path = model_path
        self._device = device

    @property
    def model(self):
        """Expose the loaded YOLO model for sharing with other detectors."""
        self._ensure_model()
        return self._model

    def _ensure_model(self):
        if self._model is None:
            from ultralytics import YOLO

            self._model = YOLO(str(self._model_path))
            logger.info("Loaded ball detector model from %s", self._model_path)

    def detect_in_rois(
        self,
        frame: np.ndarray,
        rois: list[tuple[int, int, int, int]],
    ) -> list[Detection]:
        """Run ball detection on a list of ROI crops.

        Args:
            frame: Full-resolution BGR frame
            rois: List of (x, y, w, h) rectangles in pixel coordinates

        Returns:
            List of Detection objects in canonical angular coordinates
        """
        self._ensure_model()

        all_detections = []
        frame_h, frame_w = frame.shape[:2]

        for roi_x, roi_y, roi_w, roi_h in rois:
            # Clip ROI to frame bounds
            roi_x = max(0, roi_x)
            roi_y = max(0, roi_y)
            roi_w = min(roi_w, frame_w - roi_x)
            roi_h = min(roi_h, frame_h - roi_y)

            if roi_w <= 0 or roi_h <= 0:
                continue

            crop = frame[roi_y : roi_y + roi_h, roi_x : roi_x + roi_w]
            resized = cv2.resize(crop, (self.imgsz, self.imgsz))

            results = self._model(resized, conf=self.confidence, verbose=False)

            for result in results:
                for box in result.boxes:
                    # Map detection back to full-frame coordinates
                    x1, y1, x2, y2 = box.xyxy[0].tolist()
                    scale_x = roi_w / self.imgsz
                    scale_y = roi_h / self.imgsz

                    det_cx = roi_x + ((x1 + x2) / 2) * scale_x
                    det_cy = roi_y + ((y1 + y2) / 2) * scale_y
                    det_w = (x2 - x1) * scale_x
                    det_h = (y2 - y1) * scale_y

                    pixel_center = PixelPosition(det_cx, det_cy)
                    angular_center = pixel_to_angular(pixel_center, self.profile)

                    all_detections.append(
                        Detection(
                            center=angular_center,
                            pixel_center=pixel_center,
                            confidence=float(box.conf[0]),
                            bbox_wh=(det_w, det_h),
                        )
                    )

        return all_detections

    def build_roi_around(
        self, center: AngularPosition, size: int = 400
    ) -> tuple[int, int, int, int]:
        """Build a pixel ROI rectangle centered on an angular position."""
        from video_grouper.ball_tracking.coordinates import angular_to_pixel

        pixel = angular_to_pixel(center, self.profile)
        half = size // 2
        x = max(0, int(pixel.x - half))
        y = max(0, int(pixel.y - half))
        w = min(size, self.profile.width - x)
        h = min(size, self.profile.height - y)
        return (x, y, w, h)
