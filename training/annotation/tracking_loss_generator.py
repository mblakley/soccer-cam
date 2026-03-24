"""Generate annotation packets for frames where tracking likely lost the ball.

Finds tiles where the ball was detected in frame N but NOT in frame N+1
at the same tile position — these are the moments the model loses track.
Treats consecutive video segments as a single game for time-based scoring,
so warmup periods (first 2-3 segments) are properly deprioritized.

Usage:
    python -m training.annotation.tracking_loss_generator \
        --dataset F:/training_data/ball_dataset_640 \
        --tiles F:/training_data/tiles_640 \
        --output review_packets \
        --num 500 --packet-size 100
"""

import argparse
import json
import logging
import random
import re
import shutil
import time
from collections import defaultdict
from pathlib import Path

from training.data_prep.organize_dataset import parse_tile_filename

logger = logging.getLogger(__name__)

# Progress file for UI reporting
_progress_file: Path | None = None


def _report_progress(phase: str, detail: str, pct: float | None = None):
    """Write generation progress to a JSON file for the UI to poll."""
    if _progress_file is None:
        return
    data = {
        "phase": phase,
        "detail": detail,
        "pct": pct,
        "timestamp": time.time(),
    }
    try:
        _progress_file.write_text(json.dumps(data))
    except OSError:
        pass


TILE_SIZE = 640
# Frame interval between consecutive extracted frames (from extract_frames.py)
FRAME_INTERVAL = 8
# Gap between segments (seconds) that indicates a new game within the same directory
GAME_GAP_THRESHOLD = 1800  # 30 minutes
# Minimum pixel movement across a trajectory to consider it a real (moving) ball.
# Detections that stay within this radius are static objects (cones, bags, etc.)
MIN_TRAJECTORY_MOVEMENT_PX = 30


def _trajectory_movement(traj_frames: list[int], labeled: dict) -> float:
    """Compute max displacement (pixels) of detections across a trajectory.

    Returns the distance between the two most distant detection positions.
    Static objects (cones, bags, bottles) will have near-zero movement.
    Real game balls will move significantly across consecutive frames.
    """
    positions = []
    for fi in traj_frames:
        det = labeled.get(fi)
        if det:
            positions.append((det["x"], det["y"]))
    if len(positions) < 2:
        return 0.0
    # Max distance between any two positions (cheap: just check first vs all others)
    x0, y0 = positions[0]
    max_dist = 0.0
    for x, y in positions[1:]:
        d = ((x - x0) ** 2 + (y - y0) ** 2) ** 0.5
        if d > max_dist:
            max_dist = d
    return max_dist


def _parse_segment_time(segment_name: str) -> tuple[int, int] | None:
    """Extract (start_seconds, end_seconds) from segment filename timestamps.

    Segment names contain timestamps like '18.16.03-18.32.49[F][0@0][190830]'.
    Returns wall-clock seconds since midnight, or None if no timestamp found.
    """
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{2})-(\d{2})\.(\d{2})\.(\d{2})", segment_name)
    if not m:
        return None
    start = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    end = int(m.group(4)) * 3600 + int(m.group(5)) * 60 + int(m.group(6))
    return start, end


def _build_segment_game_map(
    segments: set[str],
) -> dict[str, tuple[int, float]]:
    """Map each segment to its game cluster and compute game-level time offsets.

    Consecutive segments form a game. A gap > GAME_GAP_THRESHOLD between
    segments indicates a new game (e.g. tournament with morning + evening games).

    Returns: {segment_name: (game_cluster_idx, game_start_wall_seconds)}
    where game_start_wall_seconds is the wall-clock start of that game cluster.
    """
    seg_times = []
    for seg in segments:
        t = _parse_segment_time(seg)
        if t:
            seg_times.append((t[0], t[1], seg))

    if not seg_times:
        return {}

    seg_times.sort()

    # Cluster into games by time gaps
    clusters: list[list[tuple[int, int, str]]] = [[seg_times[0]]]
    for s, e, name in seg_times[1:]:
        prev_end = clusters[-1][-1][1]
        if s - prev_end > GAME_GAP_THRESHOLD:
            clusters.append([])
        clusters[-1].append((s, e, name))

    result = {}
    for cluster_idx, cluster in enumerate(clusters):
        game_start = cluster[0][0]
        for _s, _e, name in cluster:
            result[name] = (cluster_idx, game_start)

    return result


def _compute_game_time(
    segment: str,
    frame_idx: int,
    segment_game_map: dict[str, tuple[int, float]],
) -> tuple[float, float, float]:
    """Compute game-level time for a frame.

    Returns: (time_secs_into_game, pct_through_game, game_duration_secs)
    """
    seg_info = segment_game_map.get(segment)
    if not seg_info:
        # Fallback: use frame_idx / 25fps as rough time
        return frame_idx / 25.0, 0.5, 0.0

    cluster_idx, game_start = seg_info
    seg_time = _parse_segment_time(segment)
    if not seg_time:
        return frame_idx / 25.0, 0.5, 0.0

    seg_start, _seg_end = seg_time

    # Find max frame_idx in this segment to estimate position within segment
    # frame_idx / 25fps gives seconds into THIS segment's video
    frame_time_in_segment = frame_idx / 25.0

    # Cumulative time = (segment start - game start) + time within segment
    time_into_game = (seg_start - game_start) + frame_time_in_segment

    # Find total game duration from all segments in this cluster
    game_end = max(
        end
        for name, (ci, _gs) in segment_game_map.items()
        if ci == cluster_idx
        for end in [_parse_segment_time(name)[1]]
        if _parse_segment_time(name) is not None
    )
    game_duration = game_end - game_start

    pct = time_into_game / game_duration if game_duration > 0 else 0.5
    pct = max(0.0, min(1.0, pct))

    return time_into_game, pct, game_duration


def _parse_label(label_path: Path) -> dict | None:
    """Parse YOLO label to get detection center and size."""
    try:
        text = label_path.read_text().strip()
        if not text:
            return None
        # Take first line (first detection)
        parts = text.split("\n")[0].split()
        if len(parts) < 5:
            return None
        cx_norm = float(parts[1])
        cy_norm = float(parts[2])
        w_norm = float(parts[3])
        h_norm = float(parts[4])
        return {
            "x": int(cx_norm * TILE_SIZE),
            "y": int(cy_norm * TILE_SIZE),
            "w_norm": w_norm,
            "h_norm": h_norm,
            "confidence": 1.0,
        }
    except (ValueError, IndexError):
        return None


def _build_tile_indices(
    dataset_path: Path,
    tiles_path: Path,
    split: str = "all",
) -> list[tuple[str, dict, dict, dict]]:
    """Build tile and label indices for games.

    Args:
        split: "train", "val", or "all" (both splits combined, no duplicates).

    Returns list of (game_id, tile_index, labeled_frames, segment_game_map) tuples.
    tile_index: {(segment, row, col): {frame_idx: Path}}
    labeled_frames: {(segment, row, col): {frame_idx: detection_dict}}
    segment_game_map: {segment_name: (game_cluster_idx, game_start_seconds)}
    """
    splits = ["train", "val"] if split == "all" else [split]

    # Collect all game_ids across requested splits, with their label dirs
    game_splits: dict[str, list[Path]] = {}
    for s in splits:
        images_dir = dataset_path / "images" / s
        if not images_dir.exists():
            continue
        for game_dir in sorted(images_dir.iterdir()):
            if game_dir.is_dir():
                label_dir = dataset_path / "labels" / s / game_dir.name
                game_splits.setdefault(game_dir.name, []).append(label_dir)

    if not game_splits:
        logger.warning("No games found in %s", dataset_path)
        return []

    game_list = [g for g in sorted(game_splits) if not g.startswith("camera__")]
    skipped = len(game_splits) - len(game_list)
    if skipped:
        logger.info("Skipping %d futsal games", skipped)
    logger.info("Scanning %d games across splits: %s", len(game_list), splits)
    results = []

    for game_num, game_id in enumerate(game_list, 1):
        _report_progress(
            "scanning",
            f"Scanning tiles: {game_id} ({game_num}/{len(game_list)})",
            game_num / len(game_list) * 0.7,  # scanning is ~70% of total time
        )

        label_dirs = game_splits[game_id]
        game_tiles_dir = tiles_path / game_id

        if not game_tiles_dir.exists():
            continue

        # Index all tiles by (segment, row, col) -> frame_idx -> path
        tile_index: dict[tuple, dict[int, Path]] = defaultdict(dict)
        all_segments: set[str] = set()
        for img_path in game_tiles_dir.glob("*.jpg"):
            parsed = parse_tile_filename(img_path.stem)
            if not parsed:
                continue
            segment, frame_idx, row, col = parsed
            tile_index[(segment, row, col)][frame_idx] = img_path
            all_segments.add(segment)

        # Build segment-to-game mapping (clusters consecutive segments)
        segment_game_map = _build_segment_game_map(all_segments)

        if segment_game_map:
            # Log game clusters
            clusters: dict[int, list] = defaultdict(list)
            for seg, (ci, gs) in segment_game_map.items():
                t = _parse_segment_time(seg)
                if t:
                    clusters[ci].append((t[0], t[1], seg))
            for ci, segs in sorted(clusters.items()):
                segs.sort()
                dur = (segs[-1][1] - segs[0][0]) / 60
                logger.info(
                    "  %s game %d: %d segments, %.0f min",
                    game_id, ci, len(segs), dur,
                )

        # Index labeled frames from all label dirs for this game
        labeled_frames: dict[tuple, dict[int, dict]] = defaultdict(dict)
        for label_game_dir in label_dirs:
            if not label_game_dir.exists():
                continue
            for label_path in label_game_dir.glob("*.txt"):
                parsed = parse_tile_filename(label_path.stem)
                if not parsed:
                    continue
                segment, frame_idx, row, col = parsed
                det = _parse_label(label_path)
                if det:
                    labeled_frames[(segment, row, col)][frame_idx] = det

        results.append((game_id, tile_index, labeled_frames, segment_game_map))

    return results


def find_tracking_losses(
    dataset_path: Path,
    tiles_path: Path,
    split: str = "all",
    min_trajectory_length: int = 3,
) -> list[dict]:
    """Find trajectory breaks where the game ball was likely lost.

    Strategy:
    1. At each tile position (segment, row, col), find runs of consecutive
       frames with detections = trajectories
    2. Keep only trajectories >= min_trajectory_length frames (real ball
       movement, not noise)
    3. For each trajectory end, if the NEXT frame has a tile but no
       detection, that's a tracking loss
    4. Score by trajectory length, field position, and ball size

    Returns list of dicts with tile info for annotation.
    """
    game_indices = _build_tile_indices(dataset_path, tiles_path, split)
    losses = []

    for gi, (game_id, tile_index, labeled_frames, segment_game_map) in enumerate(
        game_indices, 1
    ):
        _report_progress(
            "analyzing",
            f"Finding losses: {game_id} ({gi}/{len(game_indices)})",
            0.7 + gi / len(game_indices) * 0.2,  # 70-90% of total
        )
        for pos_key, frame_map in tile_index.items():
            segment, row, col = pos_key
            labeled = labeled_frames.get(pos_key, {})
            sorted_frames = sorted(frame_map.keys())

            # Build trajectories: runs of consecutive labeled frames
            trajectories = []
            current_run = []

            for frame_idx in sorted_frames:
                if frame_idx in labeled:
                    current_run.append(frame_idx)
                else:
                    if len(current_run) >= min_trajectory_length:
                        trajectories.append(current_run)
                    current_run = []
            if len(current_run) >= min_trajectory_length:
                trajectories.append(current_run)

            # For each trajectory, check if there's a loss frame after it
            for traj in trajectories:
                # Skip static objects: if detection barely moved across
                # the trajectory, it's a cone/bag/bottle, not a game ball
                movement = _trajectory_movement(traj, labeled)
                if movement < MIN_TRAJECTORY_MOVEMENT_PX:
                    continue

                last_frame = traj[-1]
                next_frame = last_frame + FRAME_INTERVAL

                if next_frame not in frame_map:
                    continue  # No tile exists for next frame
                if next_frame in labeled:
                    continue  # Ball still detected (shouldn't happen)

                prev_det = labeled[last_frame]
                loss_tile_path = frame_map[next_frame]

                # Compute game-level time (across all segments)
                time_into_game, pct_through_game, game_duration = (
                    _compute_game_time(segment, next_frame, segment_game_map)
                )

                losses.append(
                    {
                        "image_path": str(loss_tile_path),
                        "game_id": game_id,
                        "filename": loss_tile_path.name,
                        "row": row,
                        "col": col,
                        "frame_idx": next_frame,
                        "prev_frame_idx": last_frame,
                        "prev_detection": prev_det,
                        "trajectory_length": len(traj),
                        "time_secs": time_into_game,
                        "pct_through_game": pct_through_game,
                        "game_duration_secs": game_duration,
                        "priority_score": _priority_score(
                            row, col, prev_det, len(traj), pct_through_game
                        ),
                    }
                )

    logger.info(
        "Found %d trajectory-break losses (min traj length=%d)",
        len(losses),
        min_trajectory_length,
    )
    return losses


def _priority_score(
    row: int, col: int, prev_det: dict, trajectory_length: int, pct_through: float
) -> float:
    """Score for prioritizing which losses to annotate.

    Higher = more valuable. Prefers:
    - Later in game (actual game play, not warmup — warmup is first 2-3 segments)
    - Longer trajectories (more likely real game ball, not noise)
    - Center columns (c2-c4, mid-field action)
    - Row 1 (mid-field) over row 2 (too many sideline balls)
    - Smaller balls (harder, more valuable)
    """
    score = 0.0

    # Later in game = actual game play. Warmup spans first 2-3 video segments
    # (~30-50 min out of ~90 min total). pct_through is now computed across
    # ALL segments in the game, not per-segment.
    score += pct_through * 50.0

    # Trajectory length: ball tracked for many frames = almost certainly game ball
    score += min(trajectory_length, 20) * 2.0

    # Row preference: row 1 (mid-field) >> row 2 (far field)
    if row == 1:
        score += 10.0
    elif row == 2:
        score += 3.0

    # Center columns = game action
    if col in (2, 3, 4):
        score += 6.0
    elif col in (1, 5):
        score += 4.0
    elif col in (0, 6):
        score += 2.0

    # Smaller balls are harder and more valuable
    ball_size = (prev_det.get("w_norm", 0.03) + prev_det.get("h_norm", 0.03)) / 2
    if ball_size < 0.02:
        score += 6.0
    elif ball_size < 0.03:
        score += 4.0
    elif ball_size < 0.04:
        score += 2.0

    # Small random factor
    score += random.random() * 0.5

    return score


def _learn_exclusions(
    output_dir: Path,
) -> tuple[dict[str, float], dict[str, float], list[tuple[str, int, int, int, int]], set[str]]:
    """Learn what to exclude from previously annotated tracking loss packets.

    Reads all annotation_results.json + manifest.json from completed packets.

    Returns:
        warmup_cutoffs: {game_id: max_time_secs} — skip frames at or before this time
        gameover_cutoffs: {game_id: min_time_secs} — skip frames at or after this time
        static_balls: [(game_id, row, col, x, y)] — ball positions marked not_game_ball
        already_annotated: set of filenames already shown
    """
    warmup_cutoffs: dict[str, float] = {}
    gameover_cutoffs: dict[str, float] = {}
    static_balls: list[tuple[str, int, int, int, int]] = []
    already_annotated: set[str] = set()  # filenames we've already shown

    for packet_dir in sorted(output_dir.iterdir()):
        if not packet_dir.is_dir() or not packet_dir.name.startswith("tracking_loss_"):
            continue

        manifest_path = packet_dir / "manifest.json"
        results_path = packet_dir / "annotation_results.json"

        if not manifest_path.exists() or not results_path.exists():
            continue

        with open(manifest_path) as f:
            manifest = json.load(f)
        with open(results_path) as f:
            results = json.load(f)

        frame_lookup = {fr["frame_idx"]: fr for fr in manifest["frames"]}
        results_by_frame = {r["frame_idx"]: r for r in results}

        for frame_idx, result in results_by_frame.items():
            frame = frame_lookup.get(frame_idx)
            if not frame:
                continue

            ctx = frame.get("context", {})
            game_id = ctx.get("game_id", "")
            time_secs = ctx.get("time_secs", 0)
            filename = ctx.get("original_filename", "")
            action = result.get("action", "")

            if filename:
                already_annotated.add(filename)

            # Learn warmup cutoff: any frame marked warmup or auto-skipped as warmup
            if result.get("warmup"):
                current = warmup_cutoffs.get(game_id, 0)
                warmup_cutoffs[game_id] = max(current, time_secs)

            # Learn game-over cutoff: frames after game ended
            if result.get("game_over"):
                current = gameover_cutoffs.get(game_id, float("inf"))
                gameover_cutoffs[game_id] = min(current, time_secs)

            # Learn static ball positions from not_game_ball
            if action == "not_game_ball":
                row = ctx.get("row", -1)
                col = ctx.get("col", -1)
                det = frame.get("model_detection", {})
                if det and row >= 0 and col >= 0:
                    static_balls.append(
                        (game_id, row, col, det.get("x", 0), det.get("y", 0))
                    )

    logger.info(
        "Learned exclusions: %d warmup cutoffs, %d game-over cutoffs, "
        "%d static ball positions, %d already-annotated frames",
        len(warmup_cutoffs),
        len(gameover_cutoffs),
        len(static_balls),
        len(already_annotated),
    )
    for gid, cutoff in warmup_cutoffs.items():
        logger.info("  %s: warmup cutoff at %.0fs (%.1f min)", gid, cutoff, cutoff / 60)
    for gid, cutoff in gameover_cutoffs.items():
        logger.info(
            "  %s: game-over cutoff at %.0fs (%.1f min)", gid, cutoff, cutoff / 60
        )

    return warmup_cutoffs, gameover_cutoffs, static_balls, already_annotated


def _is_excluded(
    loss: dict,
    warmup_cutoffs: dict[str, float],
    gameover_cutoffs: dict[str, float],
    static_balls: list[tuple[str, int, int, int, int]],
    already_annotated: set[str],
    static_ball_radius: int = 80,
) -> str | None:
    """Check if a loss frame should be excluded based on learned patterns.

    Returns exclusion reason string, or None if not excluded.
    """
    game_id = loss["game_id"]
    time_secs = loss["time_secs"]
    filename = loss["filename"]

    # Already shown in a previous packet
    if filename in already_annotated:
        return "already_annotated"

    # In warmup period
    cutoff = warmup_cutoffs.get(game_id)
    if cutoff is not None and time_secs <= cutoff:
        return "warmup"

    # After game ended
    go_cutoff = gameover_cutoffs.get(game_id)
    if go_cutoff is not None and time_secs >= go_cutoff:
        return "game_over"

    # Near a known static ball (same row/col, similar position, within time window)
    row, col = loss["row"], loss["col"]
    prev = loss["prev_detection"]
    det_x, det_y = prev["x"], prev["y"]

    for sg, sr, sc, sx, sy in static_balls:
        if sg != game_id or sr != row or sc != col:
            continue
        dist = ((det_x - sx) ** 2 + (det_y - sy) ** 2) ** 0.5
        if dist < static_ball_radius:
            return "static_ball"

    return None


def generate_next_tracking_loss_packet(
    dataset_path: Path,
    tiles_path: Path,
    output_dir: Path,
    packet_size: int = 100,
    seed: int = 43,
) -> Path | None:
    """Generate a single annotation packet, learning from previous results.

    Reads all completed tracking_loss packets' annotations to learn:
    - Warmup time cutoffs per game (skip frames at or before that time)
    - Static out-of-play ball positions (skip nearby frames)
    - Already-annotated filenames (don't repeat)

    Then generates the next best packet from remaining candidates.

    Returns:
        Manifest path of the new packet, or None if no candidates remain.
    """
    global _progress_file
    _progress_file = output_dir / ".generation_progress.json"
    _report_progress("starting", "Learning from previous annotations...", 0.0)

    random.seed(seed)

    # Learn from previous annotations
    warmup_cutoffs, gameover_cutoffs, static_balls, already_annotated = (
        _learn_exclusions(output_dir)
    )

    _report_progress("learning", "Finding tracking losses across all games...", 0.05)

    # Find all tracking losses
    losses = find_tracking_losses(dataset_path, tiles_path, split="all")
    if not losses:
        logger.warning("No tracking losses found")
        _report_progress("done", "No tracking losses found", 1.0)
        _progress_file = None
        return None

    _report_progress("filtering", f"Filtering {len(losses)} candidates...", 0.9)

    # Filter out excluded losses
    filtered = []
    excluded_counts: dict[str, int] = defaultdict(int)
    for loss in losses:
        reason = _is_excluded(
            loss, warmup_cutoffs, gameover_cutoffs, static_balls, already_annotated
        )
        if reason:
            excluded_counts[reason] += 1
        else:
            filtered.append(loss)

    logger.info(
        "Filtered %d → %d candidates. Excluded: %s",
        len(losses),
        len(filtered),
        dict(excluded_counts),
    )

    if not filtered:
        logger.warning("No candidates remaining after exclusions")
        _report_progress("done", "No candidates remaining after exclusions", 1.0)
        _progress_file = None
        return None

    # Sort by priority and take top packet_size
    filtered.sort(key=lambda x: x["priority_score"], reverse=True)
    selected = filtered[:packet_size]
    random.shuffle(selected)

    logger.info(
        "Selected %d tiles. Row distribution: r1=%d, r2=%d",
        len(selected),
        sum(1 for t in selected if t["row"] == 1),
        sum(1 for t in selected if t["row"] == 2),
    )

    # Find next packet number
    existing = [
        d.name
        for d in output_dir.iterdir()
        if d.is_dir() and d.name.startswith("tracking_loss_")
    ]
    next_num = len(existing) + 1
    packet_id = f"tracking_loss_{next_num:03d}"

    _report_progress("creating", f"Creating {packet_id} with {len(selected)} tiles...", 0.95)

    manifest_path = _create_packet(output_dir, packet_id, selected)
    logger.info("Created packet %s with %d tiles", packet_id, len(selected))

    _report_progress("done", f"Packet {packet_id} ready!", 1.0)
    _progress_file = None
    return manifest_path


def _create_packet(output_dir: Path, packet_id: str, tiles: list[dict]) -> Path:
    """Create a review packet from tracking loss tiles."""
    packet_dir = output_dir / packet_id
    crops_dir = packet_dir / "crops"
    crops_dir.mkdir(parents=True, exist_ok=True)

    frames = []
    for idx, tile in enumerate(tiles):
        src = Path(tile["image_path"])
        crop_filename = f"frame_{idx:06d}.jpg"
        dst = crops_dir / crop_filename

        shutil.copy2(src, dst)

        # Show where the ball WAS in the previous frame as a hint
        prev = tile["prev_detection"]

        frame_entry = {
            "frame_idx": idx,
            "crop_file": f"crops/{crop_filename}",
            "crop_origin": {"x": 0, "y": 0, "w": TILE_SIZE, "h": TILE_SIZE},
            "source_resolution": {"w": TILE_SIZE, "h": TILE_SIZE},
            "model_detection": {
                "x": prev["x"],
                "y": prev["y"],
                "confidence": 0.0,  # Signal that this is a hint, not a detection
            },
            "reason": "tracking_loss",
            "context": {
                "game_id": tile["game_id"],
                "original_filename": tile["filename"],
                "row": tile["row"],
                "col": tile["col"],
                "prev_frame_idx": tile["prev_frame_idx"],
                "loss_frame_idx": tile["frame_idx"],
                "prev_ball_size": round(prev.get("w_norm", 0.03), 4),
                "trajectory_length": tile.get("trajectory_length", 1),
                "time_secs": tile.get("time_secs", 0),
                "game_duration_secs": tile.get("game_duration_secs", 0),
                "pct_through": round(tile.get("pct_through_game", 0), 2),
            },
        }
        frames.append(frame_entry)

    manifest = {
        "game_id": packet_id,
        "model_version": "tracking_loss",
        "source_video": None,
        "source_resolution": {"w": TILE_SIZE, "h": TILE_SIZE},
        "total_game_frames": len(frames),
        "packet_type": "tracking_loss",
        "frames": frames,
    }

    manifest_path = packet_dir / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    return manifest_path


def main():
    parser = argparse.ArgumentParser(
        description="Generate next tracking loss annotation packet"
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("F:/training_data/ball_dataset_640"),
    )
    parser.add_argument(
        "--tiles",
        type=Path,
        default=Path("F:/training_data/tiles_640"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("review_packets"),
    )
    parser.add_argument("--packet-size", type=int, default=100)
    parser.add_argument("--seed", type=int, default=43)

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    manifest = generate_next_tracking_loss_packet(
        dataset_path=args.dataset,
        tiles_path=args.tiles,
        output_dir=args.output,
        packet_size=args.packet_size,
        seed=args.seed,
    )
    if manifest:
        print(f"Generated packet: {manifest}")
    else:
        print("No candidates remaining — all exclusions applied")


if __name__ == "__main__":
    main()
