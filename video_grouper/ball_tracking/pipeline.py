"""Ball tracking pipeline orchestrating Stages A -> B -> C -> D.

Processes a video frame-by-frame, producing per-frame ball track data.
"""

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2

from video_grouper.ball_tracking.candidate_generator import CandidateGenerator
from video_grouper.ball_tracking.coordinates import CameraProfile
from video_grouper.ball_tracking.detector import BallDetector
from video_grouper.ball_tracking.tracker import BallTracker

logger = logging.getLogger(__name__)


@dataclass
class FrameRecord:
    """Per-frame tracking output."""

    frame: int
    yaw: float
    pitch: float
    confidence: float
    source: str  # "ball", "play_region", "blend"
    vyaw: float
    vpitch: float


def process_video(
    video_path: Path,
    model_path: Path,
    output_path: Path,
    profile: CameraProfile | None = None,
    device: str = "cpu",
    confidence: float = 0.25,
) -> list[FrameRecord]:
    """Run the full ball tracking pipeline on a video.

    Args:
        video_path: Path to input video (.mp4)
        model_path: Path to trained YOLO model weights
        output_path: Path to write ball_track.json
        profile: Camera profile (default: Dahua panoramic)
        device: Inference device
        confidence: Detection confidence threshold

    Returns:
        List of per-frame tracking records
    """
    if profile is None:
        profile = CameraProfile.dahua_panoramic()

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise ValueError(f"Cannot open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    logger.info(
        "Processing %s (%dx%d, %.1f fps, %d frames)",
        video_path.name,
        width,
        height,
        fps,
        total_frames,
    )

    candidate_gen = CandidateGenerator(profile)
    detector = BallDetector(model_path, profile, confidence=confidence, device=device)
    tracker = BallTracker(fps=fps)

    records: list[FrameRecord] = []
    frame_idx = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        # Stage A: Motion proposals (every frame)
        proposals = candidate_gen.process_frame(frame)

        # Stage B: Detection (adaptive frequency)
        should_detect = (frame_idx % tracker.detection_frequency) == 0
        if should_detect:
            rois = _build_rois(tracker, proposals, detector, profile)
            detections = detector.detect_in_rois(frame, rois[: tracker.max_rois])

            # Stage C: Update tracker with detections
            for det in detections:
                tracker.update(det.center, det.confidence)

        # Predict step (every frame)
        tracker.predict()

        # Stage D: Get blended output
        state = tracker.get_state(proposals.play_region)
        records.append(
            FrameRecord(
                frame=frame_idx,
                yaw=state.position.yaw,
                pitch=state.position.pitch,
                confidence=state.confidence,
                source=state.source,
                vyaw=state.velocity.vyaw,
                vpitch=state.velocity.vpitch,
            )
        )

        if frame_idx % 300 == 0:
            logger.info(
                "Frame %d/%d -- conf=%.2f source=%s",
                frame_idx,
                total_frames,
                state.confidence,
                state.source,
            )

        frame_idx += 1

    cap.release()

    # Write output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output = {
        "fps": fps,
        "resolution": [width, height],
        "total_frames": frame_idx,
        "frames": [asdict(r) for r in records],
    }
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)

    logger.info("Wrote %d frame records to %s", len(records), output_path)
    return records


def _build_rois(
    tracker: BallTracker,
    proposals,
    detector: BallDetector,
    profile: CameraProfile,
) -> list[tuple[int, int, int, int]]:
    """Build prioritized list of ROIs for detection."""
    rois = []

    # Tracker prior ROI (highest priority when tracking)
    if tracker.is_locked:
        rois.append(detector.build_roi_around(tracker.position, size=300))

    # Motion candidate ROIs
    for candidate in proposals.candidates[:6]:
        rois.append(candidate.pixel_rect)

    # Play region ROI (fallback)
    rois.append(detector.build_roi_around(proposals.play_region, size=500))

    return rois
