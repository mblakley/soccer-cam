"""Physics-based trajectory validation for ball labels.

Links detections across consecutive frames and keeps only those that form
plausible trajectories (≥3 frames). Removes isolated false positives.

Reads from labels_640_filtered/{game}/, writes to labels_640_clean/{game}/.
"""

import argparse
import logging
import time
from collections import defaultdict
from pathlib import Path

from training.data_prep.organize_dataset import parse_tile_filename

logger = logging.getLogger(__name__)

# Tile layout for panoramic→tile coordinate conversion
TILE_SIZE = 640
STEP_X = 576  # (4096 - 640) / (7 - 1)
STEP_Y = 580  # (1800 - 640) / (3 - 1)

# Maximum distance (in panoramic pixels) between detections in consecutive frames
MAX_LINK_DISTANCE = 400

# Minimum trajectory length (frames) to keep a detection
MIN_TRAJECTORY_LENGTH = 3


def _parse_detection(label_path: Path) -> list[tuple[float, float, str]]:
    """Parse all detections from a YOLO label file.

    Returns list of (cx_norm, cy_norm, full_line) tuples.
    """
    detections = []
    with open(label_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 5:
                cx, cy = float(parts[1]), float(parts[2])
                detections.append((cx, cy, line))
    return detections


def _tile_to_pano(
    cx_norm: float, cy_norm: float, row: int, col: int
) -> tuple[float, float]:
    """Convert normalized tile coordinates to panoramic pixel coordinates."""
    pano_x = col * STEP_X + cx_norm * TILE_SIZE
    pano_y = row * STEP_Y + cy_norm * TILE_SIZE
    return pano_x, pano_y


def validate_game(
    input_dir: Path,
    output_dir: Path,
    max_distance: float = MAX_LINK_DISTANCE,
    min_trajectory: int = MIN_TRAJECTORY_LENGTH,
) -> dict[str, int]:
    """Validate labels for a single game directory.

    Returns counts: {"detections_in", "detections_out", "trajectories_found"}
    """
    stats = {"detections_in": 0, "detections_out": 0, "trajectories_found": 0}

    # Phase 1: Parse all labels into panoramic detections
    # Key: (segment, frame_idx) → list of (pano_x, pano_y, label_path, line)
    frame_detections: dict[tuple[str, int], list[tuple[float, float, Path, str]]] = (
        defaultdict(list)
    )

    for label_path in sorted(input_dir.glob("*.txt")):
        parsed = parse_tile_filename(label_path.stem)
        if parsed is None:
            continue

        segment, frame_idx, row, col = parsed
        for cx_norm, cy_norm, line in _parse_detection(label_path):
            pano_x, pano_y = _tile_to_pano(cx_norm, cy_norm, row, col)
            frame_detections[(segment, frame_idx)].append(
                (pano_x, pano_y, label_path, line)
            )
            stats["detections_in"] += 1

    if not frame_detections:
        return stats

    # Phase 2: Link detections across consecutive frames per segment
    # Group frame indices by segment
    segment_frames: dict[str, list[int]] = defaultdict(list)
    for segment, frame_idx in frame_detections:
        segment_frames[segment].append(frame_idx)

    for seg in segment_frames:
        segment_frames[seg] = sorted(set(segment_frames[seg]))

    # Track which (label_path, line) pairs are in valid trajectories
    valid_detections: set[tuple[str, str]] = set()  # (label_path_str, line)

    # Auto-detect frame interval from the data (typically 8)
    all_frame_indices = set()
    for _seg, fi in frame_detections:
        all_frame_indices.add(fi)
    sorted_all = sorted(all_frame_indices)
    if len(sorted_all) >= 2:
        gaps = [sorted_all[i + 1] - sorted_all[i] for i in range(min(100, len(sorted_all) - 1))]
        gaps = [g for g in gaps if g > 0]
        frame_interval = min(gaps) if gaps else 8
    else:
        frame_interval = 8
    # Allow linking across up to 2 intervals (to tolerate 1 missing frame)
    max_frame_gap = frame_interval * 2 + frame_interval // 2

    for segment, frame_indices in segment_frames.items():
        # Build trajectories greedily
        # Each trajectory is a list of (frame_idx, pano_x, pano_y, label_path, line)
        active_trajectories: list[list[tuple[int, float, float, Path, str]]] = []
        finished_trajectories: list[list[tuple[int, float, float, Path, str]]] = []

        for fi in frame_indices:
            dets = frame_detections[(segment, fi)]
            used = [False] * len(dets)

            # Try to extend existing trajectories
            new_active = []
            for traj in active_trajectories:
                last_fi, last_x, last_y, _, _ = traj[-1]

                # Only link to nearby frames (allow gap of ~2 intervals)
                frame_gap = fi - last_fi
                if frame_gap > max_frame_gap or frame_gap <= 0:
                    # Trajectory ended — check if long enough
                    finished_trajectories.append(traj)
                    continue

                # Find closest unmatched detection within distance threshold
                # Scale threshold by number of intervals elapsed
                n_intervals = max(frame_gap / frame_interval, 1)
                best_idx = -1
                best_dist = max_distance * n_intervals
                for i, (px, py, lp, ln) in enumerate(dets):
                    if used[i]:
                        continue
                    dist = ((px - last_x) ** 2 + (py - last_y) ** 2) ** 0.5
                    if dist < best_dist:
                        best_dist = dist
                        best_idx = i

                if best_idx >= 0:
                    px, py, lp, ln = dets[best_idx]
                    traj.append((fi, px, py, lp, ln))
                    used[best_idx] = True
                    new_active.append(traj)
                else:
                    finished_trajectories.append(traj)

            # Start new trajectories from unmatched detections
            for i, (px, py, lp, ln) in enumerate(dets):
                if not used[i]:
                    new_active.append([(fi, px, py, lp, ln)])

            active_trajectories = new_active

        # Finalize remaining active trajectories
        finished_trajectories.extend(active_trajectories)

        # Mark valid detections from trajectories meeting minimum length
        for traj in finished_trajectories:
            if len(traj) >= min_trajectory:
                stats["trajectories_found"] += 1
                for _, _, _, label_path, line in traj:
                    valid_detections.add((str(label_path), line))

    # Phase 3: Write clean labels
    output_dir.mkdir(parents=True, exist_ok=True)

    # Group valid detections by label file
    file_lines: dict[str, list[str]] = defaultdict(list)
    for label_path_str, line in valid_detections:
        file_lines[label_path_str].append(line)

    for label_path_str, lines in file_lines.items():
        label_path = Path(label_path_str)
        out_path = output_dir / label_path.name
        with open(out_path, "w") as f:
            for line in sorted(set(lines)):
                f.write(line + "\n")
        stats["detections_out"] += len(set(lines))

    return stats


def validate_trajectories(
    input_dir: Path,
    output_dir: Path,
    max_distance: float = MAX_LINK_DISTANCE,
    min_trajectory: int = MIN_TRAJECTORY_LENGTH,
) -> dict[str, int]:
    """Validate labels across all games.

    Args:
        input_dir: Root of filtered labels (with game subdirs)
        output_dir: Root for clean labels output
        max_distance: Max panoramic pixel distance to link detections
        min_trajectory: Minimum trajectory length in frames

    Returns:
        Aggregate stats across all games.
    """
    totals = {"detections_in": 0, "detections_out": 0, "trajectories_found": 0}
    start_time = time.time()

    game_dirs = sorted([d for d in input_dir.iterdir() if d.is_dir()])
    if not game_dirs:
        game_dirs = [input_dir]

    for game_dir in game_dirs:
        game_id = game_dir.name if game_dir != input_dir else "default"
        out_game_dir = output_dir / game_id

        game_stats = validate_game(game_dir, out_game_dir, max_distance, min_trajectory)
        for k in totals:
            totals[k] += game_stats[k]

        logger.info(
            "  %s: %d→%d detections, %d trajectories",
            game_id,
            game_stats["detections_in"],
            game_stats["detections_out"],
            game_stats["trajectories_found"],
        )

    elapsed = time.time() - start_time
    removed = totals["detections_in"] - totals["detections_out"]
    logger.info(
        "=== COMPLETE: %d→%d detections (removed %d, %.1f%%), %d trajectories in %.0fs ===",
        totals["detections_in"],
        totals["detections_out"],
        removed,
        removed / max(totals["detections_in"], 1) * 100,
        totals["trajectories_found"],
        elapsed,
    )
    return totals


def main():
    parser = argparse.ArgumentParser(
        description="Validate labels using trajectory analysis"
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("F:/training_data/labels_640_filtered"),
        help="Input filtered labels directory",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("F:/training_data/labels_640_clean"),
        help="Output clean labels directory",
    )
    parser.add_argument(
        "--max-distance",
        type=float,
        default=MAX_LINK_DISTANCE,
        help="Max panoramic pixel distance to link detections",
    )
    parser.add_argument(
        "--min-trajectory",
        type=int,
        default=MIN_TRAJECTORY_LENGTH,
        help="Minimum trajectory length in frames",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    validate_trajectories(
        args.input, args.output, args.max_distance, args.min_trajectory
    )


if __name__ == "__main__":
    main()
