"""Organize tiles and labels into YOLO dataset structure with train/val split.

Takes tiles from tile_frames.py and labels from bootstrap_labels.py
and creates the standard YOLO directory layout:
    ball_dataset/
        images/train/
        images/val/
        labels/train/
        labels/val/
"""

import argparse
import logging
import random
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_VAL_SPLIT = 0.15


def organize_dataset(
    tiles_dir: Path,
    labels_dir: Path,
    output_dir: Path,
    val_split: float = DEFAULT_VAL_SPLIT,
    seed: int = 42,
    include_negatives: bool = True,
) -> dict[str, int]:
    """Organize tiles and labels into YOLO dataset format.

    Args:
        tiles_dir: Directory containing tile images (nested subdirs OK)
        labels_dir: Directory containing YOLO label files
        output_dir: Output dataset root directory
        val_split: Fraction of data to use for validation
        seed: Random seed for reproducible splits
        include_negatives: Whether to include tiles without labels (negative examples)

    Returns:
        Dict with counts: {"train_images", "val_images", "train_labeled", "val_labeled"}
    """
    random.seed(seed)

    # Collect all tile images
    tile_paths = sorted(tiles_dir.rglob("*.jpg"))
    logger.info("Found %d tile images", len(tile_paths))

    # Match tiles to labels
    pairs = []
    for tile_path in tile_paths:
        # Label path mirrors tile path structure but with .txt extension
        rel = tile_path.relative_to(tiles_dir)
        label_path = labels_dir / rel.with_suffix(".txt")
        has_label = label_path.exists() and label_path.stat().st_size > 0

        if has_label or include_negatives:
            pairs.append((tile_path, label_path if has_label else None))

    logger.info(
        "%d tiles matched (%d with labels, %d negative)",
        len(pairs),
        sum(1 for _, lbl in pairs if lbl is not None),
        sum(1 for _, lbl in pairs if lbl is None),
    )

    # Shuffle and split
    random.shuffle(pairs)
    val_count = max(1, int(len(pairs) * val_split))
    val_pairs = pairs[:val_count]
    train_pairs = pairs[val_count:]

    # Create output structure
    for split in ("train", "val"):
        (output_dir / "images" / split).mkdir(parents=True, exist_ok=True)
        (output_dir / "labels" / split).mkdir(parents=True, exist_ok=True)

    stats = {"train_images": 0, "val_images": 0, "train_labeled": 0, "val_labeled": 0}

    for split_name, split_pairs in [("train", train_pairs), ("val", val_pairs)]:
        for tile_path, label_path in split_pairs:
            # Flatten name to avoid subdirectory collisions
            flat_name = tile_path.relative_to(tiles_dir)
            flat_name = str(flat_name).replace("/", "_").replace("\\", "_")
            img_stem = Path(flat_name).stem

            dst_img = output_dir / "images" / split_name / flat_name
            dst_lbl = output_dir / "labels" / split_name / f"{img_stem}.txt"

            shutil.copy2(tile_path, dst_img)
            stats[f"{split_name}_images"] += 1

            if label_path is not None:
                shutil.copy2(label_path, dst_lbl)
                stats[f"{split_name}_labeled"] += 1
            else:
                # Write empty label for negative example
                dst_lbl.touch()

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
        default=Path("training/data/tiles"),
        help="Directory containing tile images",
    )
    parser.add_argument(
        "--labels",
        type=Path,
        default=Path("training/data/labels"),
        help="Directory containing bootstrap labels",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("training/data/ball_dataset"),
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
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    organize_dataset(
        args.tiles,
        args.labels,
        args.output,
        args.val_split,
        include_negatives=not args.no_negatives,
    )


if __name__ == "__main__":
    main()
