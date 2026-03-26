"""Generate review packets from ball tracking results for mobile annotation.

Reads ball_track.json after pipeline processing, selects the most valuable
frames for human review (uncertain detections, tracker losses, confidence
transitions), extracts crops from the source video, and writes a manifest
+ crop images to the review packet directory.
"""

import json
import logging
import random
from dataclasses import dataclass
from pathlib import Path

import cv2

logger = logging.getLogger(__name__)

DEFAULT_CROP_SIZE = 640
MAX_FRAMES_PER_PACKET = 100


@dataclass
class FrameSelection:
    """A frame selected for human review."""

    frame_idx: int
    reason: str
    crop_origin: dict  # {"x": int, "y": int, "w": int, "h": int}
    model_detection: dict | None  # {"x": int, "y": int, "confidence": float} or None
    context: dict


def select_review_frames(
    track_data: dict,
    max_frames: int = MAX_FRAMES_PER_PACKET,
    low_conf_range: tuple[float, float] = (0.15, 0.50),
    high_conf_sample_rate: float = 0.05,
) -> list[FrameSelection]:
    """Select frames with highest training value from ball_track.json data.

    Priority order:
    1. Low confidence detections (model uncertain)
    2. Tracker lost frames (no ball)
    3. Confidence transitions (state changes)
    4. Random high-confidence samples (verify no hallucinations)
    """
    frames = track_data["frames"]
    resolution = track_data["resolution"]
    width, height = resolution[0], resolution[1]

    selections: list[FrameSelection] = []

    prev_source = None
    for f in frames:
        conf = f["confidence"]
        source = f["source"]
        frame_idx = f["frame"]

        crop_origin = _compute_crop_origin(
            f["yaw"], f["pitch"], width, height, track_data.get("fov_h", 3.14159)
        )

        detection = None
        if source in ("ball", "blend") and conf > 0:
            det_x = int((f["yaw"] / track_data.get("fov_h", 3.14159) + 0.5) * width)
            det_y = int((f["pitch"] / track_data.get("fov_v", 1.38) + 0.5) * height)
            detection = {
                "x": det_x - crop_origin["x"],
                "y": det_y - crop_origin["y"],
                "confidence": round(conf, 3),
            }

        context = {
            "tracker_state": source,
            "play_region_yaw": round(f.get("yaw", 0), 4),
        }

        # Priority 1: Low confidence detections
        if low_conf_range[0] <= conf <= low_conf_range[1] and source in (
            "ball",
            "blend",
        ):
            selections.append(
                FrameSelection(
                    frame_idx=frame_idx,
                    reason="low_confidence",
                    crop_origin=crop_origin,
                    model_detection=detection,
                    context=context,
                )
            )
        # Priority 2: Tracker lost
        elif source == "play_region" and conf < 0.1:
            selections.append(
                FrameSelection(
                    frame_idx=frame_idx,
                    reason="tracker_lost",
                    crop_origin=crop_origin,
                    model_detection=None,
                    context=context,
                )
            )
        # Priority 3: Confidence transitions
        elif prev_source is not None and prev_source != source:
            selections.append(
                FrameSelection(
                    frame_idx=frame_idx,
                    reason="confidence_transition",
                    crop_origin=crop_origin,
                    model_detection=detection,
                    context=context,
                )
            )
        # Priority 4: Random high-confidence samples
        elif conf > 0.7 and random.random() < high_conf_sample_rate:
            selections.append(
                FrameSelection(
                    frame_idx=frame_idx,
                    reason="high_confidence_audit",
                    crop_origin=crop_origin,
                    model_detection=detection,
                    context=context,
                )
            )

        prev_source = source

    # Cap to max_frames, prioritizing by reason
    if len(selections) > max_frames:
        priority = {
            "low_confidence": 0,
            "tracker_lost": 1,
            "confidence_transition": 2,
            "high_confidence_audit": 3,
        }
        selections.sort(key=lambda s: (priority.get(s.reason, 99), s.frame_idx))
        selections = selections[:max_frames]
        selections.sort(key=lambda s: s.frame_idx)

    return selections


def _compute_crop_origin(
    yaw: float, pitch: float, width: int, height: int, fov_h: float
) -> dict:
    """Compute crop rectangle centered on the angular position."""
    fov_v = fov_h * height / width
    cx = int((yaw / fov_h + 0.5) * width)
    cy = int((pitch / fov_v + 0.5) * height)

    half = DEFAULT_CROP_SIZE // 2
    x = max(0, min(cx - half, width - DEFAULT_CROP_SIZE))
    y = max(0, min(cy - half, height - DEFAULT_CROP_SIZE))

    return {"x": x, "y": y, "w": DEFAULT_CROP_SIZE, "h": DEFAULT_CROP_SIZE}


def extract_crops(
    video_path: Path,
    selections: list[FrameSelection],
    output_dir: Path,
) -> list[str]:
    """Extract crop images from video for selected frames.

    Returns list of crop filenames written.
    """
    crops_dir = output_dir / "crops"
    crops_dir.mkdir(parents=True, exist_ok=True)

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise ValueError(f"Cannot open video: {video_path}")

    frame_map = {s.frame_idx: s for s in selections}
    filenames = []
    frame_idx = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        if frame_idx in frame_map:
            sel = frame_map[frame_idx]
            o = sel.crop_origin
            crop = frame[o["y"] : o["y"] + o["h"], o["x"] : o["x"] + o["w"]]

            filename = f"frame_{frame_idx:06d}.jpg"
            cv2.imwrite(str(crops_dir / filename), crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
            filenames.append(filename)

        frame_idx += 1

    cap.release()
    return filenames


def generate_review_packet(
    video_path: Path,
    track_path: Path,
    output_dir: Path,
    game_id: str | None = None,
    max_frames: int = MAX_FRAMES_PER_PACKET,
) -> Path:
    """Generate a complete review packet for a processed game.

    Args:
        video_path: Path to the source video
        track_path: Path to ball_track.json
        output_dir: Base directory for review packets
        game_id: Game identifier (defaults to track_path parent dir name)
        max_frames: Maximum frames to include

    Returns:
        Path to the generated manifest.json
    """
    with open(track_path) as f:
        track_data = json.load(f)

    if game_id is None:
        game_id = track_path.parent.name

    packet_dir = output_dir / game_id
    packet_dir.mkdir(parents=True, exist_ok=True)

    selections = select_review_frames(track_data, max_frames=max_frames)
    logger.info("Selected %d frames for review from %s", len(selections), game_id)

    if not selections:
        logger.info("No frames selected for review -- model was confident throughout")
        return packet_dir / "manifest.json"

    filenames = extract_crops(video_path, selections, packet_dir)

    manifest = {
        "game_id": game_id,
        "model_version": "ball_v1",
        "source_video": str(video_path),
        "source_resolution": {
            "w": track_data["resolution"][0],
            "h": track_data["resolution"][1],
        },
        "total_game_frames": track_data["total_frames"],
        "frames": [],
    }

    for sel, filename in zip(selections, filenames):
        manifest["frames"].append(
            {
                "frame_idx": sel.frame_idx,
                "crop_file": f"crops/{filename}",
                "crop_origin": sel.crop_origin,
                "source_resolution": manifest["source_resolution"],
                "model_detection": sel.model_detection,
                "reason": sel.reason,
                "context": sel.context,
            }
        )

    manifest_path = packet_dir / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    logger.info(
        "Generated review packet: %s (%d frames)", manifest_path, len(selections)
    )
    return manifest_path
