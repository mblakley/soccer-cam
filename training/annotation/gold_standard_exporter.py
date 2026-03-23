"""Export gold-standard annotations to YOLO label format.

Unlike correction_ingester.py (which maps crop coords back to panoramic),
gold-standard tiles ARE the 640x640 images used directly in training.
Annotations map directly to YOLO normalized coordinates.

Usage:
    python -m training.annotation.gold_standard_exporter \
        --packets review_packets \
        --output F:/training_data/ball_dataset_640/labels_gold
"""

import argparse
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

TILE_SIZE = 640
BALL_BBOX_SIZE = 20  # pixels, ball diameter in 640x640 tiles


def export_gold_labels(
    packets_dir: Path,
    output_dir: Path,
    bbox_size: int = BALL_BBOX_SIZE,
) -> dict:
    """Export gold-standard annotations to YOLO label files.

    For each annotated tile:
    - "locate" action → write label at tap position
    - "not_visible" action → write empty label (confirmed negative)
    - "confirm" action → write label at model detection position
    - "reject" action → write empty label (false positive removed)
    - "skip" → ignored

    Also writes a gold_val.txt file listing all annotated image paths
    for use as a gold-standard validation set.

    Returns:
        Stats dict with counts of each action and files written.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    stats = {
        "total": 0,
        "ball_found": 0,
        "no_ball": 0,
        "not_game_ball": 0,
        "skipped": 0,
        "labels_written": 0,
        "image_paths": [],
    }

    for packet_dir in sorted(packets_dir.iterdir()):
        if not packet_dir.is_dir():
            continue

        manifest_path = packet_dir / "manifest.json"
        results_path = packet_dir / "annotation_results.json"

        if not manifest_path.exists() or not results_path.exists():
            continue

        with open(manifest_path) as f:
            manifest = json.load(f)

        # Only process gold-standard packets
        if manifest.get("packet_type") != "gold_standard":
            continue

        with open(results_path) as f:
            results = json.load(f)

        frame_lookup = {fr["frame_idx"]: fr for fr in manifest["frames"]}
        results_by_frame = {r["frame_idx"]: r for r in results}

        for frame_idx, result in results_by_frame.items():
            frame = frame_lookup.get(frame_idx)
            if not frame:
                continue

            stats["total"] += 1
            action = result["action"]
            context = frame.get("context", {})
            original_filename = context.get("original_filename", "")
            game_id = context.get("game_id", "")

            # Determine output label path (mirrors dataset structure)
            label_subdir = output_dir / game_id
            label_subdir.mkdir(parents=True, exist_ok=True)
            label_stem = Path(original_filename).stem
            label_path = label_subdir / f"{label_stem}.txt"

            if action in ("locate", "adjust"):
                # User tapped ball position
                ball_pos = result.get("ball_position")
                if ball_pos:
                    cx_norm = ball_pos["x"] / TILE_SIZE
                    cy_norm = ball_pos["y"] / TILE_SIZE
                    w_norm = bbox_size / TILE_SIZE
                    h_norm = bbox_size / TILE_SIZE
                    cx_norm = max(0.0, min(1.0, cx_norm))
                    cy_norm = max(0.0, min(1.0, cy_norm))
                    with open(label_path, "w") as f:
                        f.write(
                            f"0 {cx_norm:.6f} {cy_norm:.6f} {w_norm:.6f} {h_norm:.6f}\n"
                        )
                    stats["ball_found"] += 1
                    stats["labels_written"] += 1
                    stats["image_paths"].append((game_id, original_filename, True))

            elif action == "confirm":
                # User confirmed model detection
                det = frame.get("model_detection")
                if det:
                    cx_norm = det["x"] / TILE_SIZE
                    cy_norm = det["y"] / TILE_SIZE
                    w_norm = bbox_size / TILE_SIZE
                    h_norm = bbox_size / TILE_SIZE
                    cx_norm = max(0.0, min(1.0, cx_norm))
                    cy_norm = max(0.0, min(1.0, cy_norm))
                    with open(label_path, "w") as f:
                        f.write(
                            f"0 {cx_norm:.6f} {cy_norm:.6f} {w_norm:.6f} {h_norm:.6f}\n"
                        )
                    stats["ball_found"] += 1
                    stats["labels_written"] += 1
                    stats["image_paths"].append((game_id, original_filename, True))

            elif action in ("not_visible", "reject"):
                # Confirmed no ball / false positive
                label_path.touch()  # empty file = negative example
                stats["no_ball"] += 1
                stats["labels_written"] += 1
                stats["image_paths"].append((game_id, original_filename, False))

            elif action == "not_game_ball":
                # Real ball detected but not the game ball (sideline, spectator, etc.)
                # Write empty label — correct detection but wrong ball for our task
                label_path.touch()
                stats["not_game_ball"] += 1
                stats["labels_written"] += 1
                stats["image_paths"].append((game_id, original_filename, False))

            elif action == "skip":
                stats["skipped"] += 1

    # Write gold_val.txt listing all annotated images
    if stats["image_paths"]:
        gold_val_path = output_dir / "gold_val.txt"
        with open(gold_val_path, "w") as f:
            for game_id, filename, _has_ball in sorted(stats["image_paths"]):
                # Point to the image in the organized dataset
                # Try val first, then train
                for split in ("val", "train"):
                    img_path = output_dir.parent / "images" / split / game_id / filename
                    if img_path.exists():
                        f.write(str(img_path) + "\n")
                        break
                else:
                    logger.warning(
                        "Image not found in dataset: %s/%s", game_id, filename
                    )

    del stats["image_paths"]
    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Export gold-standard annotations to YOLO labels"
    )
    parser.add_argument(
        "--packets",
        type=Path,
        default=Path("review_packets"),
        help="Review packets directory",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("F:/training_data/ball_dataset_640/labels_gold"),
        help="Output directory for gold labels",
    )
    parser.add_argument(
        "--bbox-size",
        type=int,
        default=BALL_BBOX_SIZE,
        help="Ball bounding box size in pixels",
    )

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    stats = export_gold_labels(args.packets, args.output, args.bbox_size)
    print("Exported gold-standard labels:")
    print(f"  Ball found: {stats['ball_found']}")
    print(f"  No ball: {stats['no_ball']}")
    print(f"  Not game ball: {stats['not_game_ball']}")
    print(f"  Skipped: {stats['skipped']}")
    print(f"  Labels written: {stats['labels_written']}")


if __name__ == "__main__":
    main()
