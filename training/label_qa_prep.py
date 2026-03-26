"""Prepare composite grid images for Sonnet agent review.

Queries the SQLite cache, samples labels, and creates composite grid images
(6 tiles per image in a 3x2 grid) for positive and negative batch review.

Also stitches panoramic frames for game phase classification.

Usage:
    uv run python -m training.label_qa_prep
    uv run python -m training.label_qa_prep --games heat__05.31.2024_vs_Fairport_home
    uv run python -m training.label_qa_prep --sample-rate 0.10
"""

import argparse
import json
import logging
import random
import sqlite3
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

from training.label_qa_cache import (
    DEFAULT_DB_PATH,
    DEFAULT_TILES_DIR,
    reconstruct_panoramic,
)

logger = logging.getLogger(__name__)

BATCH_SIZE = 6  # tiles per composite grid
GRID_COLS = 3
GRID_ROWS = 2
TILE_SIZE = 640
COMPOSITE_W = GRID_COLS * TILE_SIZE  # 1920
COMPOSITE_H = GRID_ROWS * TILE_SIZE  # 1280

MIN_SAMPLES_PER_GAME = 50
MAX_SAMPLES_PER_GAME = 500
NEGATIVE_PER_POSITIVE_FRAME = 2

# Number of panoramic frames for phase classification
PHASE_FRAME_COUNT = 8


def sample_positives(
    conn: sqlite3.Connection,
    game_id: str,
    sample_rate: float,
) -> list[dict]:
    """Sample positive labels for QA review, stratified by segment."""
    rows = conn.execute(
        """SELECT id, segment, frame_idx, row, col, cx, cy, w, h,
                  pano_x, pano_y, tile_path, in_field
           FROM labels
           WHERE game_id = ? AND is_positive = 1
                 AND (in_field = 1 OR in_field IS NULL)
           ORDER BY segment, frame_idx""",
        (game_id,),
    ).fetchall()

    if not rows:
        return []

    # Group by segment for stratified sampling
    by_segment = defaultdict(list)
    for r in rows:
        by_segment[r[1]].append(r)

    total_target = max(
        MIN_SAMPLES_PER_GAME,
        min(MAX_SAMPLES_PER_GAME, int(len(rows) * sample_rate)),
    )

    # Proportional sampling per segment
    sampled = []
    for seg, seg_rows in by_segment.items():
        seg_target = max(1, int(total_target * len(seg_rows) / len(rows)))
        seg_sample = random.sample(seg_rows, min(seg_target, len(seg_rows)))
        sampled.extend(seg_sample)

    # Trim to target if oversampled
    if len(sampled) > total_target:
        sampled = random.sample(sampled, total_target)

    return [
        {
            "id": r[0],
            "segment": r[1],
            "frame_idx": r[2],
            "row": r[3],
            "col": r[4],
            "cx": r[5],
            "cy": r[6],
            "w": r[7],
            "h": r[8],
            "pano_x": r[9],
            "pano_y": r[10],
            "tile_path": r[11],
            "in_field": r[12],
        }
        for r in sampled
    ]


def sample_negatives(
    conn: sqlite3.Connection,
    game_id: str,
    positive_samples: list[dict],
    tiles_dir: Path,
) -> list[dict]:
    """Sample negative tiles (no detection) from frames with positive detections.

    For each positive frame, find tiles at the same frame that have no label file
    in labels_640_ext (i.e., the ext model found nothing there).
    """
    # Get unique (segment, frame_idx) from positive samples
    positive_frames = {(s["segment"], s["frame_idx"]) for s in positive_samples}

    # For each positive frame, find tiles that exist but have no ext label
    negatives = []
    for seg, frame_idx in positive_frames:
        # Get all tiles that DO have labels for this frame
        labeled_tiles = set(
            conn.execute(
                """SELECT row, col FROM labels
                   WHERE game_id = ? AND segment = ? AND frame_idx = ?""",
                (game_id, seg, frame_idx),
            ).fetchall()
        )

        # Generate candidate negative tiles (rows 1-2, all cols)
        candidates = []
        for row in range(1, 3):  # Skip row 0
            for col in range(7):
                if (row, col) not in labeled_tiles:
                    tile_path = str(
                        tiles_dir
                        / game_id
                        / f"{seg}_frame_{frame_idx:06d}_r{row}_c{col}.jpg"
                    )
                    candidates.append(
                        {
                            "segment": seg,
                            "frame_idx": frame_idx,
                            "row": row,
                            "col": col,
                            "tile_path": tile_path,
                        }
                    )

        if candidates:
            sample_n = min(NEGATIVE_PER_POSITIVE_FRAME, len(candidates))
            negatives.extend(random.sample(candidates, sample_n))

    # Cap at same count as positives
    if len(negatives) > len(positive_samples):
        negatives = random.sample(negatives, len(positive_samples))

    return negatives


def create_composite_grid(
    tiles: list[dict],
    draw_boxes: bool = False,
) -> np.ndarray | None:
    """Create a 3x2 composite grid from up to 6 tiles.

    Args:
        tiles: List of tile dicts with 'tile_path' and optionally 'cx', 'cy', 'w', 'h'
        draw_boxes: If True, draw yellow bounding boxes on tiles

    Returns:
        Composite image (COMPOSITE_H x COMPOSITE_W x 3) or None if no tiles loaded.
    """
    composite = np.zeros((COMPOSITE_H, COMPOSITE_W, 3), dtype=np.uint8)
    loaded = 0

    for i, tile_info in enumerate(tiles[:BATCH_SIZE]):
        tile_path = tile_info["tile_path"]
        tile = cv2.imread(tile_path)
        if tile is None:
            # Draw placeholder with tile number
            row_idx = i // GRID_COLS
            col_idx = i % GRID_COLS
            x_off = col_idx * TILE_SIZE
            y_off = row_idx * TILE_SIZE
            cv2.putText(
                composite,
                f"#{i + 1} MISSING",
                (x_off + 200, y_off + 320),
                cv2.FONT_HERSHEY_SIMPLEX,
                1.5,
                (0, 0, 255),
                3,
            )
            continue

        # Resize if needed (should be 640x640)
        if tile.shape[:2] != (TILE_SIZE, TILE_SIZE):
            tile = cv2.resize(tile, (TILE_SIZE, TILE_SIZE))

        # Draw bounding box if requested
        if draw_boxes and tile_info.get("cx") is not None:
            cx = tile_info["cx"] * TILE_SIZE
            cy = tile_info["cy"] * TILE_SIZE
            w = tile_info["w"] * TILE_SIZE
            h = tile_info["h"] * TILE_SIZE
            x1 = int(cx - w / 2)
            y1 = int(cy - h / 2)
            x2 = int(cx + w / 2)
            y2 = int(cy + h / 2)
            cv2.rectangle(tile, (x1, y1), (x2, y2), (0, 255, 255), 3)

        # Place in grid
        row_idx = i // GRID_COLS
        col_idx = i % GRID_COLS
        x_off = col_idx * TILE_SIZE
        y_off = row_idx * TILE_SIZE
        composite[y_off : y_off + TILE_SIZE, x_off : x_off + TILE_SIZE] = tile

        # Draw tile number
        cv2.putText(
            composite,
            str(i + 1),
            (x_off + 10, y_off + 50),
            cv2.FONT_HERSHEY_SIMPLEX,
            2.0,
            (0, 255, 255),
            4,
        )

        loaded += 1

    return composite if loaded > 0 else None


def create_batches(
    items: list[dict],
    output_dir: Path,
    batch_prefix: str,
    draw_boxes: bool = False,
) -> int:
    """Create composite grid images for a list of items.

    Returns number of batches created.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    batch_count = 0

    for i in range(0, len(items), BATCH_SIZE):
        batch = items[i : i + BATCH_SIZE]
        composite = create_composite_grid(batch, draw_boxes=draw_boxes)
        if composite is None:
            continue

        batch_id = f"{batch_prefix}_{batch_count:03d}"

        # Save composite image
        img_path = output_dir / f"{batch_id}.jpg"
        cv2.imwrite(str(img_path), composite, [cv2.IMWRITE_JPEG_QUALITY, 90])

        # Save metadata
        meta_path = output_dir / f"{batch_id}.json"
        meta = {
            "batch_id": batch_id,
            "tiles": [
                {k: v for k, v in t.items() if k != "tile_path"}
                | {"tile_path": t["tile_path"]}
                for t in batch
            ],
        }
        meta_path.write_text(json.dumps(meta, indent=2))

        batch_count += 1

    return batch_count


def create_phase_frames(
    conn: sqlite3.Connection,
    game_id: str,
    tiles_dir: Path,
    output_dir: Path,
) -> int:
    """Create panoramic frames spread across the game for phase classification."""
    # Get all distinct segments ordered by timestamp
    segments = conn.execute(
        """SELECT DISTINCT segment, timestamp_start, timestamp_end
           FROM labels WHERE game_id = ?
           ORDER BY timestamp_start""",
        (game_id,),
    ).fetchall()

    if not segments:
        return 0

    # Pick evenly spaced segments
    step = max(1, len(segments) // PHASE_FRAME_COUNT)
    selected = segments[::step][:PHASE_FRAME_COUNT]

    phase_dir = output_dir / "phase_frames"
    phase_dir.mkdir(parents=True, exist_ok=True)
    created = 0

    for seg_name, ts_start, ts_end in selected:
        # Find a frame_idx in the middle of this segment
        frame_rows = conn.execute(
            """SELECT DISTINCT frame_idx FROM labels
               WHERE game_id = ? AND segment = ?
               ORDER BY frame_idx""",
            (game_id, seg_name),
        ).fetchall()

        if not frame_rows:
            continue

        mid_frame = frame_rows[len(frame_rows) // 2][0]
        pano = reconstruct_panoramic(tiles_dir, game_id, seg_name, mid_frame)
        if pano is None:
            continue

        fname = f"phase_{ts_start.replace(':', '')}_{seg_name[:20]}.jpg"
        cv2.imwrite(str(phase_dir / fname), pano, [cv2.IMWRITE_JPEG_QUALITY, 85])
        created += 1

    logger.info("Created %d phase frames for %s", created, game_id)
    return created


def prep_game(
    conn: sqlite3.Connection,
    game_id: str,
    tiles_dir: Path,
    output_dir: Path,
    sample_rate: float,
) -> dict:
    """Prepare all review materials for one game."""
    game_dir = output_dir / game_id
    game_dir.mkdir(parents=True, exist_ok=True)

    stats = {}

    # Sample positives
    positives = sample_positives(conn, game_id, sample_rate)
    stats["positives_sampled"] = len(positives)
    logger.info("%s: sampled %d positives", game_id, len(positives))

    # Create positive batches
    if positives:
        pos_dir = game_dir / "positive_batches"
        n_batches = create_batches(positives, pos_dir, "pos", draw_boxes=True)
        stats["positive_batches"] = n_batches
        logger.info("%s: created %d positive batches", game_id, n_batches)

    # Sample negatives
    negatives = sample_negatives(conn, game_id, positives, tiles_dir)
    stats["negatives_sampled"] = len(negatives)
    logger.info("%s: sampled %d negatives", game_id, len(negatives))

    # Create negative batches
    if negatives:
        neg_dir = game_dir / "negative_batches"
        n_batches = create_batches(negatives, neg_dir, "neg", draw_boxes=False)
        stats["negative_batches"] = n_batches
        logger.info("%s: created %d negative batches", game_id, n_batches)

    # Create phase frames
    phase_count = create_phase_frames(conn, game_id, tiles_dir, game_dir)
    stats["phase_frames"] = phase_count

    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Prepare composite images for label QA review"
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help="Database path (default: %(default)s)",
    )
    parser.add_argument(
        "--tiles",
        type=Path,
        default=DEFAULT_TILES_DIR,
        help="Tiles directory (default: %(default)s)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_DB_PATH.parent,
        help="Output directory (default: %(default)s)",
    )
    parser.add_argument(
        "--sample-rate",
        type=float,
        default=0.10,
        help="Positive sample rate (default: %(default)s)",
    )
    parser.add_argument("--games", nargs="+", help="Only prep specific games")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    conn = sqlite3.connect(str(args.db))

    # Get games to process
    if args.games:
        game_ids = args.games
    else:
        game_ids = [
            r[0]
            for r in conn.execute(
                "SELECT game_id FROM game_meta ORDER BY game_id"
            ).fetchall()
        ]

    logger.info("Preparing %d games: %s", len(game_ids), game_ids)

    all_stats = {}
    for game_id in game_ids:
        stats = prep_game(conn, game_id, args.tiles, args.output, args.sample_rate)
        all_stats[game_id] = stats

    # Write manifest
    manifest_path = args.output / "manifest.json"
    manifest_path.write_text(json.dumps(all_stats, indent=2))
    logger.info("Manifest written to %s", manifest_path)

    conn.close()


if __name__ == "__main__":
    main()
