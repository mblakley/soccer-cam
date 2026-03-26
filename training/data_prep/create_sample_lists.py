"""Create sampled train.txt and val.txt files for YOLO training.

Scans the dataset directories and writes text files with a random sample
of image paths. This avoids YOLO scanning millions of files and running
out of memory on systems with limited RAM.
"""

import argparse
import logging
import os
import random
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def sample_jpgs(root_dir: str, sample_rate: float, label: str) -> list[str]:
    """Scan a directory tree for .jpg files and return a random sample."""
    start = time.time()
    all_files = []
    for game in sorted(os.listdir(root_dir)):
        game_path = os.path.join(root_dir, game)
        if not os.path.isdir(game_path):
            continue
        count = 0
        for entry in os.scandir(game_path):
            if entry.name.endswith(".jpg"):
                all_files.append(entry.path)
                count += 1
        logger.info("  %s: %d jpgs", game, count)

    elapsed = time.time() - start
    logger.info("%s: scanned %d files in %.0fs", label, len(all_files), elapsed)

    n_sample = int(len(all_files) * sample_rate)
    sampled = random.sample(all_files, n_sample)
    logger.info("%s: sampled %d files (%.0f%%)", label, n_sample, sample_rate * 100)
    return sampled


def main():
    parser = argparse.ArgumentParser(
        description="Create sampled train/val text files for YOLO training"
    )
    parser.add_argument(
        "--dataset-dir",
        type=Path,
        default=Path("F:/training_data/ball_dataset_640"),
        help="Dataset root directory (must contain images/train and images/val)",
    )
    parser.add_argument(
        "--train-rate",
        type=float,
        default=0.07,
        help="Fraction of train images to sample",
    )
    parser.add_argument(
        "--val-rate", type=float, default=0.05, help="Fraction of val images to sample"
    )
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    random.seed(args.seed)

    train_dir = str(args.dataset_dir / "images" / "train")
    val_dir = str(args.dataset_dir / "images" / "val")

    logger.info("=== Scanning train ===")
    train_files = sample_jpgs(train_dir, args.train_rate, "Train")

    logger.info("=== Scanning val ===")
    val_files = sample_jpgs(val_dir, args.val_rate, "Val")

    train_txt = args.dataset_dir / "train.txt"
    with open(train_txt, "w") as f:
        for p in train_files:
            f.write(p.replace("\\", "/") + "\n")
    logger.info("Wrote %s: %d entries", train_txt, len(train_files))

    val_txt = args.dataset_dir / "val.txt"
    with open(val_txt, "w") as f:
        for p in val_files:
            f.write(p.replace("\\", "/") + "\n")
    logger.info("Wrote %s: %d entries", val_txt, len(val_files))


if __name__ == "__main__":
    main()
