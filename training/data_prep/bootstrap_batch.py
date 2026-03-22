"""Run bootstrap labeling game-by-game to avoid huge file scans."""

import argparse
import logging
import time
from pathlib import Path

from training.data_prep.bootstrap_labels import bootstrap_labels
from training.data_prep.organize_dataset import DEFAULT_EXCLUDE_ROWS

logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Bootstrap labels game by game")
    parser.add_argument(
        "--tiles-dir",
        type=Path,
        default=Path("F:/training_data/tiles_640"),
    )
    parser.add_argument(
        "--labels-dir",
        type=Path,
        default=Path("F:/training_data/labels_640"),
    )
    parser.add_argument("--model", default="yolo11x.pt")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--confidence", type=float, default=0.1)
    parser.add_argument(
        "--start", type=int, default=0, help="Start index (for resuming)"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    game_dirs = sorted(d for d in args.tiles_dir.iterdir() if d.is_dir())
    total_games = len(game_dirs)
    logger.info("Found %d game directories", total_games)

    overall_start = time.time()
    total_tiles = 0
    total_detections = 0

    for i, game_dir in enumerate(game_dirs[args.start :], start=args.start + 1):
        game_id = game_dir.name
        labels_out = args.labels_dir / game_id
        logger.info("=== [%d/%d] %s ===", i, total_games, game_id)

        stats = bootstrap_labels(
            game_dir,
            labels_out,
            model_name=args.model,
            confidence=args.confidence,
            batch_size=args.batch_size,
            exclude_rows=DEFAULT_EXCLUDE_ROWS,
        )
        total_tiles += stats["tiles_processed"]
        total_detections += stats["total_detections"]

        elapsed = time.time() - overall_start
        logger.info(
            "Running total: %d tiles, %d detections in %.0f min",
            total_tiles,
            total_detections,
            elapsed / 60,
        )

    elapsed = time.time() - overall_start
    logger.info(
        "=== ALL COMPLETE: %d tiles, %d detections in %.0f min ===",
        total_tiles,
        total_detections,
        elapsed / 60,
    )


if __name__ == "__main__":
    main()
