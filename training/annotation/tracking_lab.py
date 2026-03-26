"""Tracking Lab: interactive single-segment ball tracking experiment.

Builds a chronological view of all detections in one video segment,
applies trajectory stitching to identify the "game ball", and serves
the results through the annotation server for visual review.

Usage:
    # Generate a tracking lab session for a specific segment
    python -m training.annotation.tracking_lab \
        --game heat__Heat_Tournament \
        --segment "08.35.21-08.52.16" \
        --output review_packets/tracking_lab
"""

import argparse
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

TILE_SIZE = 640
FRAME_INTERVAL = 8
FPS = 25.0


def build_tracking_lab(
    tiles_dir: Path,
    labels_dir: Path,
    game_id: str,
    segment_prefix: str,
    output_dir: Path,
    max_link_distance: float = MAX_LINK_DISTANCE,
    external_detections: Path | None = None,
) -> Path | None:
    """Build a tracking lab session for one segment.

    Creates a manifest with every frame in the segment, each frame's
    ball detections, and trajectory assignments.

    Returns path to the manifest file, or None if segment not found.
    """
    # Find the full segment name
    seg_name = None
    for lp in labels_dir.glob("*.txt"):
        parsed = parse_tile_filename(lp.stem)
        if parsed and segment_prefix in parsed[0]:
            seg_name = parsed[0]
            break

    if not seg_name:
        logger.error("Segment matching '%s' not found in %s", segment_prefix, labels_dir)
        return None

    logger.info("Building tracking lab for %s / %s", game_id, seg_name)

    # Phase 1: Parse all detections for this segment
    frame_dets: dict[int, list[dict]] = defaultdict(list)

    if external_detections and external_detections.exists():
        # Load from external detector JSON (panoramic coordinates)
        with open(external_detections) as f:
            ext_dets = json.load(f)
        for d in ext_dets:
            fi = d["frame_idx"]
            cx, cy = d["cx"], d["cy"]
            w = d.get("w", 15)
            h = d.get("h", 15)
            # Convert panoramic to tile coords
            col = max(0, min(6, int(cx / 576)))
            row = max(0, min(2, int(cy / 580)))
            tile_x = int(cx - col * 576)
            tile_y = int(cy - row * 580)
            tile_x = max(0, min(TILE_SIZE - 1, tile_x))
            tile_y = max(0, min(TILE_SIZE - 1, tile_y))
            frame_dets[fi].append({
                "pano_x": round(cx, 1),
                "pano_y": round(cy, 1),
                "row": row,
                "col": col,
                "cx_norm": round(tile_x / TILE_SIZE, 4),
                "cy_norm": round(tile_y / TILE_SIZE, 4),
                "w_norm": round(w / TILE_SIZE, 4),
                "h_norm": round(h / TILE_SIZE, 4),
                "tile_x": tile_x,
                "tile_y": tile_y,
                "conf": d.get("conf", 0),
            })
        logger.info("Loaded %d external detections", len(ext_dets))
    else:
        # Load from bootstrap YOLO tile labels
        for lp in sorted(labels_dir.glob("*.txt")):
            parsed = parse_tile_filename(lp.stem)
            if not parsed or parsed[0] != seg_name:
                continue
            seg, fi, row, col = parsed
            for cx_norm, cy_norm, line in _parse_detection(lp):
                px, py = _tile_to_pano(cx_norm, cy_norm, row, col)
                parts = line.split()
                w_norm = float(parts[3]) if len(parts) > 3 else 0.02
                h_norm = float(parts[4]) if len(parts) > 4 else 0.02
                frame_dets[fi].append({
                    "pano_x": round(px, 1),
                    "pano_y": round(py, 1),
                    "row": row,
                    "col": col,
                    "cx_norm": round(cx_norm, 4),
                    "cy_norm": round(cy_norm, 4),
                    "w_norm": round(w_norm, 4),
                    "h_norm": round(h_norm, 4),
                    "tile_x": int(cx_norm * TILE_SIZE),
                    "tile_y": int(cy_norm * TILE_SIZE),
                })

    # Phase 2: Find all frame timestamps (including no-detection frames)
    all_frame_indices = set()
    for tp in tiles_dir.glob("*.jpg"):
        parsed = parse_tile_filename(tp.stem)
        if parsed and parsed[0] == seg_name:
            all_frame_indices.add(parsed[1])

    sorted_frames = sorted(all_frame_indices)
    logger.info(
        "Segment has %d frames, %d with detections (%d total dets)",
        len(sorted_frames),
        len(frame_dets),
        sum(len(d) for d in frame_dets.values()),
    )

    # Phase 2b: Load user manual marks from feedback as ground truth
    user_marks: dict[int, dict] = {}  # frame_idx -> {row, col, x, y, pano_x, pano_y}
    feedback_path = output_dir / "feedback.json"
    if feedback_path.exists():
        with open(feedback_path) as f:
            feedback_list = json.load(f)
        for fb in feedback_list:
            if fb.get("action") == "mark_ball" and "row" in fb and "col" in fb:
                fi = fb["frame_idx"]
                px = fb["col"] * 576 + fb["x"]  # STEP_X=576
                py = fb["row"] * 580 + fb["y"]  # STEP_Y=580
                user_marks[fi] = {
                    "row": fb["row"], "col": fb["col"],
                    "x": fb["x"], "y": fb["y"],
                    "pano_x": round(px, 1), "pano_y": round(py, 1),
                }
        logger.info("Loaded %d user manual marks from feedback", len(user_marks))

    # Phase 3: User-guided detection selection
    # Instead of a blind tracker that commits to one hypothesis,
    # interpolate between user marks and pick the nearest detection.
    # This gets better as the user marks more frames.

    # Build guide path by interpolating between user marks
    guide_positions: dict[int, tuple[float, float]] = {}
    sorted_mark_items = sorted(
        ((fi, m["pano_x"], m["pano_y"]) for fi, m in user_marks.items())
    )

    if len(sorted_mark_items) >= 2:
        # Interpolate between consecutive marks
        for i in range(len(sorted_mark_items) - 1):
            fi_a, xa, ya = sorted_mark_items[i]
            fi_b, xb, yb = sorted_mark_items[i + 1]
            for fi in range(fi_a, fi_b + 1, FRAME_INTERVAL):
                t = (fi - fi_a) / max(fi_b - fi_a, 1)
                guide_positions[fi] = (xa + t * (xb - xa), ya + t * (yb - ya))

        # Extrapolate FORWARD from last mark using recent velocity
        last_fi, last_x, last_y = sorted_mark_items[-1]
        if len(sorted_mark_items) >= 3:
            prev_fi, prev_x, prev_y = sorted_mark_items[-3]
        else:
            prev_fi, prev_x, prev_y = sorted_mark_items[-2]
        dt = last_fi - prev_fi
        if dt > 0:
            vx = (last_x - prev_x) / dt * FRAME_INTERVAL
            vy = (last_y - prev_y) / dt * FRAME_INTERVAL
            # Extrapolate up to 30 frames (~10 seconds) with decaying confidence
            for step in range(1, 31):
                fi = last_fi + step * FRAME_INTERVAL
                if fi > sorted_frames[-1]:
                    break
                # Velocity decays over time (ball slows down)
                decay = 0.9 ** step
                ex = last_x + vx * step * decay
                ey = last_y + vy * step * decay
                # Clamp to field bounds
                ex = max(0, min(4096, ex))
                ey = max(0, min(1800, ey))
                guide_positions[fi] = (ex, ey)

        # Extrapolate BACKWARD from first mark
        first_fi, first_x, first_y = sorted_mark_items[0]
        if len(sorted_mark_items) >= 3:
            next_fi, next_x, next_y = sorted_mark_items[2]
        else:
            next_fi, next_x, next_y = sorted_mark_items[1]
        dt = next_fi - first_fi
        if dt > 0:
            vx = (first_x - next_x) / dt * FRAME_INTERVAL
            vy = (first_y - next_y) / dt * FRAME_INTERVAL
            for step in range(1, 31):
                fi = first_fi - step * FRAME_INTERVAL
                if fi < 0:
                    break
                decay = 0.9 ** step
                ex = first_x + vx * step * decay
                ey = first_y + vy * step * decay
                ex = max(0, min(4096, ex))
                ey = max(0, min(1800, ey))
                guide_positions[fi] = (ex, ey)

    elif len(sorted_mark_items) == 1:
        fi_a, xa, ya = sorted_mark_items[0]
        guide_positions[fi_a] = (xa, ya)

    logger.info("Guide path: %d interpolated positions from %d marks",
                len(guide_positions), len(user_marks))

    # For each frame, pick the detection closest to the guide (if within range)
    GUIDE_RADIUS = 500  # Accept detections within 500px (ball moves fast during kicks)
    guided_results: dict[int, tuple[float, float, float]] = {}  # fi -> (x, y, conf)

    matched_count = 0
    for fi in sorted_frames:
        # User marks are always included as-is
        if fi in user_marks:
            m = user_marks[fi]
            guided_results[fi] = (m["pano_x"], m["pano_y"], 1.0)
            continue

        # If we have a guide position, find nearest detection
        if fi in guide_positions:
            gx, gy = guide_positions[fi]
            dets = frame_dets.get(fi, [])
            best = None
            best_dist = float("inf")
            for d in dets:
                dist = ((d["pano_x"] - gx) ** 2 + (d["pano_y"] - gy) ** 2) ** 0.5
                if dist < best_dist:
                    best_dist = dist
                    best = d

            if best and best_dist < GUIDE_RADIUS:
                guided_results[fi] = (best["pano_x"], best["pano_y"], 0.5)
                matched_count += 1
            else:
                # No detection near guide — use interpolated position as prediction
                guided_results[fi] = (gx, gy, 0.0)

    # Fallback: for frames outside the guide range, show highest-confidence detection
    for fi in sorted_frames:
        if fi not in guided_results:
            dets = frame_dets.get(fi, [])
            if dets:
                best = max(dets, key=lambda d: d.get("conf", 0))
                guided_results[fi] = (best["pano_x"], best["pano_y"], best.get("conf", 0.5))

    logger.info("Guided selection: %d detection matches + %d interpolated + %d user marks + %d fallback",
                matched_count,
                max(0, len(guide_positions) - matched_count - len(user_marks)),
                len(user_marks),
                len(guided_results) - matched_count - len(user_marks))

    # Backwards compatibility: build tracker_positions from guided results
    tracker_positions = {fi: (x, y) for fi, (x, y, _) in guided_results.items()}

    # Compare against user marks
    match_count = 0
    mismatch_count = 0
    total_error = 0.0
    for fi, mark in user_marks.items():
        if fi in tracker_positions:
            tx, ty = tracker_positions[fi]
            error = ((tx - mark["pano_x"]) ** 2 + (ty - mark["pano_y"]) ** 2) ** 0.5
            total_error += error
            if error < 100:  # Within 100px = match
                match_count += 1
            else:
                mismatch_count += 1
                logger.warning(
                    "  MISMATCH frame %d: tracker=(%.0f,%.0f) user=(%.0f,%.0f) error=%.0fpx",
                    fi, tx, ty, mark["pano_x"], mark["pano_y"], error,
                )
        else:
            mismatch_count += 1
            logger.warning("  MISSING frame %d: no tracker position, user=(%.0f,%.0f)",
                fi, mark["pano_x"], mark["pano_y"])

    if user_marks:
        avg_error = total_error / max(match_count + mismatch_count, 1)
        logger.info(
            "User mark validation: %d/%d match (<100px), %d mismatch, avg_error=%.0fpx",
            match_count, len(user_marks), mismatch_count, avg_error,
        )

    best_trajectory = guided_results
    all_tracks = []
    best_track = None
    # (user marks + interpolated guide + matched detections)

    # Build summary of all tracks
    trajectory_list = []
    for track in all_tracks:
        if not track.detections:
            continue
        d0 = track.detections[0]
        dl = track.detections[-1]
        max_disp = max(
            ((d.x - d0.x) ** 2 + (d.y - d0.y) ** 2) ** 0.5
            for d in track.detections[1:]
        ) if len(track.detections) > 1 else 0.0

        total_path = 0.0
        for i in range(1, len(track.detections)):
            dx = track.detections[i].x - track.detections[i - 1].x
            dy = track.detections[i].y - track.detections[i - 1].y
            total_path += (dx**2 + dy**2) ** 0.5

        duration = (dl.frame_idx - d0.frame_idx) / FPS
        trajectory_list.append({
            "traj_id": track.track_id,
            "length": track.length,
            "max_displacement": round(max_disp, 1),
            "total_path": round(total_path, 1),
            "duration_secs": round(duration, 1),
            "avg_velocity": round(total_path / duration, 1) if duration > 0 else 0,
            "is_moving": max_disp >= 30,
            "start_frame": d0.frame_idx,
            "end_frame": dl.frame_idx,
            "is_best": best_track is not None and track.track_id == best_track.track_id,
        })

    trajectory_list.sort(
        key=lambda t: t["length"] * t["max_displacement"], reverse=True
    )

    # Build detection-to-track mapping
    det_track_map: dict[tuple[int, int], int] = {}
    for track in all_tracks:
        for det in track.detections:
            # Find which det_idx this detection corresponds to
            fi_dets = frame_dets.get(det.frame_idx, [])
            for di, d in enumerate(fi_dets):
                if abs(d["pano_x"] - det.x) < 1 and abs(d["pano_y"] - det.y) < 1:
                    det_track_map[(det.frame_idx, di)] = track.track_id
                    break

    # Phase 4: Build the manifest — one entry per frame
    frames = []
    for fi in sorted_frames:
        time_secs = round(fi / FPS, 2)
        dets = frame_dets.get(fi, [])

        # Annotate each detection with its track
        annotated_dets = []
        for det_idx, det in enumerate(dets):
            traj_id = det_track_map.get((fi, det_idx))
            det_copy = dict(det)
            det_copy["traj_id"] = traj_id
            annotated_dets.append(det_copy)

        # Best candidate: from the best track's trajectory (includes interpolated)
        best_det = None
        is_predicted = False
        if fi in best_trajectory:
            bx, by, bconf = best_trajectory[fi]
            is_predicted = bconf == 0.0
            # Find the matching detection if it's a real detection
            if not is_predicted:
                for d in annotated_dets:
                    if abs(d["pano_x"] - bx) < 1 and abs(d["pano_y"] - by) < 1:
                        best_det = d
                        break
            if best_det is None:
                # Predicted position or no exact match — create a synthetic entry
                # Convert panoramic back to tile coords
                from training.data_prep.trajectory_validator import STEP_X, STEP_Y
                # Find best tile for this panoramic position
                best_row = max(0, min(2, int(by / STEP_Y)))
                best_col = max(0, min(6, int(bx / STEP_X)))
                tile_x = int(bx - best_col * STEP_X)
                tile_y = int(by - best_row * STEP_Y)
                tile_x = max(0, min(TILE_SIZE - 1, tile_x))
                tile_y = max(0, min(TILE_SIZE - 1, tile_y))
                best_det = {
                    "pano_x": round(bx, 1),
                    "pano_y": round(by, 1),
                    "row": best_row,
                    "col": best_col,
                    "tile_x": tile_x,
                    "tile_y": tile_y,
                    "predicted": is_predicted,
                    "traj_id": best_track.track_id if best_track else None,
                }

        frames.append({
            "frame_idx": fi,
            "time_secs": time_secs,
            "detections": annotated_dets,
            "detection_count": len(annotated_dets),
            "best_candidate": best_det,
            "has_ball": best_det is not None,
            "is_predicted": is_predicted,
        })

    # Phase 6: Compute coverage stats
    total_frames = len(frames)
    tracked_frames = sum(1 for f in frames if f["has_ball"])
    coverage = tracked_frames / total_frames if total_frames > 0 else 0

    manifest = {
        "type": "tracking_lab",
        "game_id": game_id,
        "segment": seg_name,
        "segment_prefix": segment_prefix,
        "total_frames": total_frames,
        "tracked_frames": tracked_frames,
        "coverage_pct": round(coverage * 100, 1),
        "total_detections": sum(f["detection_count"] for f in frames),
        "trajectory_count": len(trajectory_list),
        "moving_trajectories": sum(1 for t in trajectory_list if t["is_moving"]),
        "duration_secs": round(sorted_frames[-1] / FPS, 1) if sorted_frames else 0,
        "trajectories": trajectory_list[:50],  # Top 50 trajectories
        "frames": frames,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    # Create empty feedback file
    feedback_path = output_dir / "feedback.json"
    if not feedback_path.exists():
        with open(feedback_path, "w") as f:
            json.dump([], f)

    logger.info(
        "Tracking lab created: %d frames, %d tracked (%.1f%%), %d trajectories (%d moving)",
        total_frames,
        tracked_frames,
        coverage * 100,
        len(trajectory_list),
        sum(1 for t in trajectory_list if t["is_moving"]),
    )

    return manifest_path


def main():
    parser = argparse.ArgumentParser(description="Build tracking lab session")
    parser.add_argument(
        "--game", required=True, help="Game ID (directory name)"
    )
    parser.add_argument(
        "--segment", required=True, help="Segment time prefix (e.g., '08.35.21-08.52.16')"
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
        default=Path("F:/training_data/labels_640_filtered"),
        help="Root labels directory",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("review_packets/tracking_lab"),
        help="Output directory for the lab session",
    )
    parser.add_argument(
        "--detections",
        type=Path,
        default=None,
        help="External detections JSON (panoramic coords, pre-filtered)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    build_tracking_lab(
        tiles_dir=args.tiles / args.game,
        labels_dir=args.labels / args.game,
        game_id=args.game,
        segment_prefix=args.segment,
        output_dir=args.output,
        external_detections=args.detections,
    )


if __name__ == "__main__":
    main()
