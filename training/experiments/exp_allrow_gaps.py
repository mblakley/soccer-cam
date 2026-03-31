"""Find trajectory gaps across ALL rows (not just r0).

Same approach as exp1_onnx_gaps but for r1 and r2 as well.
This reveals where the ONNX model drops the ball across the entire field.
"""
import glob as glob_mod
import json
import logging
import time
from collections import defaultdict
from pathlib import Path

from training.data_prep.organize_dataset import parse_tile_filename
from training.data_prep.trajectory_validator import (
    MAX_LINK_DISTANCE,
    _parse_detection,
    _tile_to_pano,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

LABELS_DIR = Path("F:/training_data/labels_640_ext")
OUTPUT = Path("D:/training_data/experiments/exp_allrow_gaps.json")

GAMES = [
    "flash__06.01.2024_vs_IYSA_home",
    "flash__09.27.2024_vs_RNYFC_Black_home",
    "flash__09.30.2024_vs_Chili_home",
    "flash__2025.06.02",
    "heat__05.31.2024_vs_Fairport_home",
    "heat__06.20.2024_vs_Chili_away",
    "heat__07.17.2024_vs_Fairport_away",
    "heat__Clarence_Tournament",
    "heat__Heat_Tournament",
]


def find_gaps_in_game(game_id: str) -> list[dict]:
    """Find trajectory gaps for ALL detections in one game."""
    label_dir = LABELS_DIR / game_id
    if not label_dir.exists():
        return []

    # Parse ALL detections into panoramic coordinates
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
        return []

    seg_frames: dict[str, list[int]] = defaultdict(list)
    for seg, fi in frame_dets:
        seg_frames[seg].append(fi)
    for seg in seg_frames:
        seg_frames[seg] = sorted(set(seg_frames[seg]))

    # Auto-detect frame interval
    all_fi = sorted(set(fi for _, fi in frame_dets))
    if len(all_fi) >= 2:
        gaps_list = [all_fi[i + 1] - all_fi[i] for i in range(min(100, len(all_fi) - 1))]
        gaps_list = [g for g in gaps_list if g > 0]
        frame_interval = min(gaps_list) if gaps_list else 4
    else:
        frame_interval = 4
    max_frame_gap = frame_interval * 3

    all_gaps = []

    for segment, frame_indices in seg_frames.items():
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

        # Find gaps in trajectories >= 5 frames
        for traj in finished:
            if len(traj) < 5:
                continue

            for i in range(1, len(traj)):
                fi_prev, x_prev, y_prev = traj[i - 1]
                fi_curr, x_curr, y_curr = traj[i]
                gap_frames = fi_curr - fi_prev

                if gap_frames <= frame_interval:
                    continue

                n_missing = (gap_frames // frame_interval) - 1
                for k in range(1, n_missing + 1):
                    frac = k / (n_missing + 1)
                    interp_fi = fi_prev + k * frame_interval
                    interp_x = x_prev + frac * (x_curr - x_prev)
                    interp_y = y_prev + frac * (y_curr - y_prev)

                    # Determine which row this falls in
                    if interp_y < 640:
                        row_name = "r0_far"
                    elif interp_y < 1220:
                        row_name = "r1_mid"
                    else:
                        row_name = "r2_near"

                    all_gaps.append({
                        "game_id": game_id,
                        "segment": segment,
                        "frame_idx": interp_fi,
                        "pano_x": round(interp_x, 1),
                        "pano_y": round(interp_y, 1),
                        "row": row_name,
                        "trajectory_length": len(traj),
                        "gap_size": gap_frames,
                    })

    return all_gaps


def main():
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    start = time.time()
    all_gaps = []

    for game_id in GAMES:
        gaps = find_gaps_in_game(game_id)
        all_gaps.extend(gaps)
        # Count by row
        by_row = defaultdict(int)
        for g in gaps:
            by_row[g["row"]] += 1
        logger.info(
            "%s: %d gaps (r0=%d, r1=%d, r2=%d)",
            game_id, len(gaps),
            by_row.get("r0_far", 0),
            by_row.get("r1_mid", 0),
            by_row.get("r2_near", 0),
        )

    with open(OUTPUT, "w") as f:
        json.dump(all_gaps, f, indent=2)

    # Summary
    total_by_row = defaultdict(int)
    for g in all_gaps:
        total_by_row[g["row"]] += 1

    elapsed = time.time() - start
    logger.info("Done in %.0fs: %d total gaps", elapsed, len(all_gaps))
    logger.info("  r0 (far): %d", total_by_row.get("r0_far", 0))
    logger.info("  r1 (mid): %d", total_by_row.get("r1_mid", 0))
    logger.info("  r2 (near): %d", total_by_row.get("r2_near", 0))


if __name__ == "__main__":
    main()
