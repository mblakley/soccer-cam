"""Organize tiles and labels into YOLO dataset structure with train/val split.

Takes tiles from tile_frames.py and labels from bootstrap_labels.py
and creates the standard YOLO directory layout:
    ball_dataset/
        images/train/<game_id>/
        images/val/<game_id>/
        labels/train/<game_id>/
        labels/val/<game_id>/
        train.txt  (weighted image list for training)

Supports tile-level filtering (exclude sky/tree rows) and spatial
weighting (oversample goal-area tiles via repeated entries in train.txt).
"""

import argparse
import logging
import os
import random
import re
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_VAL_SPLIT = 0.15

# Tile position pattern: _r{row}_c{col} before the file extension
_TILE_POS_RE = re.compile(r"_r(\d+)_c(\d+)$")


def parse_tile_position(stem: str) -> tuple[int, int] | None:
    """Extract (row, col) from a tile filename stem like 'frame001_r1_c3'."""
    m = _TILE_POS_RE.search(stem)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


# Default tile weights for 7x3 grid on 4096x1800 panoramic camera:
#   r0 (top row): excluded — sky, trees, background
#   r1 c0/c6 (goal areas): 3x — most important game action
#   r1 c1-c5 (main field): 2x — standard field coverage
#   r2 c0/c6 (near sideline corners): 1x — some useful near-goal data
#   r2 c1-c5 (near field): 2x — standard field coverage
DEFAULT_EXCLUDE_ROWS = {0}
DEFAULT_TILE_WEIGHTS = {
    (1, 0): 3,
    (1, 1): 2,
    (1, 2): 2,
    (1, 3): 2,
    (1, 4): 2,
    (1, 5): 2,
    (1, 6): 3,
    (2, 0): 1,
    (2, 1): 2,
    (2, 2): 2,
    (2, 3): 2,
    (2, 4): 2,
    (2, 5): 2,
    (2, 6): 1,
}


def organize_dataset(
    tiles_dir: Path,
    labels_dir: Path,
    output_dir: Path,
    val_split: float = DEFAULT_VAL_SPLIT,
    seed: int = 42,
    include_negatives: bool = True,
    exclude_rows: set[int] | None = None,
    tile_weights: dict[tuple[int, int], int] | None = None,
) -> dict[str, int]:
    """Organize tiles and labels into YOLO dataset format.

    Uses hardlinks (same volume) for speed. Splits at the game level
    to keep I/O sequential and avoid random seeks on mechanical drives.

    Args:
        tiles_dir: Directory containing tile images (nested subdirs OK)
        labels_dir: Directory containing YOLO label files
        output_dir: Output dataset root directory
        val_split: Fraction of data to use for validation
        seed: Random seed for reproducible splits
        include_negatives: Whether to include tiles without labels (negative examples)
        exclude_rows: Set of tile row indices to exclude (e.g., {0} for top row)
        tile_weights: Dict mapping (row, col) to repeat count for train.txt weighting

    Returns:
        Dict with counts: {"train_images", "val_images", "train_labeled", "val_labeled"}
    """
    random.seed(seed)

    if exclude_rows is None:
        exclude_rows = set()

    # Discover game subdirectories
    game_dirs = sorted(
        [d for d in tiles_dir.iterdir() if d.is_dir()],
        key=lambda d: d.name,
    )
    if not game_dirs:
        # Flat structure — treat tiles_dir itself as a single game
        game_dirs = [tiles_dir]

    logger.info("Found %d game directories", len(game_dirs))

    # Collect tiles per game
    games = {}
    total_tiles = 0
    total_labeled = 0
    total_excluded = 0
    for game_dir in game_dirs:
        game_id = game_dir.name if game_dir != tiles_dir else "default"
        tiles = sorted(game_dir.rglob("*.jpg"))
        pairs = []
        labeled = 0
        excluded = 0
        for tile_path in tiles:
            # Filter by tile position
            pos = parse_tile_position(tile_path.stem)
            if pos and pos[0] in exclude_rows:
                excluded += 1
                continue

            rel = tile_path.relative_to(tiles_dir)
            label_path = labels_dir / rel.with_suffix(".txt")
            has_label = label_path.exists() and label_path.stat().st_size > 0
            if has_label or include_negatives:
                pairs.append((tile_path, label_path if has_label else None, rel))
                if has_label:
                    labeled += 1
        if pairs:
            games[game_id] = pairs
            total_tiles += len(pairs)
            total_labeled += labeled
            total_excluded += excluded
            logger.info(
                "  %s: %d tiles (%d labeled, %d excluded)",
                game_id,
                len(pairs),
                labeled,
                excluded,
            )

    logger.info(
        "Total: %d tiles (%d labeled, %d negative, %d excluded)",
        total_tiles,
        total_labeled,
        total_tiles - total_labeled,
        total_excluded,
    )

    # Split games into train/val (split at game level for sequential I/O)
    game_ids = list(games.keys())
    random.shuffle(game_ids)

    # Assign games to val until we reach target fraction
    val_target = int(total_tiles * val_split)
    val_games = set()
    val_count = 0
    for gid in game_ids:
        if val_count >= val_target:
            break
        val_games.add(gid)
        val_count += len(games[gid])

    train_games = [gid for gid in game_ids if gid not in val_games]
    val_games_list = list(val_games)

    logger.info(
        "Split: %d train games (%d tiles), %d val games (%d tiles)",
        len(train_games),
        sum(len(games[g]) for g in train_games),
        len(val_games_list),
        sum(len(games[g]) for g in val_games_list),
    )

    # Create output structure
    for split in ("train", "val"):
        (output_dir / "images" / split).mkdir(parents=True, exist_ok=True)
        (output_dir / "labels" / split).mkdir(parents=True, exist_ok=True)

    stats = {"train_images": 0, "val_images": 0, "train_labeled": 0, "val_labeled": 0}
    train_paths = []  # for weighted train.txt
    processed = 0
    start_time = time.time()
    last_log_time = start_time

    def link_or_copy(src: Path, dst: Path):
        """Hardlink if possible, fall back to copy."""
        try:
            os.link(src, dst)
        except OSError:
            shutil.copy2(src, dst)

    for split_name, split_game_ids in [("train", train_games), ("val", val_games_list)]:
        for game_id in split_game_ids:
            # Create game subdirectory once
            img_game_dir = output_dir / "images" / split_name / game_id
            lbl_game_dir = output_dir / "labels" / split_name / game_id
            img_game_dir.mkdir(parents=True, exist_ok=True)
            lbl_game_dir.mkdir(parents=True, exist_ok=True)

            for tile_path, label_path, rel_path in games[game_id]:
                dst_img = output_dir / "images" / split_name / rel_path
                dst_lbl = (
                    output_dir / "labels" / split_name / rel_path.with_suffix(".txt")
                )

                # Ensure subdirs exist (for nested game structures)
                dst_img.parent.mkdir(parents=True, exist_ok=True)
                dst_lbl.parent.mkdir(parents=True, exist_ok=True)

                link_or_copy(tile_path, dst_img)
                stats[f"{split_name}_images"] += 1

                if label_path is not None:
                    link_or_copy(label_path, dst_lbl)
                    stats[f"{split_name}_labeled"] += 1
                else:
                    dst_lbl.touch()

                # Collect train paths with weighting
                if split_name == "train" and tile_weights:
                    pos = parse_tile_position(tile_path.stem)
                    weight = tile_weights.get(pos, 1) if pos else 1
                    for _ in range(weight):
                        train_paths.append(dst_img)

                processed += 1
                now = time.time()
                if now - last_log_time >= 30:
                    elapsed = now - start_time
                    rate = processed / elapsed if elapsed > 0 else 0
                    remaining = (total_tiles - processed) / rate if rate > 0 else 0
                    pct = processed / total_tiles * 100
                    logger.info(
                        "%d/%d (%.1f%%) | %.1f files/sec | ETA %.0f min",
                        processed,
                        total_tiles,
                        pct,
                        rate,
                        remaining / 60,
                    )
                    last_log_time = now

            logger.info("  Finished game %s (%s)", game_id, split_name)

    # Write weighted train.txt
    if tile_weights and train_paths:
        random.shuffle(train_paths)
        train_txt = output_dir / "train.txt"
        with open(train_txt, "w") as f:
            for p in train_paths:
                f.write(f"{p}\n")
        logger.info(
            "Wrote %s with %d entries (%d unique images, %.1fx avg weight)",
            train_txt,
            len(train_paths),
            stats["train_images"],
            len(train_paths) / max(stats["train_images"], 1),
        )

    logger.info(
        "Dataset organized: train=%d (%d labeled), val=%d (%d labeled)",
        stats["train_images"],
        stats["train_labeled"],
        stats["val_images"],
        stats["val_labeled"],
    )
    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Organize tiles + labels into YOLO dataset structure"
    )
    parser.add_argument(
        "--tiles",
        type=Path,
        default=Path("F:/training_data/tiles_640"),
        help="Directory containing tile images",
    )
    parser.add_argument(
        "--labels",
        type=Path,
        default=Path("F:/training_data/labels_640"),
        help="Directory containing bootstrap labels",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("F:/training_data/ball_dataset_640"),
        help="Output dataset root",
    )
    parser.add_argument(
        "--val-split",
        type=float,
        default=DEFAULT_VAL_SPLIT,
        help="Validation split fraction",
    )
    parser.add_argument(
        "--no-negatives",
        action="store_true",
        help="Exclude tiles without labels",
    )
    parser.add_argument(
        "--no-weights",
        action="store_true",
        help="Disable spatial tile weighting (no train.txt)",
    )
    parser.add_argument(
        "--no-exclude",
        action="store_true",
        help="Don't exclude any tile rows",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    organize_dataset(
        args.tiles,
        args.labels,
        args.output,
        args.val_split,
        include_negatives=not args.no_negatives,
        exclude_rows=set() if args.no_exclude else DEFAULT_EXCLUDE_ROWS,
        tile_weights=None if args.no_weights else DEFAULT_TILE_WEIGHTS,
    )


if __name__ == "__main__":
    main()
