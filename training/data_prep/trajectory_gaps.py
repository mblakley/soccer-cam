"""Trajectory gap detection from per-game manifest.db.

Builds ball trajectories from verified labels in the manifest, finds where
trajectories end or have mid-gaps, and provides candidates for Sonnet QA
and human review.

Used by sonnet_qa Phase 2 and generate_review.
"""

import logging
import re
import sqlite3
from collections import defaultdict

logger = logging.getLogger(__name__)

# Tile layout constants (shared with trajectory_validator.py)
TILE_SIZE = 640
STEP_X = 576  # (4096 - 640) / (7 - 1)
STEP_Y = 580  # (1800 - 640) / (3 - 1)
NUM_COLS = 7
NUM_ROWS = 3
MAX_LINK_DISTANCE = 400  # panoramic pixels

_TILE_RE = re.compile(r"^(.+)_frame_(\d{6})_r(\d+)_c(\d+)$")


def _tile_to_pano(
    cx_norm: float, cy_norm: float, row: int, col: int
) -> tuple[float, float]:
    """Convert normalized tile coordinates to panoramic pixel coordinates."""
    pano_x = col * STEP_X + cx_norm * TILE_SIZE
    pano_y = row * STEP_Y + cy_norm * TILE_SIZE
    return pano_x, pano_y


def _pano_to_tile(pano_x: float, pano_y: float) -> tuple[int, int, float, float] | None:
    """Convert panoramic coordinates to tile (row, col, cx_norm, cy_norm).

    Returns the tile whose center is closest, or None if out of bounds.
    """
    for row in range(NUM_ROWS):
        for col in range(NUM_COLS):
            tile_x0 = col * STEP_X
            tile_y0 = row * STEP_Y
            tcx = pano_x - tile_x0
            tcy = pano_y - tile_y0
            if 0 <= tcx < TILE_SIZE and 0 <= tcy < TILE_SIZE:
                return row, col, tcx / TILE_SIZE, tcy / TILE_SIZE
    return None


def _in_play_range(
    segment: str,
    frame_idx: int,
    play_ranges: list[tuple[str, int, str, int]],
) -> bool:
    """Check if a frame is within any active play phase range."""
    for seg_start, fi_start, seg_end, fi_end in play_ranges:
        after_start = (segment > seg_start) or (
            segment == seg_start and frame_idx >= fi_start
        )
        before_end = (segment < seg_end) or (segment == seg_end and frame_idx <= fi_end)
        if after_start and before_end:
            return True
    return False


def build_trajectories_from_manifest(
    conn: sqlite3.Connection,
    min_length: int = 5,
    max_link_distance: float = MAX_LINK_DISTANCE,
    play_phases_only: bool = False,
) -> list[list[tuple[int, str, float, float]]]:
    """Build ball trajectories from labels in the manifest.

    Uses all class_id=0 labels (not just verified ones) to build complete
    trajectories. Filters aggressively for game-ball-like motion:
    - Field polygon filter (detections must be on-field within 50px margin)
    - Displacement > 200px (game ball moves fast, not stationary)
    - Rejects false_positive labels if QA'd

    Requires a valid field_boundary polygon in manifest metadata.
    Returns empty list if no polygon is available.

    Args:
        play_phases_only: If True, only include frames from active play phases
            (first_half, second_half). Requires game_phases table to be populated.

    Returns list of trajectories. Each trajectory is a sorted list of
    (frame_idx, segment, pano_x, pano_y) tuples.
    """
    import json

    import cv2
    import numpy as np

    # Load field boundary polygon — required for trajectory building
    field_polygon = None
    try:
        fb_json = conn.execute(
            "SELECT value FROM metadata WHERE key = 'field_boundary'"
        ).fetchone()
        if fb_json:
            fb = json.loads(fb_json[0])
            if fb.get("polygon"):
                field_polygon = np.asarray(fb["polygon"], dtype=np.float32).reshape(
                    -1, 1, 2
                )
    except Exception:
        pass  # table may not exist in older manifests

    if field_polygon is None:
        logger.warning("No field boundary polygon — skipping trajectory building")
        return []

    # Load play phase ranges if filtering is requested
    play_ranges = None
    if play_phases_only:
        try:
            phase_rows = conn.execute(
                "SELECT phase, segment_start, frame_start, segment_end, frame_end "
                "FROM game_phases WHERE phase IN ('first_half', 'second_half') "
                "ORDER BY segment_start, frame_start"
            ).fetchall()
            if phase_rows:
                play_ranges = [(r[1], r[2], r[3], r[4]) for r in phase_rows]
        except Exception:
            pass  # table may not exist in older manifests

    # Use all labels, excluding known false positives
    rows = conn.execute(
        """SELECT tile_stem, cx, cy FROM labels
           WHERE class_id = 0
             AND (qa_verdict IS NULL OR qa_verdict IN ('true_positive', 'gap_ball_found'))"""
    ).fetchall()

    # Soft field boundary filtering thresholds.
    # The ball legitimately leaves the field (throw-ins, goal kicks, high kicks),
    # so we use a soft filter: keep all on-field, exclude far-off-field,
    # and keep some near-off-field detections.
    NEAR_OFF_FIELD_MARGIN = 150  # px outside polygon = "near off-field"

    # Parse tile_stems and convert to panoramic coordinates
    frame_dets: dict[tuple[str, int], list[tuple[float, float]]] = defaultdict(list)
    off_field_count = 0

    for tile_stem, cx, cy in rows:
        m = _TILE_RE.match(tile_stem)
        if not m:
            continue
        segment = m.group(1)
        frame_idx = int(m.group(2))
        row = int(m.group(3))
        col = int(m.group(4))

        # Skip frames outside active play phases
        if play_ranges and not _in_play_range(segment, frame_idx, play_ranges):
            continue

        pano_x, pano_y = _tile_to_pano(cx, cy, row, col)

        # Soft field boundary filter:
        # - On-field (dist >= 0): always keep
        # - Near off-field (-150px to 0): keep (ball can be near sideline)
        # - Far off-field (< -150px): exclude (spectator/equipment noise)
        dist = cv2.pointPolygonTest(field_polygon, (pano_x, pano_y), measureDist=True)
        if dist < -NEAR_OFF_FIELD_MARGIN:
            off_field_count += 1
            continue

        frame_dets[(segment, frame_idx)].append((pano_x, pano_y))

    if off_field_count > 0:
        logger.info(
            "Field filter: excluded %d far-off-field detections (>%dpx outside)",
            off_field_count,
            NEAR_OFF_FIELD_MARGIN,
        )

    if not frame_dets:
        return []

    # Group by segment, sort frame indices
    seg_frames: dict[str, list[int]] = defaultdict(list)
    for seg, fi in frame_dets:
        seg_frames[seg].append(fi)
    for seg in seg_frames:
        seg_frames[seg] = sorted(set(seg_frames[seg]))

    # Auto-detect frame interval
    all_fi = sorted(set(fi for _, fi in frame_dets))
    if len(all_fi) >= 2:
        gaps = [all_fi[i + 1] - all_fi[i] for i in range(min(100, len(all_fi) - 1))]
        gaps = [g for g in gaps if g > 0]
        frame_interval = min(gaps) if gaps else 4
    else:
        frame_interval = 4
    max_frame_gap = int(frame_interval * 2.5)

    # Greedy trajectory linking per segment
    all_trajectories = []
    static_count = 0

    for segment, frame_indices in seg_frames.items():
        active: list[list[tuple[int, str, float, float]]] = []
        finished: list[list[tuple[int, str, float, float]]] = []

        for fi in frame_indices:
            dets = frame_dets[(segment, fi)]
            used = [False] * len(dets)
            new_active = []

            for traj in active:
                last_fi = traj[-1][0]
                gap = fi - last_fi
                if gap > max_frame_gap or gap <= 0:
                    finished.append(traj)
                    continue

                n_intervals = max(gap / frame_interval, 1)
                best_idx = -1
                best_dist = max_link_distance * n_intervals
                last_x, last_y = traj[-1][2], traj[-1][3]

                for j, (px, py) in enumerate(dets):
                    if used[j]:
                        continue
                    dist = ((px - last_x) ** 2 + (py - last_y) ** 2) ** 0.5
                    if dist < best_dist:
                        best_dist = dist
                        best_idx = j

                if best_idx >= 0:
                    px, py = dets[best_idx]
                    traj.append((fi, segment, px, py))
                    used[best_idx] = True
                    new_active.append(traj)
                else:
                    finished.append(traj)

            for j, (px, py) in enumerate(dets):
                if not used[j]:
                    new_active.append([(fi, segment, px, py)])
            active = new_active

        finished.extend(active)

        # Filter by minimum length AND motion (reject static balls)
        for traj in finished:
            if len(traj) < min_length:
                continue

            # Compute displacement: max distance from first point to any other
            x0, y0 = traj[0][2], traj[0][3]
            max_disp = max(
                ((p[2] - x0) ** 2 + (p[3] - y0) ** 2) ** 0.5 for p in traj[1:]
            )

            # Game ball threshold: must move significantly (200px panoramic)
            # Static balls, equipment, and slow-moving false positives filtered out
            if max_disp < 200:
                static_count += 1
                continue

            all_trajectories.append(traj)

    logger.info(
        "Built %d moving trajectories (>=%d frames, rejected %d static) "
        "from %d detections across %d segments",
        len(all_trajectories),
        min_length,
        static_count,
        len(frame_dets),
        len(seg_frames),
    )
    return all_trajectories


def stitch_game_ball_track(
    trajectories: list[list[tuple[int, str, float, float]]],
    max_gap_seconds: float = 3.0,
    max_stitch_distance: float = 400.0,
    fps: float = 25.0,
    frame_interval: int = 4,
) -> list[list[tuple[int, str, float, float]]]:
    """Stitch moving trajectory fragments into continuous game ball tracks.

    The game has exactly one ball in play at a time. Nearby trajectory
    fragments (< max_gap_seconds apart, < max_stitch_distance px) are
    likely the same ball and get merged.

    Returns stitched trajectories sorted by total length (longest = game ball).
    """
    if not trajectories:
        return []

    max_gap_frames = int(max_gap_seconds * fps / frame_interval) * frame_interval

    # Sort fragments by start frame within each segment
    by_segment: dict[str, list] = {}
    for traj_idx, traj in enumerate(trajectories):
        seg = traj[0][1]
        by_segment.setdefault(seg, []).append((traj_idx, traj))

    stitched = []
    for seg, frags in by_segment.items():
        frags.sort(key=lambda f: f[1][0][0])  # sort by first frame_idx

        chains: list[list[tuple[int, str, float, float]]] = []
        for _, frag in frags:
            merged = False
            for chain in chains:
                last = chain[-1]
                first_new = frag[0]
                time_gap = first_new[0] - last[0]
                dist = (
                    (first_new[2] - last[2]) ** 2 + (first_new[3] - last[3]) ** 2
                ) ** 0.5

                if 0 < time_gap <= max_gap_frames and dist < max_stitch_distance:
                    chain.extend(frag)
                    merged = True
                    break

            if not merged:
                chains.append(list(frag))

        stitched.extend(chains)

    # Sort by length (longest first = most likely game ball)
    stitched.sort(key=lambda t: len(t), reverse=True)

    logger.info(
        "Stitched %d fragments into %d tracks (longest: %d frames)",
        len(trajectories),
        len(stitched),
        len(stitched[0]) if stitched else 0,
    )
    return stitched


def find_gap_candidates(
    trajectories: list[list[tuple[int, str, float, float]]],
    frame_interval: int = 4,
    max_gap_seconds: float = 3.0,
    fps: float = 25.0,
) -> list[dict]:
    """Find gaps in the game ball track where the detector lost the ball.

    Only considers gaps shorter than max_gap_seconds — longer gaps mean
    the ball went out of play (tracked as ball_events, not shown to human).

    Returns gap candidates sorted by priority.
    """
    max_gap_frames = int(max_gap_seconds * fps / frame_interval) * frame_interval
    gaps = []

    for traj_idx, traj in enumerate(trajectories):
        segment = traj[0][1]

        # Mid-trajectory gaps (interpolated positions)
        for i in range(1, len(traj)):
            fi_prev, _, x_prev, y_prev = traj[i - 1]
            fi_curr, _, x_curr, y_curr = traj[i]
            gap_frames = fi_curr - fi_prev

            if gap_frames <= frame_interval:
                continue  # no gap

            if gap_frames > max_gap_frames:
                # Ball was out of play for > 3 seconds — not a detection gap
                continue

            n_missing = (gap_frames // frame_interval) - 1
            for k in range(1, n_missing + 1):
                frac = k / (n_missing + 1)
                interp_fi = fi_prev + k * frame_interval
                interp_x = x_prev + frac * (x_curr - x_prev)
                interp_y = y_prev + frac * (y_curr - y_prev)

                gaps.append(
                    {
                        "segment": segment,
                        "frame_idx": interp_fi,
                        "pano_x": round(interp_x, 1),
                        "pano_y": round(interp_y, 1),
                        "gap_type": "mid_gap",
                        "trajectory_idx": traj_idx,
                        "trajectory_length": len(traj),
                        "gap_size": gap_frames,
                        "context_before": (fi_prev, x_prev, y_prev),
                        "context_after": (fi_curr, x_curr, y_curr),
                    }
                )

        # Track endpoints — where ball left play
        if len(traj) >= 10:
            last = traj[-1]
            prev = traj[-2]
            dx = last[2] - prev[2]
            dy = last[3] - prev[3]
            gaps.append(
                {
                    "segment": segment,
                    "frame_idx": last[0] + frame_interval,
                    "pano_x": round(last[2] + dx, 1),
                    "pano_y": round(last[3] + dy, 1),
                    "gap_type": "track_end",
                    "trajectory_idx": traj_idx,
                    "trajectory_length": len(traj),
                    "gap_size": frame_interval,
                    "context_before": (last[0], last[2], last[3]),
                    "context_after": None,
                }
            )

    # Sort: longer trajectories first, track_end > mid_gap
    type_priority = {"track_end": 0, "mid_gap": 1}
    gaps.sort(
        key=lambda g: (type_priority.get(g["gap_type"], 2), -g["trajectory_length"])
    )

    logger.info(
        "Found %d gap candidates from %d tracks (max gap: %.1fs)",
        len(gaps),
        len(trajectories),
        max_gap_seconds,
    )
    return gaps


def gap_to_tile_stem(
    segment: str, frame_idx: int, pano_x: float, pano_y: float
) -> str | None:
    """Convert a gap's panoramic position to a tile_stem.

    Returns tile_stem like '{segment}_frame_{frame_idx:06d}_r{row}_c{col}'
    or None if position is out of tile bounds.
    """
    result = _pano_to_tile(pano_x, pano_y)
    if result is None:
        return None
    row, col, _, _ = result
    return f"{segment}_frame_{frame_idx:06d}_r{row}_c{col}"


def filter_static_gaps(
    gaps: list[dict],
    conn: sqlite3.Connection,
    field_mask_path: str | None = None,
) -> list[dict]:
    """Remove gaps that are likely static balls or off-field.

    Filters:
    1. Gap position within 100px of a known static_ball (class_id=1) detection
    2. Gap in off-field area (if field mask provided)
    3. Gaps with pano_y outside field bounds (rough filter)
    """
    # Get known static ball positions
    static_positions = []
    rows = conn.execute(
        "SELECT tile_stem, cx, cy FROM labels WHERE class_id = 1"
    ).fetchall()
    for tile_stem, cx, cy in rows:
        m = _TILE_RE.match(tile_stem)
        if m:
            row, col = int(m.group(3)), int(m.group(4))
            px, py = _tile_to_pano(cx, cy, row, col)
            static_positions.append((px, py))

    # Load field mask if available
    field_polygon = None
    if field_mask_path:
        try:
            import json
            from pathlib import Path

            mask_path = Path(field_mask_path)
            if mask_path.exists():
                import numpy as np

                polygon = json.loads(mask_path.read_text())
                field_polygon = np.asarray(polygon, dtype=np.float32).reshape(-1, 1, 2)
        except Exception as e:
            logger.debug("Could not load field mask: %s", e)

    filtered = []
    static_threshold = 100  # panoramic pixels

    for gap in gaps:
        px, py = gap["pano_x"], gap["pano_y"]

        # Skip if near a known static ball
        near_static = False
        for sx, sy in static_positions:
            if ((px - sx) ** 2 + (py - sy) ** 2) ** 0.5 < static_threshold:
                near_static = True
                break
        if near_static:
            continue

        # Skip if off-field (field mask check)
        if field_polygon is not None:
            try:
                import cv2

                dist = cv2.pointPolygonTest(field_polygon, (px, py), measureDist=True)
                if dist < -50:  # well outside field
                    continue
            except Exception:
                pass

        # Rough bounds check (panoramic image is 4096x1800)
        if px < 0 or px > 4096 or py < 0 or py > 1800:
            continue

        filtered.append(gap)

    logger.info(
        "Filtered gaps: %d -> %d (removed %d static/off-field)",
        len(gaps),
        len(filtered),
        len(gaps) - len(filtered),
    )
    return filtered


def get_gap_context_frames(
    gap: dict,
    trajectory: list[tuple[int, str, float, float]],
    n_before: int = 3,
    n_after: int = 2,
) -> list[dict]:
    """Get context frames around a gap for filmstrip building.

    Returns list of frame info dicts with role 'before', 'gap', or 'after'.
    Each dict has: frame_idx, segment, pano_x, pano_y, role, tile_stem,
    tile_local_x, tile_local_y (normalized position within the tile).
    """
    segment = gap["segment"]
    gap_fi = gap["frame_idx"]
    gap_px, gap_py = gap["pano_x"], gap["pano_y"]

    frames = []

    # Before frames: last n_before trajectory points before the gap
    before_points = [(fi, seg, px, py) for fi, seg, px, py in trajectory if fi < gap_fi]
    for fi, seg, px, py in before_points[-n_before:]:
        tile_info = _pano_to_tile(px, py)
        if tile_info is None:
            continue
        row, col, cx_norm, cy_norm = tile_info
        frames.append(
            {
                "frame_idx": fi,
                "segment": seg,
                "pano_x": px,
                "pano_y": py,
                "role": "before",
                "tile_stem": f"{seg}_frame_{fi:06d}_r{row}_c{col}",
                "tile_local_x": cx_norm,
                "tile_local_y": cy_norm,
            }
        )

    # Gap frame
    tile_info = _pano_to_tile(gap_px, gap_py)
    if tile_info:
        row, col, cx_norm, cy_norm = tile_info
        frames.append(
            {
                "frame_idx": gap_fi,
                "segment": segment,
                "pano_x": gap_px,
                "pano_y": gap_py,
                "role": "gap",
                "tile_stem": f"{segment}_frame_{gap_fi:06d}_r{row}_c{col}",
                "tile_local_x": cx_norm,
                "tile_local_y": cy_norm,
            }
        )

    # After frames: first n_after trajectory points after the gap
    after_points = [(fi, seg, px, py) for fi, seg, px, py in trajectory if fi > gap_fi]
    for fi, seg, px, py in after_points[:n_after]:
        tile_info = _pano_to_tile(px, py)
        if tile_info is None:
            continue
        row, col, cx_norm, cy_norm = tile_info
        frames.append(
            {
                "frame_idx": fi,
                "segment": seg,
                "pano_x": px,
                "pano_y": py,
                "role": "after",
                "tile_stem": f"{seg}_frame_{fi:06d}_r{row}_c{col}",
                "tile_local_x": cx_norm,
                "tile_local_y": cy_norm,
            }
        )

    return frames


def build_gap_filmstrip(
    context_frames: list[dict],
    manifest,
    packs_dir,
    output_path,
) -> bool:
    """Build a filmstrip composite image for a trajectory gap.

    Shows context frames in a horizontal strip with ball positions marked:
    - Red circle: detected ball position (before/after frames)
    - Yellow circle + "?": interpolated gap position
    - Green circle: ball reappeared (after frames)

    Returns True if filmstrip was successfully created.
    """
    import cv2
    import numpy as np

    from training.tasks.sonnet_qa import _read_tile_from_packs

    tile_size = TILE_SIZE
    n_frames = len(context_frames)
    if n_frames == 0:
        return False

    # Create horizontal composite
    composite = np.zeros((tile_size, tile_size * n_frames, 3), dtype=np.uint8)

    for idx, frame in enumerate(context_frames):
        tile_stem = frame["tile_stem"]

        # Read tile from pack
        jpeg_bytes = _read_tile_from_packs(manifest, tile_stem, packs_dir)
        if jpeg_bytes is None:
            continue

        img = cv2.imdecode(np.frombuffer(jpeg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            continue
        if img.shape[:2] != (tile_size, tile_size):
            img = cv2.resize(img, (tile_size, tile_size))

        x_offset = idx * tile_size
        composite[:, x_offset : x_offset + tile_size] = img

        # Mark ball position
        bx = int(frame["tile_local_x"] * tile_size) + x_offset
        by = int(frame["tile_local_y"] * tile_size)
        radius = 20

        if frame["role"] == "before":
            # Red circle — confirmed detection
            cv2.circle(composite, (bx, by), radius, (0, 0, 255), 2)
        elif frame["role"] == "gap":
            # Yellow circle + "?" — expected position
            cv2.circle(composite, (bx, by), radius, (0, 255, 255), 3)
            cv2.putText(
                composite,
                "?",
                (bx - 8, by + 8),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (0, 255, 255),
                2,
            )
        elif frame["role"] == "after":
            # Green circle — ball reappeared
            cv2.circle(composite, (bx, by), radius, (0, 255, 0), 2)

        # Frame number label
        cv2.putText(
            composite,
            f"F{frame['frame_idx']}",
            (x_offset + 5, 25),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (255, 255, 255),
            1,
        )

    from pathlib import Path

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(output_path), composite, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return True
