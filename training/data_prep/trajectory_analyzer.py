"""Analyze ball trajectories and test filtering strategies.

Builds trajectories from label data (reusing trajectory_validator logic),
computes per-trajectory metrics, and tests multiple filtering strategies
side-by-side. Run on individual games to find the best approach before
applying it to the full pipeline.

Usage:
    python -m training.data_prep.trajectory_analyzer \
        --labels F:/training_data/labels_640_filtered/flash__2025.06.02 \
        --game flash__2025.06.02

    # With person data (after running bootstrap_persons):
    python -m training.data_prep.trajectory_analyzer \
        --labels F:/training_data/labels_640_filtered/flash__2025.06.02 \
        --persons F:/training_data/labels_640_persons/flash__2025.06.02 \
        --game flash__2025.06.02
"""

import argparse
import logging
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from training.data_prep.organize_dataset import parse_tile_filename
from training.data_prep.trajectory_validator import (
    MAX_LINK_DISTANCE,
    MIN_TRAJECTORY_LENGTH,
    _parse_detection,
    _tile_to_pano,
)

logger = logging.getLogger(__name__)

FRAME_INTERVAL = 8  # frames between consecutive extracted frames
FPS_ESTIMATE = 25.0  # approximate source video FPS


@dataclass
class TrajectoryInfo:
    """Metrics for a single trajectory."""

    frames: list[tuple[int, float, float, Path, str]]  # (fi, px, py, path, line)
    segment: str = ""

    # Computed metrics
    length: int = 0
    duration_secs: float = 0.0
    max_displacement: float = 0.0
    total_path_length: float = 0.0
    bbox_area: float = 0.0
    avg_velocity: float = 0.0
    player_correlation: float = 0.0
    label_count: int = 0

    # Derived
    score_movement: float = 0.0
    score_length_movement: float = 0.0
    score_dominant: float = 0.0
    score_player: float = 0.0
    score_combined: float = 0.0


def _build_trajectories(
    labels_dir: Path,
    max_distance: float = MAX_LINK_DISTANCE,
    min_length: int = MIN_TRAJECTORY_LENGTH,
) -> list[TrajectoryInfo]:
    """Build trajectories from labels, reusing trajectory_validator logic.

    Returns list of TrajectoryInfo with basic metrics computed.
    """
    # Phase 1: Parse all labels into panoramic detections
    frame_detections: dict[tuple[str, int], list[tuple[float, float, Path, str]]] = (
        defaultdict(list)
    )

    for label_path in sorted(labels_dir.glob("*.txt")):
        parsed = parse_tile_filename(label_path.stem)
        if parsed is None:
            continue
        segment, frame_idx, row, col = parsed
        for cx_norm, cy_norm, line in _parse_detection(label_path):
            pano_x, pano_y = _tile_to_pano(cx_norm, cy_norm, row, col)
            frame_detections[(segment, frame_idx)].append(
                (pano_x, pano_y, label_path, line)
            )

    if not frame_detections:
        return []

    # Group by segment
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

    # Phase 2: Build trajectories (same greedy linking as trajectory_validator)
    all_trajectories: list[TrajectoryInfo] = []

    for segment, frame_indices in segment_frames.items():
        active: list[list[tuple[int, float, float, Path, str]]] = []
        finished: list[list[tuple[int, float, float, Path, str]]] = []

        for fi in frame_indices:
            dets = frame_detections[(segment, fi)]
            used = [False] * len(dets)

            new_active = []
            for traj in active:
                last_fi, last_x, last_y, _, _ = traj[-1]
                frame_gap = fi - last_fi
                if frame_gap > max_frame_gap or frame_gap <= 0:
                    finished.append(traj)
                    continue

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
                    finished.append(traj)

            for i, (px, py, lp, ln) in enumerate(dets):
                if not used[i]:
                    new_active.append([(fi, px, py, lp, ln)])

            active = new_active

        finished.extend(active)

        # Compute metrics for trajectories meeting minimum length
        for traj in finished:
            if len(traj) < min_length:
                continue

            info = TrajectoryInfo(frames=traj, segment=segment)
            info.length = len(traj)
            info.label_count = len(traj)

            # Duration
            fi_first = traj[0][0]
            fi_last = traj[-1][0]
            info.duration_secs = (fi_last - fi_first) / FPS_ESTIMATE

            # Max displacement (first point vs all others)
            x0, y0 = traj[0][1], traj[0][2]
            info.max_displacement = max(
                ((t[1] - x0) ** 2 + (t[2] - y0) ** 2) ** 0.5 for t in traj[1:]
            )

            # Total path length
            total = 0.0
            for i in range(1, len(traj)):
                dx = traj[i][1] - traj[i - 1][1]
                dy = traj[i][2] - traj[i - 1][2]
                total += (dx**2 + dy**2) ** 0.5
            info.total_path_length = total

            # Bounding box area (panoramic pixels)
            xs = [t[1] for t in traj]
            ys = [t[2] for t in traj]
            info.bbox_area = (max(xs) - min(xs)) * (max(ys) - min(ys))

            # Average velocity
            if info.duration_secs > 0:
                info.avg_velocity = info.total_path_length / info.duration_secs

            all_trajectories.append(info)

    return all_trajectories


def _stitch_trajectories(
    trajectories: list[TrajectoryInfo],
    max_time_gap_secs: float = 5.0,
    max_spatial_gap_px: float = 800.0,
) -> list[TrajectoryInfo]:
    """Stitch trajectory fragments that are likely the same ball.

    If trajectory A ends at time T and trajectory B starts at time T+gap,
    and the spatial distance is reasonable for the time gap, merge them.
    This handles brief occlusions, tile boundary crossings, and out-of-bounds.

    Args:
        trajectories: List of trajectory infos (must all be from same segment)
        max_time_gap_secs: Maximum time gap to stitch across
        max_spatial_gap_px: Maximum spatial distance at the junction point
    """
    if len(trajectories) < 2:
        return trajectories

    # Group by segment
    by_segment: dict[str, list[TrajectoryInfo]] = defaultdict(list)
    for t in trajectories:
        by_segment[t.segment].append(t)

    stitched_all: list[TrajectoryInfo] = []

    for segment, seg_trajs in by_segment.items():
        # Sort by start frame
        seg_trajs.sort(key=lambda t: t.frames[0][0])

        # Greedy forward stitching
        merged: list[TrajectoryInfo] = []
        used = [False] * len(seg_trajs)

        for i in range(len(seg_trajs)):
            if used[i]:
                continue

            # Start a chain from trajectory i
            chain = seg_trajs[i]
            used[i] = True

            # Try to extend the chain
            changed = True
            while changed:
                changed = False
                chain_end_fi = chain.frames[-1][0]
                chain_end_x = chain.frames[-1][1]
                chain_end_y = chain.frames[-1][2]
                chain_end_time = chain_end_fi / FPS_ESTIMATE

                best_j = -1
                best_gap = float("inf")

                for j in range(len(seg_trajs)):
                    if used[j]:
                        continue
                    cand = seg_trajs[j]
                    cand_start_fi = cand.frames[0][0]
                    cand_start_x = cand.frames[0][1]
                    cand_start_y = cand.frames[0][2]
                    cand_start_time = cand_start_fi / FPS_ESTIMATE

                    time_gap = cand_start_time - chain_end_time
                    if time_gap < 0 or time_gap > max_time_gap_secs:
                        continue

                    spatial_gap = (
                        (cand_start_x - chain_end_x) ** 2
                        + (cand_start_y - chain_end_y) ** 2
                    ) ** 0.5

                    # Scale spatial threshold by time gap (ball moves further in more time)
                    # At 3fps extraction, max ~400px/s ball velocity -> 5s * 400 = 2000px max
                    max_gap = min(max_spatial_gap_px, 400.0 * max(time_gap, 0.3))
                    if spatial_gap > max_gap:
                        continue

                    if time_gap < best_gap:
                        best_gap = time_gap
                        best_j = j

                if best_j >= 0:
                    # Merge: append candidate's frames to chain
                    new_frames = chain.frames + seg_trajs[best_j].frames
                    chain = TrajectoryInfo(frames=new_frames, segment=segment)
                    used[best_j] = True
                    changed = True

            # Recompute metrics for the stitched chain
            chain.length = len(chain.frames)
            chain.label_count = len(chain.frames)
            fi_first = chain.frames[0][0]
            fi_last = chain.frames[-1][0]
            chain.duration_secs = (fi_last - fi_first) / FPS_ESTIMATE

            x0, y0 = chain.frames[0][1], chain.frames[0][2]
            chain.max_displacement = (
                max(
                    ((t[1] - x0) ** 2 + (t[2] - y0) ** 2) ** 0.5
                    for t in chain.frames[1:]
                )
                if len(chain.frames) > 1
                else 0.0
            )

            total = 0.0
            for k in range(1, len(chain.frames)):
                dx = chain.frames[k][1] - chain.frames[k - 1][1]
                dy = chain.frames[k][2] - chain.frames[k - 1][2]
                total += (dx**2 + dy**2) ** 0.5
            chain.total_path_length = total

            xs = [t[1] for t in chain.frames]
            ys = [t[2] for t in chain.frames]
            chain.bbox_area = (max(xs) - min(xs)) * (max(ys) - min(ys))

            if chain.duration_secs > 0:
                chain.avg_velocity = chain.total_path_length / chain.duration_secs

            merged.append(chain)

        stitched_all.extend(merged)

    return stitched_all


def _load_person_detections(
    persons_dir: Path,
) -> dict[tuple[str, int], list[tuple[float, float]]]:
    """Load person detection positions indexed by (segment, frame_idx).

    Returns dict mapping (segment, frame_idx) -> list of (pano_x, pano_y).
    """
    person_positions: dict[tuple[str, int], list[tuple[float, float]]] = defaultdict(
        list
    )

    for label_path in sorted(persons_dir.glob("*.txt")):
        parsed = parse_tile_filename(label_path.stem)
        if parsed is None:
            continue
        segment, frame_idx, row, col = parsed
        for cx_norm, cy_norm, _line in _parse_detection(label_path):
            pano_x, pano_y = _tile_to_pano(cx_norm, cy_norm, row, col)
            person_positions[(segment, frame_idx)].append((pano_x, pano_y))

    return person_positions


def _compute_player_correlation(
    trajectories: list[TrajectoryInfo],
    person_positions: dict[tuple[str, int], list[tuple[float, float]]],
    radius: float = 200.0,
):
    """Compute average nearby player count for each trajectory."""
    for traj_info in trajectories:
        total_nearby = 0
        frames_checked = 0

        for fi, px, py, _, _ in traj_info.frames:
            persons = person_positions.get((traj_info.segment, fi), [])
            nearby = sum(
                1
                for ppx, ppy in persons
                if ((ppx - px) ** 2 + (ppy - py) ** 2) ** 0.5 < radius
            )
            total_nearby += nearby
            frames_checked += 1

        if frames_checked > 0:
            traj_info.player_correlation = total_nearby / frames_checked


def _run_experiment(
    name: str,
    trajectories: list[TrajectoryInfo],
    keep_fn,
    segment_durations: dict[str, float],
):
    """Run a filtering experiment and print results."""
    kept = [t for t in trajectories if keep_fn(t)]
    discarded = [t for t in trajectories if not keep_fn(t)]

    total_labels = sum(t.label_count for t in trajectories)
    kept_labels = sum(t.label_count for t in kept)

    # Time coverage: what fraction of total segment time is covered by kept trajectories
    total_time = sum(segment_durations.values())
    covered_frames: set[tuple[str, int]] = set()
    for t in kept:
        for fi, _, _, _, _ in t.frames:
            covered_frames.add((t.segment, fi))
    # Approximate coverage: unique frames * interval / fps
    covered_time = len(covered_frames) * FRAME_INTERVAL / FPS_ESTIMATE

    print(f"\n{'=' * 60}")
    print(f"  {name}")
    print(f"{'=' * 60}")
    print(
        f"  Trajectories: {len(trajectories)} -> {len(kept)} ({len(kept) / max(len(trajectories), 1) * 100:.1f}% kept)"
    )
    print(
        f"  Labels:       {total_labels} -> {kept_labels} ({kept_labels / max(total_labels, 1) * 100:.1f}% kept)"
    )
    print(
        f"  Time coverage: {covered_time:.0f}s / {total_time:.0f}s ({covered_time / max(total_time, 1) * 100:.1f}%)"
    )

    if kept:
        print("\n  Top 5 KEPT trajectories:")
        for t in sorted(
            kept, key=lambda x: x.length * x.max_displacement, reverse=True
        )[:5]:
            print(
                f"    len={t.length:4d}  disp={t.max_displacement:6.0f}px  "
                f"path={t.total_path_length:7.0f}px  dur={t.duration_secs:5.1f}s  "
                f"vel={t.avg_velocity:5.0f}px/s  players={t.player_correlation:.1f}  "
                f"seg={t.segment[:30]}"
            )

    if discarded:
        # Show top discarded to check we're not losing real balls
        print("\n  Top 5 DISCARDED (check for false negatives):")
        for t in sorted(
            discarded, key=lambda x: x.length * x.max_displacement, reverse=True
        )[:5]:
            print(
                f"    len={t.length:4d}  disp={t.max_displacement:6.0f}px  "
                f"path={t.total_path_length:7.0f}px  dur={t.duration_secs:5.1f}s  "
                f"vel={t.avg_velocity:5.0f}px/s  players={t.player_correlation:.1f}  "
                f"seg={t.segment[:30]}"
            )

    return kept


def analyze_game(
    labels_dir: Path,
    game_id: str,
    persons_dir: Path | None = None,
):
    """Run all experiments on a single game."""
    print(f"\n{'#' * 60}")
    print(f"  TRAJECTORY ANALYSIS: {game_id}")
    print(f"{'#' * 60}")

    # Build trajectories
    print("\nBuilding trajectories...")
    trajectories = _build_trajectories(labels_dir)
    print(f"  Found {len(trajectories)} trajectories (min length=3)")

    if not trajectories:
        print("  No trajectories found!")
        return

    # Compute segment durations for time coverage
    segment_durations: dict[str, float] = {}
    for t in trajectories:
        fi_max = max(f[0] for f in t.frames)
        fi_min = min(f[0] for f in t.frames)
        dur = fi_max / FPS_ESTIMATE  # approximate segment duration from max frame
        if t.segment not in segment_durations or dur > segment_durations[t.segment]:
            segment_durations[t.segment] = dur

    # Load person data if available
    has_persons = False
    if persons_dir and persons_dir.exists():
        print("Loading person detections...")
        person_positions = _load_person_detections(persons_dir)
        if person_positions:
            has_persons = True
            _compute_player_correlation(trajectories, person_positions)
            print(f"  Loaded {len(person_positions)} frame-person entries")
        else:
            print("  No person detections found")
    else:
        print("No person data (skipping player correlation experiments)")

    # Summary statistics
    print("\n--- Trajectory Summary ---")
    lengths = [t.length for t in trajectories]
    disps = [t.max_displacement for t in trajectories]
    print(
        f"  Length:       min={min(lengths)}, median={sorted(lengths)[len(lengths) // 2]}, max={max(lengths)}, mean={sum(lengths) / len(lengths):.1f}"
    )
    print(
        f"  Displacement: min={min(disps):.0f}, median={sorted(disps)[len(disps) // 2]:.0f}, max={max(disps):.0f}, mean={sum(disps) / len(disps):.1f}"
    )

    static = sum(1 for d in disps if d < 30)
    print(f"  Static (<30px): {static} ({static / len(disps) * 100:.1f}%)")
    moving = sum(1 for d in disps if d >= 30)
    print(f"  Moving (>=30px): {moving} ({moving / len(disps) * 100:.1f}%)")

    if has_persons:
        players = [t.player_correlation for t in trajectories]
        print(
            f"  Player corr:  min={min(players):.1f}, median={sorted(players)[len(players) // 2]:.1f}, max={max(players):.1f}, mean={sum(players) / len(players):.1f}"
        )

    # Length distribution
    print("\n--- Length Distribution ---")
    for lo, hi in [
        (3, 5),
        (6, 10),
        (11, 20),
        (21, 50),
        (51, 100),
        (101, 500),
        (501, 99999),
    ]:
        n = sum(1 for t in trajectories if lo <= t.length <= hi)
        n_static = sum(
            1 for t in trajectories if lo <= t.length <= hi and t.max_displacement < 30
        )
        label = f"{lo}-{hi}" if hi < 99999 else f"{lo}+"
        if n > 0:
            print(
                f"  {label:>7s}: {n:5d} ({n_static:4d} static, {n - n_static:4d} moving)"
            )

    # ===== EXPERIMENT A: Movement threshold only =====
    for thresh in [15, 30, 50]:
        _run_experiment(
            f"Exp A: Movement > {thresh}px",
            trajectories,
            lambda t, th=thresh: t.max_displacement >= th,
            segment_durations,
        )

    # ===== EXPERIMENT B: Length + movement =====
    for min_len, min_disp in [(10, 30), (20, 30), (10, 50)]:
        _run_experiment(
            f"Exp B: Length >= {min_len} AND displacement > {min_disp}px",
            trajectories,
            lambda t, ml=min_len, md=min_disp: (
                t.length >= ml and t.max_displacement >= md
            ),
            segment_durations,
        )

    # ===== EXPERIMENT C: Dominant per segment =====
    for top_k in [1, 3, 5]:
        # Build per-segment ranking
        seg_trajs: dict[str, list[TrajectoryInfo]] = defaultdict(list)
        for t in trajectories:
            seg_trajs[t.segment].append(t)

        dominant_set: set[int] = set()
        for seg, seg_ts in seg_trajs.items():
            ranked = sorted(
                seg_ts, key=lambda x: x.length * x.max_displacement, reverse=True
            )
            for t in ranked[:top_k]:
                dominant_set.add(id(t))

        _run_experiment(
            f"Exp C: Top {top_k} per segment (by length*displacement)",
            trajectories,
            lambda t: id(t) in dominant_set,
            segment_durations,
        )

    if has_persons:
        # ===== EXPERIMENT D: Player correlation =====
        for min_players in [0.5, 1.0, 2.0]:
            _run_experiment(
                f"Exp D: Player correlation >= {min_players}",
                trajectories,
                lambda t, mp=min_players: t.player_correlation >= mp,
                segment_durations,
            )

        # ===== EXPERIMENT E: Combined score =====
        # Score = movement * log(length) * (1 + player_density)
        import math

        for t in trajectories:
            t.score_combined = (
                t.max_displacement
                * math.log(max(t.length, 1))
                * (1 + t.player_correlation)
            )

        # Top-K by combined score per segment
        for top_k in [1, 3, 5]:
            seg_trajs_e: dict[str, list[TrajectoryInfo]] = defaultdict(list)
            for t in trajectories:
                seg_trajs_e[t.segment].append(t)

            combined_set: set[int] = set()
            for seg, seg_ts in seg_trajs_e.items():
                ranked = sorted(seg_ts, key=lambda x: x.score_combined, reverse=True)
                for t in ranked[:top_k]:
                    combined_set.add(id(t))

            _run_experiment(
                f"Exp E: Top {top_k} per segment (combined: disp*log(len)*(1+players))",
                trajectories,
                lambda t: id(t) in combined_set,
                segment_durations,
            )

    # ===== EXPERIMENT F: Stitch then top-K =====
    # First filter to moving trajectories, then stitch fragments
    moving_trajs = [t for t in trajectories if t.max_displacement >= 30]
    print(f"\n--- Stitching {len(moving_trajs)} moving trajectories (disp >= 30px) ---")

    for max_gap in [3.0, 5.0, 8.0]:
        stitched = _stitch_trajectories(moving_trajs, max_time_gap_secs=max_gap)
        print(
            f"\n  Stitch gap={max_gap}s: {len(moving_trajs)} -> {len(stitched)} trajectories"
        )

        if stitched:
            top5 = sorted(
                stitched, key=lambda x: x.length * x.max_displacement, reverse=True
            )[:5]
            print("  Top 5 stitched trajectories:")
            for t in top5:
                print(
                    f"    len={t.length:4d}  disp={t.max_displacement:6.0f}px  "
                    f"path={t.total_path_length:7.0f}px  dur={t.duration_secs:5.1f}s  "
                    f"vel={t.avg_velocity:5.0f}px/s  seg={t.segment[:30]}"
                )

        for top_k in [1, 3, 5]:
            seg_trajs_f: dict[str, list[TrajectoryInfo]] = defaultdict(list)
            for t in stitched:
                seg_trajs_f[t.segment].append(t)

            stitch_set: set[int] = set()
            for seg, seg_ts in seg_trajs_f.items():
                ranked = sorted(
                    seg_ts, key=lambda x: x.length * x.max_displacement, reverse=True
                )
                for t in ranked[:top_k]:
                    stitch_set.add(id(t))

            _run_experiment(
                f"Exp F: Stitch (gap={max_gap}s) + top {top_k}/segment",
                stitched,
                lambda t: id(t) in stitch_set,
                segment_durations,
            )

    print(f"\n{'#' * 60}")
    print(f"  ANALYSIS COMPLETE: {game_id}")
    print(f"{'#' * 60}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Analyze trajectories and test filtering strategies"
    )
    parser.add_argument(
        "--labels",
        type=Path,
        required=True,
        help="Input labels directory for a single game",
    )
    parser.add_argument(
        "--persons",
        type=Path,
        default=None,
        help="Person labels directory for the same game (optional)",
    )
    parser.add_argument(
        "--game",
        type=str,
        default="unknown",
        help="Game ID for display purposes",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    analyze_game(args.labels, args.game, args.persons)


if __name__ == "__main__":
    main()
