"""Bootstrap ball annotations using pretrained YOLO on COCO sports_ball class.

Runs YOLO26x on tiles at low confidence to auto-label ball positions.
Exports YOLO-format label files for import into CVAT or direct training.
"""

import argparse
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

SPORTS_BALL_CLASS = 32  # COCO class index for sports_ball
BALL_CLASS_OUTPUT = 0  # Our single-class output index
DEFAULT_CONFIDENCE = 0.1  # Low threshold -- prefer false positives over missed balls


def bootstrap_labels(
    tiles_dir: Path,
    labels_dir: Path,
    model_name: str = "yolo26x.pt",
    confidence: float = DEFAULT_CONFIDENCE,
) -> dict[str, int]:
    """Run pretrained YOLO on tiles and export ball detections as YOLO labels.

    Args:
        tiles_dir: Directory containing tile images
        labels_dir: Output directory for YOLO-format label files
        model_name: Pretrained YOLO model to use
        confidence: Minimum detection confidence threshold

    Returns:
        Dict with counts: {"tiles_processed", "tiles_with_balls", "total_detections"}
    """
    from ultralytics import YOLO

    model = YOLO(model_name)
    labels_dir.mkdir(parents=True, exist_ok=True)

    tile_paths = sorted(tiles_dir.rglob("*.jpg"))
    logger.info(
        "Processing %d tiles with %s (conf=%.2f)",
        len(tile_paths),
        model_name,
        confidence,
    )

    stats = {"tiles_processed": 0, "tiles_with_balls": 0, "total_detections": 0}

    for tile_path in tile_paths:
        results = model(str(tile_path), conf=confidence, verbose=False)
        stats["tiles_processed"] += 1

        detections = []
        for result in results:
            for box in result.boxes:
                cls_id = int(box.cls[0])
                if cls_id == SPORTS_BALL_CLASS:
                    # Convert to YOLO format (normalized center_x, center_y, width, height)
                    x1, y1, x2, y2 = box.xyxy[0].tolist()
                    img_h, img_w = result.orig_shape
                    cx = ((x1 + x2) / 2) / img_w
                    cy = ((y1 + y2) / 2) / img_h
                    w = (x2 - x1) / img_w
                    h = (y2 - y1) / img_h
                    conf = float(box.conf[0])
                    detections.append((BALL_CLASS_OUTPUT, cx, cy, w, h, conf))

        if detections:
            # Write YOLO-format label file (class x_center y_center width height)
            label_path = labels_dir / tile_path.relative_to(tiles_dir).with_suffix(
                ".txt"
            )
            label_path.parent.mkdir(parents=True, exist_ok=True)
            with open(label_path, "w") as f:
                for cls, cx, cy, w, h, _conf in detections:
                    f.write(f"{cls} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}\n")

            stats["tiles_with_balls"] += 1
            stats["total_detections"] += len(detections)

        if stats["tiles_processed"] % 500 == 0:
            logger.info(
                "Processed %d/%d tiles (%d detections so far)",
                stats["tiles_processed"],
                len(tile_paths),
                stats["total_detections"],
            )

    logger.info(
        "Done: %d tiles, %d with balls, %d total detections",
        stats["tiles_processed"],
        stats["tiles_with_balls"],
        stats["total_detections"],
    )
    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Bootstrap ball labels using pretrained YOLO"
    )
    parser.add_argument("tiles", type=Path, help="Directory containing tile images")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("training/data/labels"),
        help="Output labels dir",
    )
    parser.add_argument("--model", default="yolo26x.pt", help="Pretrained YOLO model")
    parser.add_argument(
        "--confidence",
        type=float,
        default=DEFAULT_CONFIDENCE,
        help="Detection confidence threshold",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    bootstrap_labels(args.tiles, args.output, args.model, args.confidence)


if __name__ == "__main__":
    main()
