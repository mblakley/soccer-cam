"""Intelligent positive/negative sampling for ball detection training.

Reads from the junction-based dataset structure at ball_dataset_640/ and writes
train.txt and val.txt with a smart mix of:
- All positive tiles (tiles with cleaned labels)
- Hard negatives (spatially/temporally adjacent to positives)
- Random negatives (uniform sample for coverage)

Paths in the output txt files go through ball_dataset_640/images/{split}/ so
that Ultralytics can find matching labels via ball_dataset_640/labels/{split}/.
"""

import argparse
import logging
import os
import random
import time
from pathlib import Path

from training.data_prep.organize_dataset import parse_tile_filename, parse_tile_position

logger = logging.getLogger(__name__)

DEFAULT_NEG_RATIO = 1.0  # negative tiles per positive tile


def _scan_split(
    dataset_dir: Path,
    split: str,
    exclude_rows: set[int],
) -> tuple[dict[str, Path], set[str]]:
    """Scan one split of the dataset, returning (stem→image_path, label_stems).

    Reads images from dataset_dir/images/{split}/ and labels from
    dataset_dir/labels/{split}/ (both via junctions).
    """
    images_dir = dataset_dir / "images" / split
    labels_dir = dataset_dir / "labels" / split

    tile_paths: dict[str, Path] = {}  # "game/tile_stem" → image path
    label_stems: set[str] = set()

    # Scan images
    for game_dir in sorted(d for d in images_dir.iterdir() if d.is_dir()):
        game_id = game_dir.name
        for entry in os.scandir(game_dir):
            if not entry.name.endswith(".jpg"):
                continue
            stem = Path(entry.name).stem
            pos = parse_tile_position(stem)
            if pos and pos[0] in exclude_rows:
                continue
            tile_paths[f"{game_id}/{stem}"] = Path(entry.path)

    # Scan labels
    if labels_dir.exists():
        for game_dir in sorted(d for d in labels_dir.iterdir() if d.is_dir()):
            game_id = game_dir.name
            for entry in os.scandir(game_dir):
                if entry.name.endswith(".txt") and os.path.getsize(entry.path) > 0:
                    label_stems.add(f"{game_id}/{Path(entry.name).stem}")

    return tile_paths, label_stems


def _find_hard_negatives(
    positive_stems: set[str],
    all_tile_paths: dict[str, Path],
) -> set[str]:
    """Find tiles spatially and temporally adjacent to positive tiles.

    Adjacent means:
    - Same segment, same frame, neighboring row/col (spatial)
    - Same segment, same row/col, neighboring frame (temporal)
    """
    hard_neg_stems = set()

    parsed_positives: list[tuple[str, str, int, int, int]] = []
    for stem in positive_stems:
        game_id = stem.split("/")[0]
        tile_stem = stem.split("/")[1] if "/" in stem else stem
        parsed = parse_tile_filename(tile_stem)
        if parsed:
            segment, frame_idx, row, col = parsed
            parsed_positives.append((game_id, segment, frame_idx, row, col))

    available: set[str] = set(all_tile_paths.keys())

    for game_id, segment, frame_idx, row, col in parsed_positives:
        # Spatial neighbors (same frame, adjacent tiles)
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                nr, nc = row + dr, col + dc
                if nr < 0 or nc < 0:
                    continue
                neighbor = f"{game_id}/{segment}_frame_{frame_idx:06d}_r{nr}_c{nc}"
                if neighbor in available and neighbor not in positive_stems:
                    hard_neg_stems.add(neighbor)

        # Temporal neighbors (same tile position, adjacent frames)
        for df in (-8, 8):
            nf = frame_idx + df
            if nf < 0:
                continue
            neighbor = f"{game_id}/{segment}_frame_{nf:06d}_r{row}_c{col}"
            if neighbor in available and neighbor not in positive_stems:
                hard_neg_stems.add(neighbor)

    return hard_neg_stems


def smart_sample(
    dataset_dir: Path,
    neg_ratio: float = DEFAULT_NEG_RATIO,
    seed: int = 42,
    exclude_rows: set[int] | None = None,
) -> dict[str, int]:
    """Create smart-sampled train.txt and val.txt from dataset folder structure.

    Reads from ball_dataset_640/images/{train,val}/ and labels/{train,val}/
    (via junctions). Writes train.txt and val.txt into dataset_dir.

    Args:
        dataset_dir: Dataset root (F:/training_data/ball_dataset_640)
        neg_ratio: Ratio of negative to positive tiles
        seed: Random seed
        exclude_rows: Tile rows to exclude (e.g., {0} for sky)

    Returns:
        Dict with counts.
    """
    random.seed(seed)
    if exclude_rows is None:
        exclude_rows = {0}

    start_time = time.time()
    stats = {
        "train_positive": 0,
        "train_hard_neg": 0,
        "train_random_neg": 0,
        "val_positive": 0,
        "val_negative": 0,
    }

    # Scan train and val splits
    logger.info("Scanning train split...")
    train_tiles, train_labels = _scan_split(dataset_dir, "train", exclude_rows)
    logger.info("  %d tiles, %d labels", len(train_tiles), len(train_labels))

    logger.info("Scanning val split...")
    val_tiles, val_labels = _scan_split(dataset_dir, "val", exclude_rows)
    logger.info("  %d tiles, %d labels", len(val_tiles), len(val_labels))

    # --- Train sampling ---
    train_positives = train_labels & set(train_tiles.keys())
    logger.info("Train positives: %d", len(train_positives))

    hard_negatives = _find_hard_negatives(train_positives, train_tiles)
    logger.info("Hard negatives: %d", len(hard_negatives))

    train_paths = []

    # All positives
    for stem in sorted(train_positives):
        train_paths.append(str(train_tiles[stem]))
        stats["train_positive"] += 1

    # Hard negatives
    for stem in sorted(hard_negatives):
        if stem in train_tiles:
            train_paths.append(str(train_tiles[stem]))
            stats["train_hard_neg"] += 1

    # Random negatives to reach target ratio
    target_negatives = int(stats["train_positive"] * neg_ratio)
    remaining_needed = target_negatives - stats["train_hard_neg"]

    if remaining_needed > 0:
        random_pool = [
            stem
            for stem in train_tiles
            if stem not in train_positives and stem not in hard_negatives
        ]
        n_random = min(remaining_needed, len(random_pool))
        if n_random > 0:
            for stem in random.sample(random_pool, n_random):
                train_paths.append(str(train_tiles[stem]))
                stats["train_random_neg"] += 1

    # --- Val sampling ---
    val_paths = []
    val_positives = val_labels & set(val_tiles.keys())

    for stem in sorted(val_positives):
        val_paths.append(str(val_tiles[stem]))
        stats["val_positive"] += 1

    # Val negatives
    val_neg_pool = [stem for stem in val_tiles if stem not in val_positives]
    n_val_neg = min(int(stats["val_positive"] * neg_ratio), len(val_neg_pool))
    if n_val_neg > 0:
        for stem in random.sample(val_neg_pool, n_val_neg):
            val_paths.append(str(val_tiles[stem]))
            stats["val_negative"] += 1

    # Shuffle and write
    random.shuffle(train_paths)
    random.shuffle(val_paths)

    train_txt = dataset_dir / "train.txt"
    with open(train_txt, "w") as f:
        for p in train_paths:
            f.write(p.replace("\\", "/") + "\n")

    val_txt = dataset_dir / "val.txt"
    with open(val_txt, "w") as f:
        for p in val_paths:
            f.write(p.replace("\\", "/") + "\n")

    elapsed = time.time() - start_time
    logger.info(
        "=== COMPLETE in %.0fs ===\n"
        "  Train: %d positive, %d hard negative, %d random negative = %d total\n"
        "  Val: %d positive, %d negative = %d total\n"
        "  Wrote %s and %s",
        elapsed,
        stats["train_positive"],
        stats["train_hard_neg"],
        stats["train_random_neg"],
        len(train_paths),
        stats["val_positive"],
        stats["val_negative"],
        len(val_paths),
        train_txt,
        val_txt,
    )
    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Smart sampling: all positives + hard/random negatives"
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("F:/training_data/ball_dataset_640"),
        help="Dataset root directory (with images/ and labels/ junctions)",
    )
    parser.add_argument(
        "--neg-ratio",
        type=float,
        default=DEFAULT_NEG_RATIO,
        help="Ratio of negative to positive tiles",
    )
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    smart_sample(args.dataset, args.neg_ratio, args.seed)


if __name__ == "__main__":
    main()
