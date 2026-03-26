"""Heuristic pre-filter for bootstrap ball labels.

Removes obviously wrong detections based on aspect ratio, size, and edge clipping.
Reads from labels_640/{game}/, writes to labels_640_filtered/{game}/.
"""

import argparse
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Aspect ratio: ball should be roughly round
MIN_ASPECT = 0.5
MAX_ASPECT = 2.0

# Size in normalized coords: ~5-38px in a 640px tile
MIN_WIDTH = 0.008  # ~5px / 640
MAX_WIDTH = 0.06  # ~38px / 640

# Edge margin: reject boxes that extend outside the tile
EDGE_MARGIN = 0.0  # normalized; 0 means reject if any part outside [0, 1]


def filter_label_file(
    label_path: Path,
    min_aspect: float = MIN_ASPECT,
    max_aspect: float = MAX_ASPECT,
    min_width: float = MIN_WIDTH,
    max_width: float = MAX_WIDTH,
) -> list[str]:
    """Filter detections in a single YOLO label file.

    Returns list of valid label lines (without trailing newline).
    """
    kept = []
    with open(label_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 5:
                continue

            cx, cy, w, h = (
                float(parts[1]),
                float(parts[2]),
                float(parts[3]),
                float(parts[4]),
            )

            # Aspect ratio check
            if h > 0:
                aspect = w / h
                if aspect < min_aspect or aspect > max_aspect:
                    continue
            else:
                continue

            # Size check
            if w < min_width or w > max_width:
                continue
            if h < min_width or h > max_width:
                continue

            # Edge clipping: bbox must be fully inside [0, 1]
            x1 = cx - w / 2
            y1 = cy - h / 2
            x2 = cx + w / 2
            y2 = cy + h / 2
            if (
                x1 < EDGE_MARGIN
                or y1 < EDGE_MARGIN
                or x2 > 1.0 - EDGE_MARGIN
                or y2 > 1.0 - EDGE_MARGIN
            ):
                continue

            kept.append(line)

    return kept


def filter_labels(
    input_dir: Path,
    output_dir: Path,
    min_aspect: float = MIN_ASPECT,
    max_aspect: float = MAX_ASPECT,
    min_width: float = MIN_WIDTH,
    max_width: float = MAX_WIDTH,
) -> dict[str, int]:
    """Filter all labels in input_dir, write survivors to output_dir.

    Preserves game subdirectory structure.

    Returns:
        Dict with counts: {"files_in", "files_out", "detections_in", "detections_out"}
    """
    stats = {"files_in": 0, "files_out": 0, "detections_in": 0, "detections_out": 0}
    start_time = time.time()
    last_log_time = start_time

    game_dirs = sorted([d for d in input_dir.iterdir() if d.is_dir()])
    if not game_dirs:
        game_dirs = [input_dir]

    for game_dir in game_dirs:
        game_id = game_dir.name if game_dir != input_dir else "default"
        out_game_dir = output_dir / game_id
        out_game_dir.mkdir(parents=True, exist_ok=True)

        game_in = 0
        game_out = 0

        for label_path in sorted(game_dir.glob("*.txt")):
            stats["files_in"] += 1

            with open(label_path) as f:
                original_count = sum(1 for line in f if line.strip())
            stats["detections_in"] += original_count

            kept = filter_label_file(
                label_path, min_aspect, max_aspect, min_width, max_width
            )
            stats["detections_out"] += len(kept)

            if kept:
                out_path = out_game_dir / label_path.name
                with open(out_path, "w") as f:
                    for line in kept:
                        f.write(line + "\n")
                stats["files_out"] += 1
                game_out += 1

            game_in += 1

        now = time.time()
        if now - last_log_time >= 10 or game_dir == game_dirs[-1]:
            logger.info(
                "  %s: %d/%d files kept, %d/%d detections kept",
                game_id,
                game_out,
                game_in,
                stats["detections_out"],
                stats["detections_in"],
            )
            last_log_time = now

    elapsed = time.time() - start_time
    removed = stats["detections_in"] - stats["detections_out"]
    logger.info(
        "=== COMPLETE: %d→%d files, %d→%d detections (removed %d, %.1f%%) in %.0fs ===",
        stats["files_in"],
        stats["files_out"],
        stats["detections_in"],
        stats["detections_out"],
        removed,
        removed / max(stats["detections_in"], 1) * 100,
        elapsed,
    )
    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Filter bootstrap labels by heuristics"
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("F:/training_data/labels_640"),
        help="Input labels directory",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("F:/training_data/labels_640_filtered"),
        help="Output filtered labels directory",
    )
    parser.add_argument("--min-aspect", type=float, default=MIN_ASPECT)
    parser.add_argument("--max-aspect", type=float, default=MAX_ASPECT)
    parser.add_argument("--min-width", type=float, default=MIN_WIDTH)
    parser.add_argument("--max-width", type=float, default=MAX_WIDTH)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    filter_labels(
        args.input,
        args.output,
        args.min_aspect,
        args.max_aspect,
        args.min_width,
        args.max_width,
    )


if __name__ == "__main__":
    main()
