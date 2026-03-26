"""Filter training labels using per-game field mask polygons.

Applies a soft field mask to labels_640_clean: keeps on-field labels,
downsamples near-off-field labels (within 150px of boundary), and removes
far-off-field labels (>150px outside boundary).

The ball legitimately leaves the field during throw-ins, goal kicks, and
high kicks, so we don't remove ALL off-field labels — just the ones that
are clearly spectator/parking lot noise.

Usage:
    uv run python -m training.data_prep.field_mask_filter
    uv run python -m training.data_prep.field_mask_filter --games heat__05.31.2024_vs_Fairport_home
"""

import argparse
import json
import logging
import random
import shutil
from pathlib import Path

import cv2
import numpy as np

from training.data_prep.organize_dataset import parse_tile_filename
from training.data_prep.trajectory_validator import _tile_to_pano

logger = logging.getLogger(__name__)

DEFAULT_LABELS_DIR = Path("F:/training_data/labels_640_clean")
DEFAULT_OUTPUT_DIR = Path("F:/training_data/labels_640_field_filtered")
DEFAULT_MASKS_DIR = Path("F:/training_data/label_qa")

# Distance thresholds for soft field mask filtering
NEAR_OFF_FIELD_MARGIN = (
    150  # px: within this distance outside polygon = "near off-field"
)
NEAR_OFF_FIELD_KEEP_RATE = 0.2  # Keep 20% of near-off-field labels


def load_polygon(mask_path: Path) -> np.ndarray | None:
    """Load and pre-reshape a field mask polygon for cv2."""
    if not mask_path.exists():
        return None
    polygon = json.loads(mask_path.read_text())
    return np.asarray(polygon, dtype=np.float32).reshape(-1, 1, 2)


def classify_label_position(
    cx: float,
    cy: float,
    row: int,
    col: int,
    polygon: np.ndarray,
) -> str:
    """Classify a label as on-field, near-off-field, or far-off-field.

    Returns: "on_field", "near_off_field", or "far_off_field"
    """
    pano_x, pano_y = _tile_to_pano(cx, cy, row, col)
    dist = cv2.pointPolygonTest(polygon, (pano_x, pano_y), measureDist=True)

    if dist >= 0:
        return "on_field"
    elif dist >= -NEAR_OFF_FIELD_MARGIN:
        return "near_off_field"
    else:
        return "far_off_field"


def filter_game(
    labels_dir: Path,
    output_dir: Path,
    game_id: str,
    polygon: np.ndarray,
    seed: int = 42,
) -> dict:
    """Filter labels for one game using the field mask polygon."""
    game_in = labels_dir / game_id
    game_out = output_dir / game_id
    game_out.mkdir(parents=True, exist_ok=True)

    rng = random.Random(seed)
    stats = {
        "on_field": 0,
        "near_off_field_kept": 0,
        "near_off_field_dropped": 0,
        "far_off_field": 0,
        "empty": 0,
        "parse_error": 0,
    }

    for label_file in game_in.iterdir():
        if label_file.suffix != ".txt":
            continue

        stem = label_file.stem
        parsed = parse_tile_filename(stem)
        if parsed is None:
            stats["parse_error"] += 1
            continue

        _segment, _frame_idx, row, col = parsed
        content = label_file.read_text().strip()

        if not content:
            # Empty label = negative example, always copy
            shutil.copy2(label_file, game_out / label_file.name)
            stats["empty"] += 1
            continue

        parts = content.split()
        if len(parts) < 5:
            stats["parse_error"] += 1
            continue

        cx, cy = float(parts[1]), float(parts[2])
        position = classify_label_position(cx, cy, row, col, polygon)

        if position == "on_field":
            shutil.copy2(label_file, game_out / label_file.name)
            stats["on_field"] += 1
        elif position == "near_off_field":
            if rng.random() < NEAR_OFF_FIELD_KEEP_RATE:
                shutil.copy2(label_file, game_out / label_file.name)
                stats["near_off_field_kept"] += 1
            else:
                stats["near_off_field_dropped"] += 1
        else:
            stats["far_off_field"] += 1

    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Filter training labels with field mask polygons"
    )
    parser.add_argument(
        "--labels",
        type=Path,
        default=DEFAULT_LABELS_DIR,
        help="Input labels directory (default: %(default)s)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Output filtered labels directory (default: %(default)s)",
    )
    parser.add_argument(
        "--masks",
        type=Path,
        default=DEFAULT_MASKS_DIR,
        help="Directory containing per-game field_mask.json files (default: %(default)s)",
    )
    parser.add_argument("--games", nargs="+", help="Only filter specific games")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    # Discover games
    if args.games:
        game_ids = args.games
    else:
        game_ids = sorted(d.name for d in args.labels.iterdir() if d.is_dir())

    for game_id in game_ids:
        mask_path = args.masks / game_id / "field_mask.json"
        polygon = load_polygon(mask_path)
        if polygon is None:
            logger.warning("No field mask for %s, copying all labels", game_id)
            game_in = args.labels / game_id
            game_out = args.output / game_id
            if game_in.exists():
                shutil.copytree(game_in, game_out, dirs_exist_ok=True)
            continue

        stats = filter_game(args.labels, args.output, game_id, polygon)
        logger.info(
            "%s: %d on-field, %d near-off kept, %d near-off dropped, %d far-off removed",
            game_id,
            stats["on_field"],
            stats["near_off_field_kept"],
            stats["near_off_field_dropped"],
            stats["far_off_field"],
        )


if __name__ == "__main__":
    main()
