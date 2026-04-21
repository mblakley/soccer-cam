"""Classify ball detections into game_ball, static_ball, not_ball.

Uses trajectory analysis + QA verdicts + field position to auto-classify
all labels. Outputs multi-class YOLO labels (class 0/1/2).

Classes:
    0: game_ball  — ball in active play (part of a moving trajectory)
    1: static_ball — real ball but not in play (practice ball, ball bag, sideline)
    2: not_ball   — false positive (person, equipment, shadow, etc.)

Classification logic:
    1. QA verdicts override everything (human/Sonnet reviewed)
    2. Moving trajectory (≥3 frames, displacement > threshold) → game_ball
    3. Static trajectory (≥3 frames, barely moves) → static_ball
    4. Isolated detection (no trajectory) → not_ball

Usage:
    uv run python -m training.data_prep.label_classifier
    uv run python -m training.data_prep.label_classifier --games heat__Heat_Tournament
"""

import argparse
import logging
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

from training.data_prep.organize_dataset import parse_tile_filename
from training.data_prep.trajectory_validator import (
    MAX_LINK_DISTANCE,
    MIN_TRAJECTORY_LENGTH,
    _parse_detection,
    _tile_to_pano,
)

logger = logging.getLogger(__name__)

# Classification constants
STATIC_DISPLACEMENT_THRESHOLD = 50  # px in panoramic coords — if total trajectory
# displacement < this, it's static
FRAME_INTERVAL = 4  # expected frame interval in the label data

# Class IDs
GAME_BALL = 0
STATIC_BALL = 1
NOT_BALL = 2

# QA verdict → class mapping
VERDICT_CLASS = {
    "TRUE_POSITIVE": GAME_BALL,
    "FP_NOT_GAME_BALL": STATIC_BALL,
    "FP_NOT_BALL": NOT_BALL,
    "FP_OFF_FIELD": NOT_BALL,
    "UNCLEAR": None,  # leave to trajectory analysis
}

DEFAULT_INPUT = Path("F:/training_data/labels_640_ext")
DEFAULT_OUTPUT = Path("F:/training_data/labels_640_classified")
DEFAULT_DB = Path("F:/training_data/label_qa/tile_cache.db")


def load_qa_verdicts(db_path: Path) -> dict[tuple[str, str], int]:
    """Load QA verdicts from SQLite. Returns {(game_id, label_filename): class_id}."""
    verdicts = {}
    if not db_path.exists():
        return verdicts
    conn = sqlite3.connect(str(db_path))
    for row in conn.execute(
        "SELECT game_id, segment, frame_idx, row, col, qa_verdict "
        "FROM labels WHERE qa_verdict IS NOT NULL"
    ):
        game_id, seg, fi, r, c, verdict = row
        cls = VERDICT_CLASS.get(verdict)
        if cls is not None:
            fname = f"{seg}_frame_{fi:06d}_r{r}_c{c}.txt"
            verdicts[(game_id, fname)] = cls
    conn.close()
    logger.info("Loaded %d QA verdicts", len(verdicts))
    return verdicts


def classify_game(
    input_dir: Path,
    output_dir: Path,
    game_id: str,
    qa_verdicts: dict[tuple[str, str], int],
    max_distance: float = MAX_LINK_DISTANCE,
    min_trajectory: int = MIN_TRAJECTORY_LENGTH,
) -> dict[str, int]:
    """Classify all detections in a game directory.

    Returns counts per class.
    """
    stats = {
        "game_ball": 0,
        "static_ball": 0,
        "not_ball": 0,
        "qa_override": 0,
        "total": 0,
    }

    game_out = output_dir / game_id
    game_out.mkdir(parents=True, exist_ok=True)

    # Phase 1: Parse all labels into panoramic detections
    # detection = (pano_x, pano_y, label_path, original_line, row, col, cx_norm, cy_norm)
    frame_detections: dict[tuple[str, int], list] = defaultdict(list)

    for label_path in sorted(input_dir.glob("*.txt")):
        parsed = parse_tile_filename(label_path.stem)
        if parsed is None:
            continue

        segment, frame_idx, row, col = parsed
        for cx_norm, cy_norm, line in _parse_detection(label_path):
            pano_x, pano_y = _tile_to_pano(cx_norm, cy_norm, row, col)
            frame_detections[(segment, frame_idx)].append(
                (pano_x, pano_y, label_path, line, row, col, cx_norm, cy_norm)
            )
            stats["total"] += 1

    if not frame_detections:
        return stats

    # Phase 2: Build trajectories (same as trajectory_validator)
    segment_frames: dict[str, list[int]] = defaultdict(list)
    for segment, frame_idx in frame_detections:
        segment_frames[segment].append(frame_idx)
    for seg in segment_frames:
        segment_frames[seg] = sorted(set(segment_frames[seg]))

    # Auto-detect frame interval
    all_fi = set()
    for _seg, fi in frame_detections:
        all_fi.add(fi)
    sorted_fi = sorted(all_fi)
    if len(sorted_fi) >= 2:
        gaps = [
            sorted_fi[i + 1] - sorted_fi[i] for i in range(min(100, len(sorted_fi) - 1))
        ]
        gaps = [g for g in gaps if g > 0]
        frame_interval = min(gaps) if gaps else FRAME_INTERVAL
    else:
        frame_interval = FRAME_INTERVAL
    max_frame_gap = frame_interval * 2 + frame_interval // 2

    # detection_id → class assignment
    # detection_id = (label_path_str, line_str)
    detection_class: dict[tuple[str, str], int] = {}

    for segment, frame_indices in segment_frames.items():
        active_trajectories: list[list] = []
        finished_trajectories: list[list] = []

        for fi in frame_indices:
            dets = frame_detections[(segment, fi)]
            used = [False] * len(dets)

            new_active = []
            for traj in active_trajectories:
                last_fi = traj[-1][0]
                last_x, last_y = traj[-1][1], traj[-1][2]

                frame_gap = fi - last_fi
                if frame_gap > max_frame_gap or frame_gap <= 0:
                    finished_trajectories.append(traj)
                    continue

                n_intervals = max(frame_gap / frame_interval, 1)
                best_idx = -1
                best_dist = max_distance * n_intervals
                for i, (px, py, *_rest) in enumerate(dets):
                    if used[i]:
                        continue
                    dist = ((px - last_x) ** 2 + (py - last_y) ** 2) ** 0.5
                    if dist < best_dist:
                        best_dist = dist
                        best_idx = i

                if best_idx >= 0:
                    traj.append((fi, *dets[best_idx]))
                    used[best_idx] = True
                    new_active.append(traj)
                else:
                    finished_trajectories.append(traj)

            for i, det in enumerate(dets):
                if not used[i]:
                    new_active.append([(fi, *det)])

            active_trajectories = new_active

        finished_trajectories.extend(active_trajectories)

        # Phase 3: Classify each trajectory by analyzing frame-to-frame motion
        for traj in finished_trajectories:
            if len(traj) >= min_trajectory:
                # Compute per-frame velocities (displacement between consecutive points)
                velocities = []
                for i in range(1, len(traj)):
                    dx = traj[i][1] - traj[i - 1][1]
                    dy = traj[i][2] - traj[i - 1][2]
                    dt = traj[i][0] - traj[i - 1][0]  # frame gap
                    if dt > 0:
                        speed = ((dx**2 + dy**2) ** 0.5) / dt * frame_interval
                        velocities.append(speed)

                # Total path length (sum of all moves, not just start→end)
                path_length = sum(
                    (
                        (traj[i][1] - traj[i - 1][1]) ** 2
                        + (traj[i][2] - traj[i - 1][2]) ** 2
                    )
                    ** 0.5
                    for i in range(1, len(traj))
                )

                # Max speed in any frame
                max_speed = max(velocities) if velocities else 0

                # Average speed
                avg_speed = sum(velocities) / len(velocities) if velocities else 0

                # Classify based on motion profile:
                # - Game ball: moves significantly at some point
                # - Static ball: barely moves across entire trajectory
                if path_length > STATIC_DISPLACEMENT_THRESHOLD or max_speed > 20:
                    cls = GAME_BALL
                else:
                    cls = STATIC_BALL
            else:
                # Short trajectory (1-2 frames) — likely noise
                # But could be a ball entering/leaving frame
                cls = NOT_BALL

            for entry in traj:
                # entry = (fi, pano_x, pano_y, label_path, line, row, col, cx, cy)
                label_path = entry[3]
                line = entry[4]
                det_id = (str(label_path), line)
                detection_class[det_id] = cls

    # Phase 4: Apply QA verdict overrides
    for det_id, cls in list(detection_class.items()):
        label_path_str, line = det_id
        fname = Path(label_path_str).name
        qa_cls = qa_verdicts.get((game_id, fname))
        if qa_cls is not None:
            detection_class[det_id] = qa_cls
            stats["qa_override"] += 1

    # Phase 5: Write classified labels
    # Group by label file
    file_lines: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for (label_path_str, line), cls in detection_class.items():
        # Rewrite the class ID in the YOLO line
        parts = line.split()
        parts[0] = str(cls)
        new_line = " ".join(parts)
        file_lines[label_path_str].append((cls, new_line))

        if cls == GAME_BALL:
            stats["game_ball"] += 1
        elif cls == STATIC_BALL:
            stats["static_ball"] += 1
        else:
            stats["not_ball"] += 1

    for label_path_str, entries in file_lines.items():
        label_path = Path(label_path_str)
        out_path = game_out / label_path.name
        with open(out_path, "w") as f:
            for _cls, line in sorted(set(entries)):
                f.write(line + "\n")

    return stats


def main():
    parser = argparse.ArgumentParser(description="Classify ball detections")
    parser.add_argument("--labels", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--games", nargs="+", help="Only process specific games")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    qa_verdicts = load_qa_verdicts(args.db)

    game_dirs = sorted([d for d in args.labels.iterdir() if d.is_dir()])
    if args.games:
        game_dirs = [d for d in game_dirs if d.name in args.games]

    totals = {
        "game_ball": 0,
        "static_ball": 0,
        "not_ball": 0,
        "qa_override": 0,
        "total": 0,
    }
    start = time.time()

    for game_dir in game_dirs:
        game_id = game_dir.name
        game_stats = classify_game(
            game_dir,
            args.output,
            game_id,
            qa_verdicts,
        )
        for k, v in game_stats.items():
            totals[k] += v
        logger.info(
            "%s: %d game_ball, %d static, %d not_ball (%d QA overrides) / %d total",
            game_id,
            game_stats["game_ball"],
            game_stats["static_ball"],
            game_stats["not_ball"],
            game_stats["qa_override"],
            game_stats["total"],
        )

    elapsed = time.time() - start
    logger.info(
        "Done in %.0fs: %d game_ball, %d static_ball, %d not_ball (%d QA overrides) / %d total",
        elapsed,
        totals["game_ball"],
        totals["static_ball"],
        totals["not_ball"],
        totals["qa_override"],
        totals["total"],
    )


if __name__ == "__main__":
    main()
