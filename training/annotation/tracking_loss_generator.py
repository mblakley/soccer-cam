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


def find_tracking_losses(
    dataset_path: Path,
    tiles_path: Path,
    split: str = "val",
    prefer_far_field: bool = True,
) -> list[dict]:
    """Find tiles where ball detection was lost between consecutive frames.

    For each (game, segment, row, col) position:
    1. Find all frames that have a label (ball detected)
    2. For each labeled frame, check if the NEXT frame at that position
       has a tile image but NO label (ball lost)
    3. The "loss frame" tile is what we show the annotator

    Returns list of dicts with tile info for annotation.
    """
    images_dir = dataset_path / "images" / split
    labels_dir = dataset_path / "labels" / split

    if not images_dir.exists():
        logger.warning("Images directory not found: %s", images_dir)
        return []

    losses = []

    for game_dir in sorted(images_dir.iterdir()):
        if not game_dir.is_dir():
            continue
        game_id = game_dir.name
        game_tiles_dir = tiles_path / game_id
        label_game_dir = labels_dir / game_id

        if not game_tiles_dir.exists():
            continue

        # Index all tiles by (segment, row, col) -> sorted frame indices
        tile_index: dict[tuple, dict[int, Path]] = defaultdict(dict)
        for img_path in game_tiles_dir.glob("*.jpg"):
            parsed = parse_tile_filename(img_path.stem)
            if not parsed:
                continue
            segment, frame_idx, row, col = parsed
            tile_index[(segment, row, col)][frame_idx] = img_path

        # Index labeled frames for this game
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

        # Find losses: labeled at frame N, no label at frame N+FRAME_INTERVAL
        for pos_key, frame_map in tile_index.items():
            segment, row, col = pos_key
            labeled = labeled_frames.get(pos_key, {})
            sorted_frames = sorted(frame_map.keys())

            for i, frame_idx in enumerate(sorted_frames):
                if frame_idx not in labeled:
                    continue

                # Check next frame
                next_frame = frame_idx + FRAME_INTERVAL
                if next_frame not in frame_map:
                    continue
                if next_frame in labeled:
                    continue  # Still detected, no loss

                # Found a tracking loss!
                prev_det = labeled[frame_idx]
                loss_tile_path = frame_map[next_frame]

                losses.append(
                    {
                        "image_path": str(loss_tile_path),
                        "game_id": game_id,
                        "filename": loss_tile_path.name,
                        "row": row,
                        "col": col,
                        "frame_idx": next_frame,
                        "prev_frame_idx": frame_idx,
                        "prev_detection": prev_det,
                        # Score: prefer mid-field, edge cols, small balls
                        "priority_score": _priority_score(
                            row, col, prev_det, prefer_far_field
                        ),
                    }
                )

    logger.info("Found %d tracking loss frames across all games", len(losses))
    return losses


def _priority_score(
    row: int, col: int, prev_det: dict, prefer_far_field: bool
) -> float:
    """Score for prioritizing which losses to annotate.

    Higher = more valuable to annotate.
    Prefers: row 1 (mid-field) over row 2 (far field, too many sideline balls),
    edge columns (c0, c6) where the ball enters/exits frame,
    and smaller ball detections.
    """
    score = 0.0

    # Row preference: row 1 (mid-field) is highest value for game ball
    # Row 2 (far field) has too many sideline ball false positives
    if row == 1:
        score += 10.0
    elif row == 2:
        score += 3.0

    # Edge columns (c0, c6) where ball tracking commonly fails
    if col in (0, 6):
        score += 6.0
    elif col in (1, 5):
        score += 3.0

    # Smaller balls are harder and more valuable to annotate
    ball_size = (prev_det.get("w_norm", 0.03) + prev_det.get("h_norm", 0.03)) / 2
    if ball_size < 0.02:  # < 13px, very small
        score += 8.0
    elif ball_size < 0.03:  # < 19px, small
        score += 5.0
    elif ball_size < 0.04:  # < 26px, medium
        score += 2.0

    # Add small random factor to avoid always picking same positions
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
