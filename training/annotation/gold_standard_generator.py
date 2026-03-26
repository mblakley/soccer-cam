"""Generate gold-standard annotation packets from dataset tiles.

Samples tiles from the organized dataset for human annotation to create
a reliable validation set with perfect ground-truth labels.

Sampling strategy:
  - Tiles WITH existing bootstrap labels (verify: is the ball really there?)
  - Tiles WITHOUT labels from the same games (find missed balls)

Usage:
    python -m training.annotation.gold_standard_generator \
        --dataset F:/training_data/ball_dataset_640 \
        --tiles F:/training_data/tiles_640 \
        --output review_packets \
        --num-labeled 500 --num-unlabeled 500
"""

import argparse
import json
import logging
import random
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

TILE_SIZE = 640


def find_labeled_tiles(dataset_path: Path, split: str = "val") -> list[dict]:
    """Find tiles that have bootstrap labels in the organized dataset."""
    images_dir = dataset_path / "images" / split
    labels_dir = dataset_path / "labels" / split
    tiles = []

    if not images_dir.exists():
        logger.warning("Images directory not found: %s", images_dir)
        return tiles

    for game_dir in sorted(images_dir.iterdir()):
        if not game_dir.is_dir():
            continue
        game_id = game_dir.name
        label_game_dir = labels_dir / game_id

        for img_path in game_dir.glob("*.jpg"):
            label_path = label_game_dir / (img_path.stem + ".txt")
            if label_path.exists():
                # Parse existing label for model_detection
                detection = _parse_label(label_path)
                tiles.append(
                    {
                        "image_path": str(img_path),
                        "game_id": game_id,
                        "filename": img_path.name,
                        "has_label": True,
                        "model_detection": detection,
                    }
                )

    return tiles


def find_unlabeled_tiles(
    tiles_path: Path,
    dataset_path: Path,
    val_games: list[str],
) -> list[dict]:
    """Find tiles from val games that don't have labels in the dataset.

    These are tiles where the bootstrap model didn't detect a ball,
    but a ball might actually be present (missed detections).
    """
    labels_dir = dataset_path / "labels" / "val"
    tiles = []

    for game_id in val_games:
        game_tiles_dir = tiles_path / game_id
        if not game_tiles_dir.exists():
            continue

        # Get set of labeled tile stems for this game
        labeled_stems = set()
        game_labels_dir = labels_dir / game_id
        if game_labels_dir.exists():
            labeled_stems = {p.stem for p in game_labels_dir.glob("*.txt")}

        # Find unlabeled tiles (skip top row r0 as excluded)
        for img_path in game_tiles_dir.glob("*.jpg"):
            if "_r0_" in img_path.name:
                continue
            if img_path.stem not in labeled_stems:
                tiles.append(
                    {
                        "image_path": str(img_path),
                        "game_id": game_id,
                        "filename": img_path.name,
                        "has_label": False,
                        "model_detection": None,
                    }
                )

    return tiles


def _parse_label(label_path: Path) -> dict | None:
    """Parse a YOLO label file to get detection coordinates."""
    try:
        text = label_path.read_text().strip()
        if not text:
            return None
        parts = text.split()
        if len(parts) < 5:
            return None
        # YOLO format: class cx_norm cy_norm w_norm h_norm
        cx_norm = float(parts[1])
        cy_norm = float(parts[2])
        return {
            "x": int(cx_norm * TILE_SIZE),
            "y": int(cy_norm * TILE_SIZE),
            "confidence": 1.0,  # bootstrap label, no real confidence
        }
    except (ValueError, IndexError):
        return None


def generate_gold_standard_packets(
    dataset_path: Path,
    tiles_path: Path,
    output_dir: Path,
    num_labeled: int = 500,
    num_unlabeled: int = 500,
    packet_size: int = 100,
    seed: int = 42,
) -> list[Path]:
    """Generate annotation packets for gold-standard validation set.

    Args:
        dataset_path: Path to organized dataset (ball_dataset_640)
        tiles_path: Path to raw tiles directory (tiles_640)
        output_dir: Base directory for review packets
        num_labeled: Number of labeled tiles to sample (verify existing labels)
        num_unlabeled: Number of unlabeled tiles to sample (find missed balls)
        packet_size: Tiles per packet (for manageable review sessions)
        seed: Random seed for reproducibility

    Returns:
        List of manifest paths created.
    """
    rng = random.Random(seed)

    # Find val games
    val_images = dataset_path / "images" / "val"
    val_games = [d.name for d in sorted(val_images.iterdir()) if d.is_dir()]
    logger.info("Found %d val games: %s", len(val_games), val_games)

    # Sample labeled tiles
    labeled_tiles = find_labeled_tiles(dataset_path, "val")
    logger.info("Found %d labeled val tiles", len(labeled_tiles))
    sampled_labeled = rng.sample(labeled_tiles, min(num_labeled, len(labeled_tiles)))

    # Sample unlabeled tiles
    unlabeled_tiles = find_unlabeled_tiles(tiles_path, dataset_path, val_games)
    logger.info("Found %d unlabeled val tiles", len(unlabeled_tiles))
    sampled_unlabeled = rng.sample(
        unlabeled_tiles, min(num_unlabeled, len(unlabeled_tiles))
    )

    # Combine and shuffle
    all_tiles = sampled_labeled + sampled_unlabeled
    rng.shuffle(all_tiles)
    logger.info(
        "Total tiles for annotation: %d (%d labeled + %d unlabeled)",
        len(all_tiles),
        len(sampled_labeled),
        len(sampled_unlabeled),
    )

    # Split into packets
    manifests = []
    for packet_idx in range(0, len(all_tiles), packet_size):
        batch = all_tiles[packet_idx : packet_idx + packet_size]
        packet_id = f"gold_standard_{packet_idx // packet_size + 1:03d}"
        manifest_path = _create_packet(output_dir, packet_id, batch)
        manifests.append(manifest_path)
        logger.info("Created packet %s with %d tiles", packet_id, len(batch))

    return manifests


def _create_packet(output_dir: Path, packet_id: str, tiles: list[dict]) -> Path:
    """Create a single review packet from a list of tiles."""
    packet_dir = output_dir / packet_id
    crops_dir = packet_dir / "crops"
    crops_dir.mkdir(parents=True, exist_ok=True)

    frames = []
    for idx, tile in enumerate(tiles):
        src = Path(tile["image_path"])
        # Use index as frame_idx for consistent naming
        crop_filename = f"frame_{idx:06d}.jpg"
        dst = crops_dir / crop_filename

        # Copy tile to packet (tiles are already 640x640)
        shutil.copy2(src, dst)

        frame_entry = {
            "frame_idx": idx,
            "crop_file": f"crops/{crop_filename}",
            "crop_origin": {"x": 0, "y": 0, "w": TILE_SIZE, "h": TILE_SIZE},
            "source_resolution": {"w": TILE_SIZE, "h": TILE_SIZE},
            "model_detection": tile["model_detection"],
            "reason": "gold_standard_verify"
            if tile["has_label"]
            else "gold_standard_search",
            "context": {
                "game_id": tile["game_id"],
                "original_filename": tile["filename"],
                "has_bootstrap_label": tile["has_label"],
            },
        }
        frames.append(frame_entry)

    manifest = {
        "game_id": packet_id,
        "model_version": "gold_standard",
        "source_video": None,
        "source_resolution": {"w": TILE_SIZE, "h": TILE_SIZE},
        "total_game_frames": len(frames),
        "packet_type": "gold_standard",
        "frames": frames,
    }

    manifest_path = packet_dir / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    return manifest_path


def main():
    parser = argparse.ArgumentParser(
        description="Generate gold-standard annotation packets"
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("F:/training_data/ball_dataset_640"),
        help="Path to organized dataset",
    )
    parser.add_argument(
        "--tiles",
        type=Path,
        default=Path("F:/training_data/tiles_640"),
        help="Path to raw tiles directory",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("review_packets"),
        help="Output directory for packets",
    )
    parser.add_argument("--num-labeled", type=int, default=500)
    parser.add_argument("--num-unlabeled", type=int, default=500)
    parser.add_argument("--packet-size", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    manifests = generate_gold_standard_packets(
        dataset_path=args.dataset,
        tiles_path=args.tiles,
        output_dir=args.output,
        num_labeled=args.num_labeled,
        num_unlabeled=args.num_unlabeled,
        packet_size=args.packet_size,
        seed=args.seed,
    )
    print(f"Generated {len(manifests)} packets in {args.output}")


if __name__ == "__main__":
    main()
