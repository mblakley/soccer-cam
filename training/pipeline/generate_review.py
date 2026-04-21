"""Generate human review packets from manifest for the annotation server.

Finds the most valuable tiles for human review:
1. Low-confidence ONNX detections (conf 0.45-0.60) — likely false positives
2. Edge tiles (row 2, cols 0/6) — ball near frame boundary, hard to detect
3. Trajectory breaks — frames where ball tracking gaps start/end
4. Isolated detections — single detection with no neighbors (suspicious)

Reads tile images from pack files, saves crops for the annotation server UI.

Usage:
    uv run python -m training.pipeline.generate_review --count 500
    uv run python -m training.pipeline.generate_review --count 200 --game flash__2024.06.01
"""

import argparse
import json
import logging
import re
import sqlite3
import time
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)

DB_PATH = Path("D:/training_data/manifest.db")
REVIEW_DIR = Path("review_packets/ball_verify")
TILE_SIZE = 640
TILE_RE = re.compile(r"^(.+)_frame_(\d{6})_r(\d+)_c(\d+)$")


def read_tile_from_pack(conn, game_id, tile_stem):
    """Read a tile's JPEG bytes from its pack file."""
    m = TILE_RE.match(tile_stem)
    if not m:
        return None
    seg, fidx, row, col = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))

    r = conn.execute(
        "SELECT pack_file, pack_offset, pack_size FROM tiles "
        "WHERE game_id=? AND segment=? AND frame_idx=? AND row=? AND col=?",
        (game_id, seg, fidx, row, col),
    ).fetchone()
    if not r or not r[0]:
        return None

    with open(r[0], "rb") as f:
        f.seek(r[1])
        data = f.read(r[2])
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)


def _find_trajectory_breaks(conn, game_id, frame_interval=4):
    """Find frames where solid ball tracks suddenly end.

    A trajectory break = detection at frame N, no detection at frame N+interval.
    Returns list of (tile_stem, cx, cy, w, h, confidence, gap_length).
    The tile_stem is the LAST detection before the gap.
    """
    # Get all detections for this game, ordered by tile_stem
    # tile_stem encodes segment + frame_idx, so sorting gives temporal order
    rows = conn.execute(
        "SELECT tile_stem, cx, cy, w, h, confidence FROM labels "
        "WHERE game_id = ? AND confidence IS NOT NULL "
        "ORDER BY tile_stem",
        (game_id,),
    ).fetchall()

    if not rows:
        return []

    # Group detections by segment + (row, col) to track per-tile trajectories
    from collections import defaultdict

    tracks = defaultdict(
        list
    )  # (segment, row, col) -> sorted list of (frame_idx, cx, cy, w, h, conf, stem)

    for stem, cx, cy, w, h, conf in rows:
        m = TILE_RE.match(stem)
        if not m:
            continue
        seg = m.group(1)
        fidx = int(m.group(2))
        row, col = int(m.group(3)), int(m.group(4))
        tracks[(seg, row, col)].append((fidx, cx, cy, w, h, conf, stem))

    breaks = []
    for key, detections in tracks.items():
        if len(detections) < 3:
            continue  # need some track history

        detections.sort()

        # Find gaps: consecutive frames with detection, then gap
        for i in range(2, len(detections)):
            prev_fidx = detections[i - 1][0]
            curr_fidx = detections[i][0]
            gap = curr_fidx - prev_fidx

            if gap > frame_interval * 3:
                # This is a trajectory break — the detection before the gap is interesting
                # Also the detection after the gap
                # Check that we had a solid track before (at least 2 consecutive)
                prev_prev_fidx = detections[i - 2][0]
                if prev_fidx - prev_prev_fidx <= frame_interval * 2:
                    # Solid track ended — last detection before gap
                    _, cx, cy, w, h, conf, stem = detections[i - 1]
                    breaks.append((stem, cx, cy, w, h, conf, gap))
                    # First detection after gap
                    _, cx2, cy2, w2, h2, conf2, stem2 = detections[i]
                    breaks.append((stem2, cx2, cy2, w2, h2, conf2, gap))

    return breaks


def get_review_candidates(conn, count=500, game_filter=None):
    """Find the most valuable tiles for human review, prioritized.

    Priority order:
    1. Trajectory breaks — where solid tracks suddenly end (highest value)
    2. Low-confidence detections — likely false positives
    3. Edge tile detections — ball near frame boundary
    4. Calibration samples — random medium-confidence for baseline
    """
    candidates = []

    # Get games with confidence data
    if game_filter:
        games = [(game_filter,)]
    else:
        games = conn.execute(
            "SELECT DISTINCT game_id FROM labels WHERE confidence IS NOT NULL"
        ).fetchall()

    # Strategy 1: Trajectory breaks (HIGHEST PRIORITY)
    logger.info("Finding trajectory breaks...")
    for (gid,) in games:
        breaks = _find_trajectory_breaks(conn, gid)
        for stem, cx, cy, w, h, conf, gap_len in breaks:
            m = TILE_RE.match(stem)
            if not m:
                continue
            row, col = int(m.group(3)), int(m.group(4))
            # Higher gap = more interesting (longer track loss)
            priority = min(200, 120 + gap_len // 4)
            candidates.append(
                {
                    "game_id": gid,
                    "tile_stem": stem,
                    "cx": cx,
                    "cy": cy,
                    "w": w,
                    "h": h,
                    "confidence": conf,
                    "source": "onnx",
                    "priority": priority,
                    "reason": f"track_break (gap={gap_len} frames, conf={conf:.2f})",
                    "row": row,
                    "col": col,
                }
            )

    logger.info("  Found %d trajectory break candidates", len(candidates))

    # Strategy 2: Low-confidence detections
    query = """
        SELECT game_id, tile_stem, cx, cy, w, h, confidence, source
        FROM labels WHERE confidence IS NOT NULL AND confidence < 0.55
    """
    params = []
    if game_filter:
        query += " AND game_id = ?"
        params.append(game_filter)
    query += " ORDER BY confidence ASC LIMIT ?"
    params.append(count)

    for gid, stem, cx, cy, w, h, conf, source in conn.execute(query, params).fetchall():
        m = TILE_RE.match(stem)
        if not m:
            continue
        row, col = int(m.group(3)), int(m.group(4))
        priority = 100 - int(conf * 100)
        if col in (0, 6) or row == 2:
            priority += 15
        candidates.append(
            {
                "game_id": gid,
                "tile_stem": stem,
                "cx": cx,
                "cy": cy,
                "w": w,
                "h": h,
                "confidence": conf,
                "source": source,
                "priority": priority,
                "reason": f"low_conf ({conf:.2f})",
                "row": row,
                "col": col,
            }
        )

    # Strategy 3: Edge tiles with medium confidence
    edge_query = """
        SELECT game_id, tile_stem, cx, cy, w, h, confidence, source
        FROM labels WHERE confidence IS NOT NULL AND confidence BETWEEN 0.55 AND 0.75
    """
    edge_params = []
    if game_filter:
        edge_query += " AND game_id = ?"
        edge_params.append(game_filter)
    edge_query += " ORDER BY RANDOM() LIMIT ?"
    edge_params.append(count // 2)

    for gid, stem, cx, cy, w, h, conf, source in conn.execute(
        edge_query, edge_params
    ).fetchall():
        m = TILE_RE.match(stem)
        if not m:
            continue
        row, col = int(m.group(3)), int(m.group(4))
        if col not in (0, 6) and row != 2:
            continue
        candidates.append(
            {
                "game_id": gid,
                "tile_stem": stem,
                "cx": cx,
                "cy": cy,
                "w": w,
                "h": h,
                "confidence": conf,
                "source": source,
                "priority": 60,
                "reason": f"edge r{row}c{col} ({conf:.2f})",
                "row": row,
                "col": col,
            }
        )

    # Strategy 4: Calibration samples
    cal_query = """
        SELECT game_id, tile_stem, cx, cy, w, h, confidence, source
        FROM labels WHERE confidence IS NOT NULL AND confidence BETWEEN 0.75 AND 0.95
    """
    cal_params = []
    if game_filter:
        cal_query += " AND game_id = ?"
        cal_params.append(game_filter)
    cal_query += " ORDER BY RANDOM() LIMIT ?"
    cal_params.append(count // 10)

    for gid, stem, cx, cy, w, h, conf, source in conn.execute(
        cal_query, cal_params
    ).fetchall():
        m = TILE_RE.match(stem)
        if not m:
            continue
        row, col = int(m.group(3)), int(m.group(4))
        candidates.append(
            {
                "game_id": gid,
                "tile_stem": stem,
                "cx": cx,
                "cy": cy,
                "w": w,
                "h": h,
                "confidence": conf,
                "source": source,
                "priority": 20,
                "reason": f"calibration ({conf:.2f})",
                "row": row,
                "col": col,
            }
        )

    # Deduplicate, sort by priority, take top N
    seen = {}
    for c in candidates:
        key = f"{c['game_id']}/{c['tile_stem']}"
        if key not in seen or c["priority"] > seen[key]["priority"]:
            seen[key] = c

    result = sorted(seen.values(), key=lambda x: -x["priority"])[:count]
    logger.info(
        "Final candidates: %d (breaks: %d, low_conf: %d, edge: %d, cal: %d)",
        len(result),
        sum(1 for r in result if "track_break" in r["reason"]),
        sum(1 for r in result if "low_conf" in r["reason"]),
        sum(1 for r in result if "edge" in r["reason"]),
        sum(1 for r in result if "calibration" in r["reason"]),
    )
    return result


def generate_review_packet(count=500, game_filter=None):
    """Generate a review packet with crops from pack files."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")

    logger.info("Finding %d review candidates...", count)
    candidates = get_review_candidates(conn, count, game_filter)
    logger.info("Found %d candidates", len(candidates))

    # Create output dirs
    crops_dir = REVIEW_DIR / "crops"
    full_dir = REVIEW_DIR / "full_frames"
    crops_dir.mkdir(parents=True, exist_ok=True)
    full_dir.mkdir(parents=True, exist_ok=True)

    # Generate crops
    frames = []
    written = 0
    for cand in candidates:
        tile_img = read_tile_from_pack(conn, cand["game_id"], cand["tile_stem"])
        if tile_img is None:
            continue

        i = written  # sequential index for filenames

        # Save full tile
        full_path = full_dir / f"full_{i:05d}.jpg"
        cv2.imwrite(str(full_path), tile_img)

        # Draw detection box and save crop
        h, w = tile_img.shape[:2]
        bx = int(cand["cx"] * w)
        by = int(cand["cy"] * h)
        bw = int(cand["w"] * w)
        bh = int(cand["h"] * h)

        # Crop around detection with padding
        pad = max(bw, bh, 60)
        x1 = max(0, bx - pad)
        y1 = max(0, by - pad)
        x2 = min(w, bx + pad)
        y2 = min(h, by + pad)
        crop = tile_img[y1:y2, x1:x2].copy()

        # Draw crosshair on crop
        cx_crop = bx - x1
        cy_crop = by - y1
        cv2.circle(crop, (cx_crop, cy_crop), max(bw, bh) // 2 + 3, (0, 255, 0), 2)
        cv2.circle(crop, (cx_crop, cy_crop), 3, (0, 0, 255), -1)

        crop_path = crops_dir / f"crop_{i:05d}.jpg"
        cv2.imwrite(str(crop_path), crop)

        frames.append(
            {
                "frame_idx": i,
                "game_id": cand["game_id"],
                "tile_stem": cand["tile_stem"],
                "confidence": cand["confidence"],
                "reason": cand["reason"],
                "cx": cand["cx"],
                "cy": cand["cy"],
                "w": cand["w"],
                "h": cand["h"],
                "row": cand["row"],
                "col": cand["col"],
            }
        )

        written += 1
        if written % 50 == 0:
            logger.info("  Generated %d crops", written)

    # Write manifest
    manifest = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_frames": len(frames),
        "source": "manifest_db",
        "description": "Ball detection review - low confidence + edge cases",
        "frames": frames,
    }
    manifest_path = REVIEW_DIR / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    # Clear old results so you start fresh
    results_path = REVIEW_DIR / "verification_results.json"
    if results_path.exists():
        results_path.unlink()

    conn.close()
    logger.info("Review packet: %d frames in %s", len(frames), REVIEW_DIR)
    logger.info("Start the annotation server:")
    logger.info("  uvicorn training.annotation_server:app --host 0.0.0.0 --port 8642")
    return len(frames)


def main():
    global DB_PATH

    parser = argparse.ArgumentParser(description="Generate human review packets")
    parser.add_argument("--count", type=int, default=500, help="Number of candidates")
    parser.add_argument("--game", type=str, default=None, help="Filter to one game")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="Manifest DB path")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    DB_PATH = args.db

    generate_review_packet(args.count, args.game)


if __name__ == "__main__":
    main()
