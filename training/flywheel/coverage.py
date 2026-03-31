"""Measure per-game ball tracking coverage and find gaps.

Coverage = fraction of active-play frames with a tracked ball detection.
A gap is a stretch of N+ consecutive frames with no detection in a trajectory.

Reuses trajectory linking from exp_allrow_gaps.py but generalized for
any label directory and game list.
"""

import json
import logging
from collections import defaultdict
from pathlib import Path

from training.data_prep.organize_dataset import parse_tile_filename
from training.data_prep.trajectory_validator import (
    MAX_LINK_DISTANCE,
    _parse_detection,
    _tile_to_pano,
)

logger = logging.getLogger(__name__)

# Frames between label extractions (every 4th frame from 25fps video)
DEFAULT_FRAME_INTERVAL = 4
MIN_TRAJECTORY_FRAMES = 5
# Gap threshold: gaps longer than this (in frames) are worth investigating
SHORT_GAP_THRESHOLD = 50   # ~2 seconds at 25fps
LONG_GAP_THRESHOLD = 150   # ~6 seconds


def measure_game_coverage(
    game_id: str,
    labels_dir: Path,
    frame_interval: int | None = None,
) -> dict:
    """Measure tracking coverage for one game.

    Returns dict with coverage stats and gap list.
    """
    label_dir = labels_dir / game_id
    if not label_dir.exists():
        return {"game_id": game_id, "error": "no labels", "coverage": 0.0, "gaps": []}

    # Parse all detections into panoramic coordinates
    frame_dets: dict[tuple[str, int], list[tuple[float, float]]] = defaultdict(list)

    for label_path in label_dir.glob("*.txt"):
        parsed = parse_tile_filename(label_path.stem)
        if parsed is None:
            continue
        segment, frame_idx, row, col = parsed
        for cx_norm, cy_norm, _line in _parse_detection(label_path):
            pano_x, pano_y = _tile_to_pano(cx_norm, cy_norm, row, col)
            frame_dets[(segment, frame_idx)].append((pano_x, pano_y))

    if not frame_dets:
        return {"game_id": game_id, "error": "no detections", "coverage": 0.0, "gaps": []}

    # Auto-detect frame interval
    if frame_interval is None:
        all_fi = sorted(set(fi for _, fi in frame_dets))
        if len(all_fi) >= 2:
            deltas = [all_fi[i + 1] - all_fi[i] for i in range(min(100, len(all_fi) - 1))]
            deltas = [d for d in deltas if d > 0]
            frame_interval = min(deltas) if deltas else DEFAULT_FRAME_INTERVAL
        else:
            frame_interval = DEFAULT_FRAME_INTERVAL

    max_frame_gap = frame_interval * 3

    # Group by segment
    seg_frames: dict[str, list[int]] = defaultdict(list)
    for seg, fi in frame_dets:
        seg_frames[seg].append(fi)
    for seg in seg_frames:
        seg_frames[seg] = sorted(set(seg_frames[seg]))

    # Build trajectories and find gaps per segment
    all_trajectories = []
    all_gaps = []

    for segment, frame_indices in seg_frames.items():
        trajectories, gaps = _link_and_find_gaps(
            segment, frame_indices, frame_dets,
            game_id, frame_interval, max_frame_gap,
        )
        all_trajectories.extend(trajectories)
        all_gaps.extend(gaps)

    # Compute coverage: frames with at least one trajectory detection / total frames
    total_frames = sum(len(fis) for fis in seg_frames.values())
    frames_with_detection = len(frame_dets)

    # Count frames that are part of a trajectory (not isolated noise)
    trajectory_frames = set()
    for traj in all_trajectories:
        if len(traj) >= MIN_TRAJECTORY_FRAMES:
            for fi, _, _ in traj:
                trajectory_frames.add(fi)

    coverage = len(trajectory_frames) / max(total_frames, 1)

    # Categorize gaps
    short_gaps = [g for g in all_gaps if g["gap_frames"] <= SHORT_GAP_THRESHOLD]
    long_gaps = [g for g in all_gaps if g["gap_frames"] > SHORT_GAP_THRESHOLD]
    very_long_gaps = [g for g in all_gaps if g["gap_frames"] > LONG_GAP_THRESHOLD]

    return {
        "game_id": game_id,
        "coverage": round(coverage, 4),
        "total_frames": total_frames,
        "frames_with_detection": frames_with_detection,
        "frames_in_trajectories": len(trajectory_frames),
        "trajectory_count": len([t for t in all_trajectories if len(t) >= MIN_TRAJECTORY_FRAMES]),
        "frame_interval": frame_interval,
        "gap_count": len(all_gaps),
        "short_gaps": len(short_gaps),
        "long_gaps": len(long_gaps),
        "very_long_gaps": len(very_long_gaps),
        "gaps": all_gaps,
    }


def _link_and_find_gaps(
    segment: str,
    frame_indices: list[int],
    frame_dets: dict,
    game_id: str,
    frame_interval: int,
    max_frame_gap: int,
) -> tuple[list, list]:
    """Link detections into trajectories and find gaps within them."""
    active: list[list[tuple[int, float, float]]] = []
    finished: list[list[tuple[int, float, float]]] = []

    for fi in frame_indices:
        dets = frame_dets[(segment, fi)]
        used = [False] * len(dets)
        new_active = []

        for traj in active:
            last_fi, last_x, last_y = traj[-1]
            gap = fi - last_fi
            if gap > max_frame_gap or gap <= 0:
                finished.append(traj)
                continue

            n_intervals = max(gap / frame_interval, 1)
            best_idx = -1
            best_dist = MAX_LINK_DISTANCE * n_intervals
            for j, (px, py) in enumerate(dets):
                if used[j]:
                    continue
                dist = ((px - last_x) ** 2 + (py - last_y) ** 2) ** 0.5
                if dist < best_dist:
                    best_dist = dist
                    best_idx = j

            if best_idx >= 0:
                px, py = dets[best_idx]
                traj.append((fi, px, py))
                used[best_idx] = True
                new_active.append(traj)
            else:
                finished.append(traj)

        for j, (px, py) in enumerate(dets):
            if not used[j]:
                new_active.append([(fi, px, py)])
        active = new_active

    finished.extend(active)

    # Find gaps within trajectories
    gaps = []
    for traj in finished:
        if len(traj) < MIN_TRAJECTORY_FRAMES:
            continue

        for i in range(1, len(traj)):
            fi_prev, x_prev, y_prev = traj[i - 1]
            fi_curr, x_curr, y_curr = traj[i]
            gap_frames = fi_curr - fi_prev

            if gap_frames <= frame_interval:
                continue

            displacement = ((x_curr - x_prev) ** 2 + (y_curr - y_prev) ** 2) ** 0.5

            gaps.append({
                "game_id": game_id,
                "segment": segment,
                "frame_start": fi_prev,
                "frame_end": fi_curr,
                "gap_frames": gap_frames,
                "gap_seconds": round(gap_frames / 25.0, 1),
                "x_start": round(x_prev, 1),
                "y_start": round(y_prev, 1),
                "x_end": round(x_curr, 1),
                "y_end": round(y_curr, 1),
                "displacement": round(displacement, 1),
                "trajectory_length": len(traj),
                "priority": _compute_priority(gap_frames, len(traj), displacement),
            })

    return finished, gaps


def _compute_priority(gap_frames: int, traj_length: int, displacement: float) -> float:
    """Compute priority score for a gap. Higher = more valuable to fill.

    Factors:
    - Gap length (longer = more lost time)
    - Trajectory quality before gap (longer trajectory = more value)
    - Displacement (large = fast kick, harder to fill automatically)
    """
    gap_score = min(gap_frames / 50.0, 5.0)  # 0-5 based on gap length
    traj_score = min(traj_length / 20.0, 3.0)  # 0-3 based on trajectory quality
    speed_score = min(displacement / 500.0, 2.0)  # 0-2 based on ball speed
    return round(gap_score + traj_score + speed_score, 2)


def measure_all_games(
    labels_dir: Path,
    games: list[str] | None = None,
) -> list[dict]:
    """Measure coverage for all games. If games is None, scan labels_dir."""
    if games is None:
        if not labels_dir.exists():
            logger.error("Labels directory not found: %s", labels_dir)
            return []
        games = sorted(d.name for d in labels_dir.iterdir() if d.is_dir())

    results = []
    for game_id in games:
        result = measure_game_coverage(game_id, labels_dir)
        results.append(result)
        if result.get("error"):
            logger.warning("%s: %s", game_id, result["error"])
        else:
            logger.info(
                "%s: %.1f%% coverage, %d gaps (%d long, %d very long)",
                game_id,
                result["coverage"] * 100,
                result.get("gap_count", 0),
                result.get("long_gaps", 0),
                result.get("very_long_gaps", 0),
            )

    # Summary
    avg_coverage = sum(r["coverage"] for r in results) / max(len(results), 1)
    total_gaps = sum(r["gap_count"] for r in results)
    total_long = sum(r["long_gaps"] for r in results)
    logger.info(
        "Overall: %.1f%% avg coverage, %d total gaps (%d long)",
        avg_coverage * 100, total_gaps, total_long,
    )

    return results
