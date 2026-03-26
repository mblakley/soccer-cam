"""Bootstrap person annotations using pretrained YOLO on COCO person class.

Runs a pretrained YOLO model on tiles to detect people. Person positions
are used to correlate with ball trajectories — the game ball should have
players clustered around it, while sideline balls don't.

Usage:
    python -m training.data_prep.bootstrap_persons \
        F:/training_data/tiles_640 \
        -o F:/training_data/labels_640_persons \
        --model yolo11m.pt --confidence 0.3
"""

import argparse
import logging
import time
from pathlib import Path

from training.data_prep.organize_dataset import parse_tile_position

logger = logging.getLogger(__name__)

PERSON_CLASS = 0  # COCO class index for person
PERSON_CLASS_OUTPUT = 0  # Output class index
DEFAULT_CONFIDENCE = 0.3  # Higher than ball — people are easier to detect
DEFAULT_BATCH_SIZE = 32


def bootstrap_persons(
    tiles_dir: Path,
    labels_dir: Path,
    model_name: str = "yolo11m.pt",
    confidence: float = DEFAULT_CONFIDENCE,
    batch_size: int = DEFAULT_BATCH_SIZE,
    exclude_rows: set[int] | None = None,
) -> dict[str, int]:
    """Run pretrained YOLO on tiles and export person detections as YOLO labels.

    Args:
        tiles_dir: Directory containing tile images (can be a game subdir)
        labels_dir: Output directory for YOLO-format label files
        model_name: Pretrained YOLO model to use
        confidence: Minimum detection confidence threshold
        batch_size: Number of tiles to process per batch

    Returns:
        Dict with counts: {"tiles_processed", "tiles_with_persons", "total_detections"}
    """
    from ultralytics import YOLO

    model = YOLO(model_name)
    labels_dir.mkdir(parents=True, exist_ok=True)

    if exclude_rows is None:
        exclude_rows = set()

    all_paths = sorted(tiles_dir.rglob("*.jpg"))
    if exclude_rows:
        tile_paths = [
            p
            for p in all_paths
            if (pos := parse_tile_position(p.stem)) is None
            or pos[0] not in exclude_rows
        ]
        logger.info(
            "Filtered %d -> %d tiles (excluded rows %s)",
            len(all_paths),
            len(tile_paths),
            exclude_rows,
        )
    else:
        tile_paths = all_paths
    logger.info(
        "Processing %d tiles with %s (conf=%.2f, batch=%d)",
        len(tile_paths),
        model_name,
        confidence,
        batch_size,
    )

    stats = {"tiles_processed": 0, "tiles_with_persons": 0, "total_detections": 0}
    total_tiles = len(tile_paths)
    start_time = time.time()
    last_log_time = start_time

    for batch_start in range(0, total_tiles, batch_size):
        batch_paths = tile_paths[batch_start : batch_start + batch_size]
        batch_strs = [str(p) for p in batch_paths]

        results = model(batch_strs, conf=confidence, verbose=False)

        for tile_path, result in zip(batch_paths, results):
            stats["tiles_processed"] += 1

            detections = []
            for box in result.boxes:
                cls_id = int(box.cls[0])
                if cls_id == PERSON_CLASS:
                    x1, y1, x2, y2 = box.xyxy[0].tolist()
                    img_h, img_w = result.orig_shape
                    cx = ((x1 + x2) / 2) / img_w
                    cy = ((y1 + y2) / 2) / img_h
                    w = (x2 - x1) / img_w
                    h = (y2 - y1) / img_h
                    detections.append((PERSON_CLASS_OUTPUT, cx, cy, w, h))

            if detections:
                label_path = labels_dir / tile_path.relative_to(tiles_dir).with_suffix(
                    ".txt"
                )
                label_path.parent.mkdir(parents=True, exist_ok=True)
                with open(label_path, "w") as f:
                    for cls, cx, cy, w, h in detections:
                        f.write(f"{cls} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}\n")

                stats["tiles_with_persons"] += 1
                stats["total_detections"] += len(detections)

        now = time.time()
        if now - last_log_time >= 30 or stats["tiles_processed"] % 1000 < batch_size:
            elapsed = now - start_time
            rate = stats["tiles_processed"] / elapsed if elapsed > 0 else 0
            remaining = (
                (total_tiles - stats["tiles_processed"]) / rate if rate > 0 else 0
            )
            pct = stats["tiles_processed"] / total_tiles * 100
            logger.info(
                "%d/%d tiles (%.1f%%) | %d persons | %.1f tiles/sec | ETA %.0f min",
                stats["tiles_processed"],
                total_tiles,
                pct,
                stats["total_detections"],
                rate,
                remaining / 60,
            )
            last_log_time = now

    elapsed = time.time() - start_time
    logger.info(
        "=== COMPLETE: %d tiles, %d with persons, %d total detections in %.0f min ===",
        stats["tiles_processed"],
        stats["tiles_with_persons"],
        stats["total_detections"],
        elapsed / 60,
    )
    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Bootstrap person labels using pretrained YOLO"
    )
    parser.add_argument("tiles", type=Path, help="Directory containing tile images")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("F:/training_data/labels_640_persons"),
        help="Output labels dir",
    )
    parser.add_argument("--model", default="yolo11m.pt", help="Pretrained YOLO model")
    parser.add_argument(
        "--confidence",
        type=float,
        default=DEFAULT_CONFIDENCE,
        help="Detection confidence threshold",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="Batch size for inference",
    )
    parser.add_argument(
        "--exclude-rows",
        type=int,
        nargs="*",
        default=[0],
        help="Tile rows to exclude (default: 0 = sky row)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    bootstrap_persons(
        args.tiles,
        args.output,
        args.model,
        args.confidence,
        args.batch_size,
        set(args.exclude_rows) if args.exclude_rows else None,
    )


if __name__ == "__main__":
    main()
