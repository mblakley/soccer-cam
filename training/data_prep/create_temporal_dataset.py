"""Build triplet manifests for temporal (3-frame) ball detection training.

For each clean label, finds the same tile position in adjacent frames
(frame_idx ± 8) and creates a training triplet entry. The manifest is a
JSON-lines file that tells the temporal dataloader which 3 files to load.

No new image files are created — only a manifest index.
"""

import argparse
import json
import logging
import os
import time
from pathlib import Path

from training.data_prep.organize_dataset import parse_tile_filename, parse_tile_position

logger = logging.getLogger(__name__)

DEFAULT_FRAME_INTERVAL = 8  # matches extract_frames.py interval
DEFAULT_NEG_RATIO = 0.5  # negative triplets per positive triplet


def _scan_tile_index(
    tiles_dir: Path, exclude_rows: set[int] | None = None
) -> dict[str, Path]:
    """Build a lookup from 'game/segment_frame_NNNNNN_rR_cC' → tile path."""
    if exclude_rows is None:
        exclude_rows = {0}

    index: dict[str, Path] = {}
    for game_dir in sorted(d for d in tiles_dir.iterdir() if d.is_dir()):
        game_id = game_dir.name
        for entry in os.scandir(game_dir):
            if not entry.name.endswith(".jpg"):
                continue
            stem = Path(entry.name).stem
            pos = parse_tile_position(stem)
            if pos and pos[0] in exclude_rows:
                continue
            index[f"{game_id}/{stem}"] = Path(entry.path)
    return index


def _parse_label_index(
    labels_dir: Path,
) -> list[tuple[str, str, int, int, int, float, float]]:
    """Parse all clean labels into structured data.

    Returns list of (game_id, segment, frame_idx, row, col, cx, cy).
    """
    entries = []
    for game_dir in sorted(d for d in labels_dir.iterdir() if d.is_dir()):
        game_id = game_dir.name
        for entry in os.scandir(game_dir):
            if not entry.name.endswith(".txt"):
                continue
            stem = Path(entry.name).stem
            parsed = parse_tile_filename(stem)
            if parsed is None:
                continue
            segment, frame_idx, row, col = parsed

            # Read label to get ball center
            with open(entry.path) as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) >= 5:
                        cx, cy = float(parts[1]), float(parts[2])
                        entries.append((game_id, segment, frame_idx, row, col, cx, cy))
    return entries


def create_temporal_dataset(
    tiles_dir: Path,
    labels_dir: Path,
    output_path: Path,
    frame_interval: int = DEFAULT_FRAME_INTERVAL,
    neg_ratio: float = DEFAULT_NEG_RATIO,
    exclude_rows: set[int] | None = None,
    seed: int = 42,
) -> dict[str, int]:
    """Create a triplet manifest for temporal model training.

    Each line in the manifest is a JSON object:
    {
        "prev": "path/to/frame_N-8_rR_cC.jpg",
        "curr": "path/to/frame_N_rR_cC.jpg",
        "next": "path/to/frame_N+8_rR_cC.jpg",
        "cx": 0.45,   // normalized ball center x (or null for negative)
        "cy": 0.62,   // normalized ball center y (or null for negative)
        "positive": true
    }

    Args:
        tiles_dir: Root tiles directory
        labels_dir: Root clean labels directory
        output_path: Path for the output manifest (.jsonl)
        frame_interval: Frame gap between adjacent frames (default 8)
        neg_ratio: Ratio of negative to positive triplets
        exclude_rows: Tile rows to exclude
        seed: Random seed

    Returns:
        Dict with counts.
    """
    import random

    random.seed(seed)
    if exclude_rows is None:
        exclude_rows = {0}

    start_time = time.time()

    logger.info("Building tile index...")
    tile_index = _scan_tile_index(tiles_dir, exclude_rows)
    logger.info("Indexed %d tiles", len(tile_index))

    logger.info("Parsing clean labels...")
    label_entries = _parse_label_index(labels_dir)
    logger.info("Found %d label entries", len(label_entries))

    # Build positive triplets
    positives = []
    skipped = 0
    for game_id, segment, frame_idx, row, col, cx, cy in label_entries:
        curr_key = f"{game_id}/{segment}_frame_{frame_idx:06d}_r{row}_c{col}"
        prev_key = (
            f"{game_id}/{segment}_frame_{frame_idx - frame_interval:06d}_r{row}_c{col}"
        )
        next_key = (
            f"{game_id}/{segment}_frame_{frame_idx + frame_interval:06d}_r{row}_c{col}"
        )

        if curr_key not in tile_index:
            skipped += 1
            continue
        if prev_key not in tile_index or next_key not in tile_index:
            skipped += 1
            continue

        positives.append(
            {
                "prev": str(tile_index[prev_key]).replace("\\", "/"),
                "curr": str(tile_index[curr_key]).replace("\\", "/"),
                "next": str(tile_index[next_key]).replace("\\", "/"),
                "cx": cx,
                "cy": cy,
                "positive": True,
            }
        )

    logger.info(
        "Built %d positive triplets (%d skipped — missing adjacent frames)",
        len(positives),
        skipped,
    )

    # Build negative triplets from tiles without labels
    label_stems = set()
    for game_id, segment, frame_idx, row, col, _, _ in label_entries:
        label_stems.add(f"{game_id}/{segment}_frame_{frame_idx:06d}_r{row}_c{col}")

    # Candidate negative tiles: not in label set
    neg_candidates = [k for k in tile_index if k not in label_stems]
    random.shuffle(neg_candidates)

    n_negatives = int(len(positives) * neg_ratio)
    negatives = []

    for key in neg_candidates:
        if len(negatives) >= n_negatives:
            break

        parsed = parse_tile_filename(key.split("/", 1)[1])
        if parsed is None:
            continue
        segment, frame_idx, row, col = parsed
        game_id = key.split("/")[0]

        prev_key = (
            f"{game_id}/{segment}_frame_{frame_idx - frame_interval:06d}_r{row}_c{col}"
        )
        next_key = (
            f"{game_id}/{segment}_frame_{frame_idx + frame_interval:06d}_r{row}_c{col}"
        )

        if prev_key not in tile_index or next_key not in tile_index:
            continue

        negatives.append(
            {
                "prev": str(tile_index[prev_key]).replace("\\", "/"),
                "curr": str(tile_index[key]).replace("\\", "/"),
                "next": str(tile_index[next_key]).replace("\\", "/"),
                "cx": None,
                "cy": None,
                "positive": False,
            }
        )

    logger.info("Built %d negative triplets", len(negatives))

    # Combine, shuffle, and write
    all_triplets = positives + negatives
    random.shuffle(all_triplets)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w") as f:
        for entry in all_triplets:
            f.write(json.dumps(entry) + "\n")

    elapsed = time.time() - start_time
    logger.info(
        "=== COMPLETE: %d triplets (%d pos, %d neg) written to %s in %.0fs ===",
        len(all_triplets),
        len(positives),
        len(negatives),
        output_path,
        elapsed,
    )

    return {
        "positives": len(positives),
        "negatives": len(negatives),
        "total": len(all_triplets),
        "skipped": skipped,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Create triplet manifest for temporal ball detection"
    )
    parser.add_argument(
        "--tiles",
        type=Path,
        default=Path("F:/training_data/tiles_640"),
        help="Root tiles directory",
    )
    parser.add_argument(
        "--labels",
        type=Path,
        default=Path("F:/training_data/labels_640_clean"),
        help="Root clean labels directory",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("F:/training_data/temporal_triplets.jsonl"),
        help="Output manifest path",
    )
    parser.add_argument(
        "--frame-interval",
        type=int,
        default=DEFAULT_FRAME_INTERVAL,
        help="Frame gap between adjacent frames",
    )
    parser.add_argument(
        "--neg-ratio",
        type=float,
        default=DEFAULT_NEG_RATIO,
        help="Ratio of negative to positive triplets",
    )
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    create_temporal_dataset(
        args.tiles,
        args.labels,
        args.output,
        args.frame_interval,
        args.neg_ratio,
        seed=args.seed,
    )


if __name__ == "__main__":
    main()
