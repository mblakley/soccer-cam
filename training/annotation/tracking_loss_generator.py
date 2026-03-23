"""Generate annotation packets for frames where tracking likely lost the ball.

Finds tiles where the ball was detected in frame N but NOT in frame N+1
at the same tile position — these are the moments the model loses track.
Prioritizes far-field tiles (row 2) and small detections.

Usage:
    python -m training.annotation.tracking_loss_generator \
        --dataset F:/training_data/ball_dataset_640 \
        --tiles F:/training_data/tiles_640 \
        --output review_packets \
        --num 500 --packet-size 100
"""

import argparse
import json
import logging
import random
import shutil
from collections import defaultdict
from pathlib import Path

from training.data_prep.organize_dataset import parse_tile_filename

logger = logging.getLogger(__name__)

TILE_SIZE = 640
# Frame interval between consecutive extracted frames (from extract_frames.py)
FRAME_INTERVAL = 8


def _parse_label(label_path: Path) -> dict | None:
    """Parse YOLO label to get detection center and size."""
    try:
        text = label_path.read_text().strip()
        if not text:
            return None
        # Take first line (first detection)
        parts = text.split("\n")[0].split()
        if len(parts) < 5:
            return None
        cx_norm = float(parts[1])
        cy_norm = float(parts[2])
        w_norm = float(parts[3])
        h_norm = float(parts[4])
        return {
            "x": int(cx_norm * TILE_SIZE),
            "y": int(cy_norm * TILE_SIZE),
            "w_norm": w_norm,
            "h_norm": h_norm,
            "confidence": 1.0,
        }
    except (ValueError, IndexError):
        return None


def _build_tile_indices(
    dataset_path: Path,
    tiles_path: Path,
    split: str = "val",
) -> list[tuple[str, dict, dict]]:
    """Build tile and label indices for each val game.

    Returns list of (game_id, tile_index, labeled_frames) tuples.
    tile_index: {(segment, row, col): {frame_idx: Path}}
    labeled_frames: {(segment, row, col): {frame_idx: detection_dict}}
    """
    images_dir = dataset_path / "images" / split
    labels_dir = dataset_path / "labels" / split

    if not images_dir.exists():
        logger.warning("Images directory not found: %s", images_dir)
        return []

    results = []

    for game_dir in sorted(images_dir.iterdir()):
        if not game_dir.is_dir():
            continue
        game_id = game_dir.name
        game_tiles_dir = tiles_path / game_id
        label_game_dir = labels_dir / game_id

        if not game_tiles_dir.exists():
            continue

        # Index all tiles by (segment, row, col) -> frame_idx -> path
        tile_index: dict[tuple, dict[int, Path]] = defaultdict(dict)
        for img_path in game_tiles_dir.glob("*.jpg"):
            parsed = parse_tile_filename(img_path.stem)
            if not parsed:
                continue
            segment, frame_idx, row, col = parsed
            tile_index[(segment, row, col)][frame_idx] = img_path

        # Index labeled frames
        labeled_frames: dict[tuple, dict[int, dict]] = defaultdict(dict)
        if label_game_dir and label_game_dir.exists():
            for label_path in label_game_dir.glob("*.txt"):
                parsed = parse_tile_filename(label_path.stem)
                if not parsed:
                    continue
                segment, frame_idx, row, col = parsed
                det = _parse_label(label_path)
                if det:
                    labeled_frames[(segment, row, col)][frame_idx] = det

        results.append((game_id, tile_index, labeled_frames))

    return results


def find_tracking_losses(
    dataset_path: Path,
    tiles_path: Path,
    split: str = "val",
    min_trajectory_length: int = 3,
) -> list[dict]:
    """Find trajectory breaks where the game ball was likely lost.

    Strategy:
    1. At each tile position (segment, row, col), find runs of consecutive
       frames with detections = trajectories
    2. Keep only trajectories >= min_trajectory_length frames (real ball
       movement, not noise)
    3. For each trajectory end, if the NEXT frame has a tile but no
       detection, that's a tracking loss
    4. Score by trajectory length, field position, and ball size

    Returns list of dicts with tile info for annotation.
    """
    game_indices = _build_tile_indices(dataset_path, tiles_path, split)
    losses = []

    for game_id, tile_index, labeled_frames in game_indices:
        for pos_key, frame_map in tile_index.items():
            segment, row, col = pos_key
            labeled = labeled_frames.get(pos_key, {})
            sorted_frames = sorted(frame_map.keys())

            # Build trajectories: runs of consecutive labeled frames
            trajectories = []
            current_run = []

            for frame_idx in sorted_frames:
                if frame_idx in labeled:
                    current_run.append(frame_idx)
                else:
                    if len(current_run) >= min_trajectory_length:
                        trajectories.append(current_run)
                    current_run = []
            if len(current_run) >= min_trajectory_length:
                trajectories.append(current_run)

            # For each trajectory, check if there's a loss frame after it
            for traj in trajectories:
                last_frame = traj[-1]
                next_frame = last_frame + FRAME_INTERVAL

                if next_frame not in frame_map:
                    continue  # No tile exists for next frame
                if next_frame in labeled:
                    continue  # Ball still detected (shouldn't happen)

                prev_det = labeled[last_frame]
                loss_tile_path = frame_map[next_frame]

                losses.append(
                    {
                        "image_path": str(loss_tile_path),
                        "game_id": game_id,
                        "filename": loss_tile_path.name,
                        "row": row,
                        "col": col,
                        "frame_idx": next_frame,
                        "prev_frame_idx": last_frame,
                        "prev_detection": prev_det,
                        "trajectory_length": len(traj),
                        "priority_score": _priority_score(
                            row, col, prev_det, len(traj), next_frame
                        ),
                    }
                )

    logger.info(
        "Found %d trajectory-break losses (min traj length=%d)",
        len(losses),
        min_trajectory_length,
    )
    return losses


def _priority_score(
    row: int, col: int, prev_det: dict, trajectory_length: int, frame_idx: int
) -> float:
    """Score for prioritizing which losses to annotate.

    Higher = more valuable. Prefers:
    - Later frames (actual game, not warmup — warmup is always first)
    - Longer trajectories (more likely real game ball, not noise)
    - Center columns (c2-c4, mid-field action)
    - Row 1 (mid-field) over row 2 (too many sideline balls)
    - Smaller balls (harder, more valuable)
    """
    score = 0.0

    # Later frames = actual game, not warmup. This is the strongest filter.
    # Warmup is typically first ~5 min = ~1000 frames at 3fps/8-frame interval.
    # Heavily reward frames deep into the video.
    score += min(frame_idx / 500, 40.0)

    # Trajectory length: ball tracked for many frames = almost certainly game ball
    score += min(trajectory_length, 20) * 2.0

    # Row preference: row 1 (mid-field) >> row 2 (far field)
    if row == 1:
        score += 10.0
    elif row == 2:
        score += 3.0

    # Center columns = game action
    if col in (2, 3, 4):
        score += 6.0
    elif col in (1, 5):
        score += 4.0
    elif col in (0, 6):
        score += 2.0

    # Smaller balls are harder and more valuable
    ball_size = (prev_det.get("w_norm", 0.03) + prev_det.get("h_norm", 0.03)) / 2
    if ball_size < 0.02:
        score += 6.0
    elif ball_size < 0.03:
        score += 4.0
    elif ball_size < 0.04:
        score += 2.0

    # Small random factor
    score += random.random() * 0.5

    return score


def generate_tracking_loss_packets(
    dataset_path: Path,
    tiles_path: Path,
    output_dir: Path,
    num_tiles: int = 500,
    packet_size: int = 100,
    seed: int = 43,
) -> list[Path]:
    """Generate annotation packets focused on tracking loss frames.

    Args:
        dataset_path: Path to organized dataset (ball_dataset_640)
        tiles_path: Path to raw tiles directory (tiles_640)
        output_dir: Base directory for review packets
        num_tiles: Total tiles to include
        packet_size: Tiles per packet
        seed: Random seed

    Returns:
        List of manifest paths created.
    """
    random.seed(seed)

    losses = find_tracking_losses(dataset_path, tiles_path, split="val")
    if not losses:
        logger.warning("No tracking losses found")
        return []

    # Sort by priority (highest first) and take top N
    losses.sort(key=lambda x: x["priority_score"], reverse=True)
    selected = losses[:num_tiles]

    # Shuffle for annotation variety
    random.shuffle(selected)

    logger.info(
        "Selected %d tracking loss tiles (of %d found). Row distribution: r1=%d, r2=%d",
        len(selected),
        len(losses),
        sum(1 for t in selected if t["row"] == 1),
        sum(1 for t in selected if t["row"] == 2),
    )

    # Split into packets
    manifests = []
    for packet_idx in range(0, len(selected), packet_size):
        batch = selected[packet_idx : packet_idx + packet_size]
        packet_id = f"tracking_loss_{packet_idx // packet_size + 1:03d}"
        manifest_path = _create_packet(output_dir, packet_id, batch)
        manifests.append(manifest_path)
        logger.info("Created packet %s with %d tiles", packet_id, len(batch))

    return manifests


def _create_packet(output_dir: Path, packet_id: str, tiles: list[dict]) -> Path:
    """Create a review packet from tracking loss tiles."""
    packet_dir = output_dir / packet_id
    crops_dir = packet_dir / "crops"
    crops_dir.mkdir(parents=True, exist_ok=True)

    frames = []
    for idx, tile in enumerate(tiles):
        src = Path(tile["image_path"])
        crop_filename = f"frame_{idx:06d}.jpg"
        dst = crops_dir / crop_filename

        shutil.copy2(src, dst)

        # Show where the ball WAS in the previous frame as a hint
        prev = tile["prev_detection"]

        frame_entry = {
            "frame_idx": idx,
            "crop_file": f"crops/{crop_filename}",
            "crop_origin": {"x": 0, "y": 0, "w": TILE_SIZE, "h": TILE_SIZE},
            "source_resolution": {"w": TILE_SIZE, "h": TILE_SIZE},
            "model_detection": {
                "x": prev["x"],
                "y": prev["y"],
                "confidence": 0.0,  # Signal that this is a hint, not a detection
            },
            "reason": "tracking_loss",
            "context": {
                "game_id": tile["game_id"],
                "original_filename": tile["filename"],
                "row": tile["row"],
                "col": tile["col"],
                "prev_frame_idx": tile["prev_frame_idx"],
                "loss_frame_idx": tile["frame_idx"],
                "prev_ball_size": round(prev.get("w_norm", 0.03), 4),
                "trajectory_length": tile.get("trajectory_length", 1),
            },
        }
        frames.append(frame_entry)

    manifest = {
        "game_id": packet_id,
        "model_version": "tracking_loss",
        "source_video": None,
        "source_resolution": {"w": TILE_SIZE, "h": TILE_SIZE},
        "total_game_frames": len(frames),
        "packet_type": "tracking_loss",
        "frames": frames,
    }

    manifest_path = packet_dir / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    return manifest_path


def main():
    parser = argparse.ArgumentParser(
        description="Generate tracking loss annotation packets"
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("F:/training_data/ball_dataset_640"),
    )
    parser.add_argument(
        "--tiles",
        type=Path,
        default=Path("F:/training_data/tiles_640"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("review_packets"),
    )
    parser.add_argument("--num", type=int, default=500)
    parser.add_argument("--packet-size", type=int, default=100)
    parser.add_argument("--seed", type=int, default=43)

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    manifests = generate_tracking_loss_packets(
        dataset_path=args.dataset,
        tiles_path=args.tiles,
        output_dir=args.output,
        num_tiles=args.num,
        packet_size=args.packet_size,
        seed=args.seed,
    )
    print(f"Generated {len(manifests)} tracking loss packets in {args.output}")


if __name__ == "__main__":
    main()
