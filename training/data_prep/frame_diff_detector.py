"""Frame differencing ball detector.

Detects moving objects by computing the absolute difference between
consecutive frames. The ball creates a bright spot in the difference
image even when it's only 8-12 pixels, because it moves against the
static grass background.

This is a complementary detector to YOLO — it catches balls that are
too small for the CNN but are clearly moving.

Usage:
    python -m training.data_prep.frame_diff_detector \
        --video F:/training_data/temp_video/18.02.52-18.19.36*.mp4 \
        --output F:/training_data/diff_detections.json
"""

import argparse
import json
import logging
import time
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# Detection parameters
MIN_BLOB_AREA = 20  # Minimum blob area in pixels (ball ~8-12px = ~50-100 area)
MAX_BLOB_AREA = 2000  # Maximum blob area (reject large moving objects like players)
DIFF_THRESHOLD = 30  # Pixel intensity threshold for difference image
MORPH_KERNEL_SIZE = 3  # Morphological operation kernel size
GAUSSIAN_BLUR = 5  # Blur kernel for noise reduction


def detect_motion_blobs(
    frame_prev: np.ndarray,
    frame_curr: np.ndarray,
    threshold: int = DIFF_THRESHOLD,
    min_area: int = MIN_BLOB_AREA,
    max_area: int = MAX_BLOB_AREA,
) -> list[dict]:
    """Detect moving blobs by frame differencing.

    Args:
        frame_prev: Previous frame (grayscale or BGR)
        frame_curr: Current frame (grayscale or BGR)
        threshold: Pixel difference threshold
        min_area: Minimum blob area
        max_area: Maximum blob area

    Returns:
        List of detected blobs: [{x, y, area, intensity}]
    """
    # Convert to grayscale if needed
    if len(frame_prev.shape) == 3:
        gray_prev = cv2.cvtColor(frame_prev, cv2.COLOR_BGR2GRAY)
        gray_curr = cv2.cvtColor(frame_curr, cv2.COLOR_BGR2GRAY)
    else:
        gray_prev = frame_prev
        gray_curr = frame_curr

    # Compute absolute difference
    diff = cv2.absdiff(gray_prev, gray_curr)

    # Blur to reduce noise
    diff = cv2.GaussianBlur(diff, (GAUSSIAN_BLUR, GAUSSIAN_BLUR), 0)

    # Threshold
    _, binary = cv2.threshold(diff, threshold, 255, cv2.THRESH_BINARY)

    # Morphological operations to clean up
    kernel = np.ones((MORPH_KERNEL_SIZE, MORPH_KERNEL_SIZE), np.uint8)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)

    # Find contours
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    blobs = []
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < min_area or area > max_area:
            continue

        # Get centroid
        M = cv2.moments(contour)
        if M["m00"] == 0:
            continue
        cx = int(M["m10"] / M["m00"])
        cy = int(M["m01"] / M["m00"])

        # Get average intensity in the diff image at this location
        mask = np.zeros_like(binary)
        cv2.drawContours(mask, [contour], -1, 255, -1)
        intensity = float(cv2.mean(diff, mask=mask)[0])

        blobs.append(
            {
                "x": cx,
                "y": cy,
                "area": int(area),
                "intensity": round(intensity, 1),
            }
        )

    # Sort by intensity (brightest = most movement)
    blobs.sort(key=lambda b: b["intensity"], reverse=True)
    return blobs


def detect_ball_from_video(
    video_path: Path,
    frame_interval: int = 8,
    search_region: dict | None = None,
) -> list[dict]:
    """Run frame differencing on a video to find ball candidates.

    Args:
        video_path: Path to the video file
        frame_interval: Process every Nth frame
        search_region: Optional {x_min, x_max, y_min, y_max} in panoramic pixels
            to restrict search area

    Returns:
        List of detections: [{frame_idx, x, y, area, intensity}]
    """
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        logger.error("Cannot open video: %s", video_path)
        return []

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    logger.info("Video: %d frames, processing every %d", total_frames, frame_interval)

    detections = []
    prev_frame = None
    frame_idx = 0
    frames_processed = 0
    start = time.time()

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        if frame_idx % frame_interval == 0:
            if prev_frame is not None:
                # Optionally crop to search region
                if search_region:
                    x1 = search_region.get("x_min", 0)
                    x2 = search_region.get("x_max", frame.shape[1])
                    y1 = search_region.get("y_min", 0)
                    y2 = search_region.get("y_max", frame.shape[0])
                    crop_prev = prev_frame[y1:y2, x1:x2]
                    crop_curr = frame[y1:y2, x1:x2]
                    offset_x, offset_y = x1, y1
                else:
                    crop_prev = prev_frame
                    crop_curr = frame
                    offset_x, offset_y = 0, 0

                blobs = detect_motion_blobs(crop_prev, crop_curr)

                for blob in blobs:
                    detections.append(
                        {
                            "frame_idx": frame_idx,
                            "x": blob["x"] + offset_x,
                            "y": blob["y"] + offset_y,
                            "area": blob["area"],
                            "intensity": blob["intensity"],
                        }
                    )

                frames_processed += 1

            prev_frame = frame.copy()

        frame_idx += 1

        if frames_processed > 0 and frames_processed % 500 == 0:
            elapsed = time.time() - start
            rate = frames_processed / elapsed
            logger.info(
                "%d/%d frames, %d detections (%.1f f/s)",
                frame_idx,
                total_frames,
                len(detections),
                rate,
            )

    cap.release()
    elapsed = time.time() - start
    logger.info(
        "DONE: %d frames processed, %d detections in %.0fs",
        frames_processed,
        len(detections),
        elapsed,
    )

    return detections


def correlate_with_tracker(
    diff_detections: list[dict],
    tracker_positions: dict[int, tuple[float, float]],
    max_distance: float = 100.0,
) -> list[dict]:
    """Filter diff detections that are near the tracker's predicted position.

    This reduces false positives by only keeping motion blobs that are
    near where the ball is expected to be (from the Kalman tracker).

    Args:
        diff_detections: Detections from frame differencing
        tracker_positions: {frame_idx: (predicted_x, predicted_y)}
        max_distance: Maximum distance to accept a match

    Returns:
        Filtered detections with match_distance added
    """
    matched = []
    for det in diff_detections:
        fi = det["frame_idx"]
        if fi in tracker_positions:
            tx, ty = tracker_positions[fi]
            dist = ((det["x"] - tx) ** 2 + (det["y"] - ty) ** 2) ** 0.5
            if dist < max_distance:
                det_copy = dict(det)
                det_copy["match_distance"] = round(dist, 1)
                matched.append(det_copy)

    return matched


def main():
    parser = argparse.ArgumentParser(
        description="Detect moving ball using frame differencing"
    )
    parser.add_argument(
        "--video",
        type=Path,
        required=True,
        help="Path to video file",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output JSON file for detections",
    )
    parser.add_argument(
        "--frame-interval",
        type=int,
        default=8,
        help="Process every Nth frame",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    detections = detect_ball_from_video(args.video, args.frame_interval)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w") as f:
            json.dump(detections, f)
        logger.info("Saved %d detections to %s", len(detections), args.output)
    else:
        # Print summary
        frames_with_dets = len(set(d["frame_idx"] for d in detections))
        logger.info(
            "Summary: %d detections across %d frames", len(detections), frames_with_dets
        )
        if detections:
            avg_intensity = sum(d["intensity"] for d in detections) / len(detections)
            avg_area = sum(d["area"] for d in detections) / len(detections)
            logger.info(
                "  Avg intensity: %.1f, Avg area: %.1f", avg_intensity, avg_area
            )


if __name__ == "__main__":
    main()
