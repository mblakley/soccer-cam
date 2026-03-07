"""Stage A: Low-res motion proposals for ball candidate generation.

Runs every frame at quarter resolution. Pure OpenCV math, no ML.
Finds motion blobs and estimates the play region centroid.
"""

import cv2
import numpy as np

from video_grouper.ball_tracking.coordinates import (
    AngularPosition,
    CameraProfile,
    PixelPosition,
    pixel_to_angular,
)


class CandidateROI:
    """A candidate region of interest for ball detection."""

    def __init__(
        self,
        center: AngularPosition,
        pixel_rect: tuple[int, int, int, int],
        motion_score: float,
    ):
        self.center = center
        self.pixel_rect = pixel_rect  # (x, y, w, h) in full-res coordinates
        self.motion_score = motion_score


class MotionProposals:
    """Result of motion analysis for a single frame."""

    def __init__(
        self,
        play_region: AngularPosition,
        candidates: list[CandidateROI],
        has_motion: bool,
    ):
        self.play_region = play_region
        self.candidates = candidates
        self.has_motion = has_motion


class CandidateGenerator:
    """Generate ball candidate ROIs from low-resolution motion analysis.

    Runs frame differencing at quarter resolution, finds connected components,
    and estimates the play region from the largest motion cluster.
    """

    def __init__(
        self,
        profile: CameraProfile,
        scale_factor: float = 0.25,
        diff_threshold: int = 25,
        min_blob_area: int = 20,
        max_candidates: int = 15,
        roi_size: int = 400,
    ):
        self.profile = profile
        self.scale_factor = scale_factor
        self.diff_threshold = diff_threshold
        self.min_blob_area = min_blob_area
        self.max_candidates = max_candidates
        self.roi_size = roi_size  # ROI size in full-res pixels

        self._prev_gray = None

    def reset(self):
        """Reset state for a new video."""
        self._prev_gray = None

    def process_frame(self, frame: np.ndarray) -> MotionProposals:
        """Analyze a frame and return motion-based candidate ROIs.

        Args:
            frame: Full-resolution BGR frame (4096x1800)

        Returns:
            MotionProposals with play region and candidate ROIs
        """
        h, w = frame.shape[:2]
        small_w = int(w * self.scale_factor)
        small_h = int(h * self.scale_factor)
        small = cv2.resize(frame, (small_w, small_h))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

        if self._prev_gray is None:
            self._prev_gray = gray
            center = pixel_to_angular(PixelPosition(w / 2, h / 2), self.profile)
            return MotionProposals(play_region=center, candidates=[], has_motion=False)

        # Frame differencing
        diff = cv2.absdiff(gray, self._prev_gray)
        self._prev_gray = gray

        _, thresh = cv2.threshold(diff, self.diff_threshold, 255, cv2.THRESH_BINARY)

        # Morphological cleanup
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        thresh = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel)
        thresh = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)

        # Connected components
        num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(thresh)

        # Filter by area and sort by motion score (area)
        blobs = []
        for i in range(1, num_labels):  # skip background (label 0)
            area = stats[i, cv2.CC_STAT_AREA]
            if area >= self.min_blob_area:
                cx_small, cy_small = centroids[i]
                # Scale back to full resolution
                cx_full = cx_small / self.scale_factor
                cy_full = cy_small / self.scale_factor
                blobs.append((cx_full, cy_full, float(area)))

        blobs.sort(key=lambda b: b[2], reverse=True)

        # Play region = weighted centroid of all motion
        if blobs:
            total_weight = sum(b[2] for b in blobs)
            play_x = sum(b[0] * b[2] for b in blobs) / total_weight
            play_y = sum(b[1] * b[2] for b in blobs) / total_weight
            play_region = pixel_to_angular(PixelPosition(play_x, play_y), self.profile)
        else:
            play_region = pixel_to_angular(PixelPosition(w / 2, h / 2), self.profile)

        # Build candidate ROIs
        candidates = []
        half_roi = self.roi_size // 2
        for cx, cy, score in blobs[: self.max_candidates]:
            x = max(0, int(cx - half_roi))
            y = max(0, int(cy - half_roi))
            roi_w = min(self.roi_size, w - x)
            roi_h = min(self.roi_size, h - y)

            center = pixel_to_angular(PixelPosition(cx, cy), self.profile)
            candidates.append(
                CandidateROI(
                    center=center, pixel_rect=(x, y, roi_w, roi_h), motion_score=score
                )
            )

        return MotionProposals(
            play_region=play_region,
            candidates=candidates,
            has_motion=len(blobs) > 0,
        )
