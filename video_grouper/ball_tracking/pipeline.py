"""Ball + player tracking pipeline orchestrating Stages A -> B -> C -> D.

Processes a video frame-by-frame, producing per-frame ball track data
with dynamic FOV based on player spread.
"""

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2

from video_grouper.ball_tracking.candidate_generator import CandidateGenerator
from video_grouper.ball_tracking.coordinates import AngularPosition, CameraProfile
from video_grouper.ball_tracking.detector import BallDetector
from video_grouper.ball_tracking.fov_controller import FovController
from video_grouper.ball_tracking.player_tracker import PlayerTracker
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
    fov: float  # FOV in degrees based on player spread
    player_count: int  # number of active players in FOV computation


def process_video(
    video_path: Path,
    model_path: Path,
    output_path: Path,
    profile: CameraProfile | None = None,
    device: str = "cpu",
    confidence: float = 0.25,
    player_detection_interval: int = 3,
    player_track_scale: float = 0.5,
    player_confidence: float = 0.15,
    fov_min: float = 25.0,
    fov_max: float = 60.0,
    fov_padding: float = 1.2,
) -> list[FrameRecord]:
    """Run the full ball + player tracking pipeline on a video.

    Args:
        video_path: Path to input video (.mp4)
        model_path: Path to trained YOLO model weights
        output_path: Path to write ball_track.json
        profile: Camera profile (default: Dahua panoramic)
        device: Inference device
        confidence: Detection confidence threshold
        player_detection_interval: Run player tracking every N frames
        player_track_scale: Downscale factor for player tracking (0.5 = half res)
        player_confidence: Detection confidence for player tracking
        fov_min: Minimum FOV in degrees
        fov_max: Maximum FOV in degrees
        fov_padding: Padding factor for player bounding box (1.2 = 20%)

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

    # Player tracking uses yolov8n COCO pretrained (person class 0)
    # with its own model instance to avoid ByteTrack state conflicts
    player_tracker = PlayerTracker(
        model_path="yolov8n.pt",
        profile=profile,
        confidence=player_confidence,
        track_scale=player_track_scale,
        fps=fps,
    )
    fov_ctrl = FovController(
        min_fov_deg=fov_min,
        max_fov_deg=fov_max,
        padding=fov_padding,
        relevance_radius=0.8,  # wider radius to include more players
    )

    records: list[FrameRecord] = []
    frame_idx = 0

    # Pipeline-side EMA smoothing for pan target
    smooth_yaw = 0.0
    smooth_pitch = 0.0
    pan_alpha = 0.96  # higher = smoother, slower response
    last_known_target = AngularPosition(yaw=0.0, pitch=0.0)

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        # Stage A: Motion proposals (every frame)
        proposals = candidate_gen.process_frame(frame)

        # Stage B: Ball detection on ROI crops (adaptive frequency)
        should_detect = (frame_idx % tracker.detection_frequency) == 0
        if should_detect:
            rois = _build_rois(tracker, proposals, detector, profile)
            detections = detector.detect_in_rois(frame, rois[: tracker.max_rois])

            # Stage C: Update ball tracker with detections
            for det in detections:
                tracker.update(det.center, det.confidence)

        # Stage B2: Player tracking on full frame (every N frames)
        should_track_players = (frame_idx % player_detection_interval) == 0
        if should_track_players:
            players = player_tracker.track_frame(frame)
        else:
            players = player_tracker.get_last_players()

        # Predict step (every frame)
        tracker.predict()

        # Stage D: Get blended output from ball tracker
        state = tracker.get_state(proposals.play_region)

        # Stage C2: Update FOV and get player bbox center
        ball_pos = state.position if state.confidence > 0.1 else None
        if should_track_players:
            fov_state = fov_ctrl.update(
                players=players,
                ball_position=ball_pos,
                play_region=proposals.play_region,
                ball_confidence=state.confidence,
            )
        else:
            fov_state = fov_ctrl.predict()

        # Determine pan target: prefer player bbox center > ball > motion > hold
        if fov_state.center is not None:
            # Player cluster center is the most stable signal
            target = fov_state.center
            source = "players"
            last_known_target = target
        elif state.confidence > 0.3:
            # Confident ball detection
            target = state.position
            source = "ball"
            last_known_target = target
        elif proposals.has_motion:
            # Motion-based play region as fallback (only when motion exists)
            target = proposals.play_region
            source = "motion"
            last_known_target = target
        else:
            # No players, no ball, no motion: hold last known position
            target = last_known_target
            source = "hold"

        # EMA smooth the pan target
        smooth_yaw = smooth_yaw * pan_alpha + target.yaw * (1 - pan_alpha)
        smooth_pitch = smooth_pitch * pan_alpha + target.pitch * (1 - pan_alpha)

        records.append(
            FrameRecord(
                frame=frame_idx,
                yaw=round(smooth_yaw, 4),
                pitch=round(smooth_pitch, 4),
                confidence=state.confidence,
                source=source,
                vyaw=state.velocity.vyaw,
                vpitch=state.velocity.vpitch,
                fov=round(fov_state.smoothed_fov_deg, 1),
                player_count=fov_state.active_player_count,
            )
        )

        if frame_idx % 300 == 0:
            logger.info(
                "Frame %d/%d -- src=%s yaw=%.3f fov=%.1f players=%d",
                frame_idx,
                total_frames,
                source,
                smooth_yaw,
                fov_state.smoothed_fov_deg,
                fov_state.active_player_count,
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
