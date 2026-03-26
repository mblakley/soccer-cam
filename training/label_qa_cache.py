"""Build SQLite cache indexing all ext labels for QA review.

Scans labels_640_ext/, parses filenames, reads label content, computes
panoramic coordinates, and stores everything in a queryable SQLite database.

Field mask polygons are applied separately after Sonnet agents define them.

Usage:
    uv run python -m training.label_qa_cache
    uv run python -m training.label_qa_cache --rebuild
    uv run python -m training.label_qa_cache --games heat__05.31.2024_vs_Fairport_home
    uv run python -m training.label_qa_cache apply-field-mask --game heat__05.31.2024_vs_Fairport_home
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

from training.data_prep.organize_dataset import parse_tile_filename
from training.data_prep.trajectory_validator import STEP_X, STEP_Y, _tile_to_pano

logger = logging.getLogger(__name__)

# Panoramic frame dimensions
PANO_W = 4096
PANO_H = 1800
N_COLS = 7
N_ROWS = 3

# Segment timestamp pattern: HH.MM.SS-HH.MM.SS[...]
_SEG_TIME_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{2})-(\d{2})\.(\d{2})\.(\d{2})")

DEFAULT_LABELS_DIR = Path("F:/training_data/labels_640_ext")
DEFAULT_TILES_DIR = Path("F:/training_data/tiles_640")
DEFAULT_DB_PATH = Path("F:/training_data/label_qa/tile_cache.db")


def parse_segment_timestamps(segment: str) -> tuple[str | None, str | None]:
    """Extract start/end timestamps from segment name.

    E.g., '18.01.30-18.18.20[F][0@0][189242]_ch1' -> ('18:01:30', '18:18:20')
    """
    m = _SEG_TIME_RE.match(segment)
    if m:
        start = f"{m.group(1)}:{m.group(2)}:{m.group(3)}"
        end = f"{m.group(4)}:{m.group(5)}:{m.group(6)}"
        return start, end
    return None, None


def create_db(db_path: Path, rebuild: bool = False) -> sqlite3.Connection:
    """Create or open the SQLite database with schema."""
    if rebuild and db_path.exists():
        db_path.unlink()
        logger.info("Removed existing database for rebuild")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS labels (
            id INTEGER PRIMARY KEY,
            game_id TEXT NOT NULL,
            segment TEXT NOT NULL,
            frame_idx INTEGER NOT NULL,
            row INTEGER NOT NULL,
            col INTEGER NOT NULL,
            is_positive INTEGER NOT NULL,
            cx REAL, cy REAL, w REAL, h REAL,
            pano_x REAL, pano_y REAL,
            tile_path TEXT NOT NULL,
            label_path TEXT NOT NULL,
            timestamp_start TEXT,
            timestamp_end TEXT,
            in_field INTEGER,
            game_phase TEXT,
            qa_verdict TEXT,
            qa_batch_id TEXT
        );

        CREATE TABLE IF NOT EXISTS game_meta (
            game_id TEXT PRIMARY KEY,
            field_mask_json TEXT,
            game_phases_json TEXT,
            total_labels INTEGER,
            total_positives INTEGER,
            positives_in_field INTEGER,
            qa_status TEXT DEFAULT 'pending'
        );

        CREATE INDEX IF NOT EXISTS idx_labels_game ON labels(game_id);
        CREATE INDEX IF NOT EXISTS idx_labels_frame ON labels(game_id, segment, frame_idx);
        CREATE INDEX IF NOT EXISTS idx_labels_positive ON labels(is_positive);
        CREATE INDEX IF NOT EXISTS idx_labels_in_field ON labels(in_field);
        CREATE INDEX IF NOT EXISTS idx_labels_verdict ON labels(qa_verdict);
    """)
    conn.commit()
    return conn


def reconstruct_panoramic(
    tiles_dir: Path, game_id: str, segment: str, frame_idx: int
) -> np.ndarray | None:
    """Stitch tiles back into a panoramic frame.

    Returns BGR image (PANO_H x PANO_W x 3) or None if insufficient tiles found.
    """
    pano = np.zeros((PANO_H, PANO_W, 3), dtype=np.uint8)
    tiles_found = 0

    for row in range(N_ROWS):
        for col in range(N_COLS):
            tile_name = f"{segment}_frame_{frame_idx:06d}_r{row}_c{col}.jpg"
            tile_path = tiles_dir / game_id / tile_name
            if not tile_path.exists():
                # Try .excluded extension (row 0 tiles)
                excluded_path = tile_path.with_suffix(".excluded")
                if excluded_path.exists():
                    continue
                continue

            tile = cv2.imread(str(tile_path))
            if tile is None:
                continue

            x_off = col * STEP_X
            y_off = row * STEP_Y
            h, w = tile.shape[:2]
            pano[y_off : y_off + h, x_off : x_off + w] = tile
            tiles_found += 1

    if tiles_found < 4:
        logger.warning(
            "Only %d tiles found for %s/%s/frame_%06d",
            tiles_found,
            game_id,
            segment,
            frame_idx,
        )
        return None

    return pano


def index_game(
    conn: sqlite3.Connection,
    labels_dir: Path,
    tiles_dir: Path,
    game_id: str,
) -> dict:
    """Index all label files for a single game into the database."""
    game_dir = labels_dir / game_id
    if not game_dir.is_dir():
        logger.warning("Game directory not found: %s", game_dir)
        return {}

    stats = {"total": 0, "positives": 0, "parse_errors": 0}
    batch = []

    for label_file in game_dir.iterdir():
        if label_file.suffix != ".txt":
            continue

        stem = label_file.stem
        parsed = parse_tile_filename(stem)
        if parsed is None:
            stats["parse_errors"] += 1
            continue

        segment, frame_idx, row, col = parsed
        ts_start, ts_end = parse_segment_timestamps(segment)

        # Compute deterministic tile path
        tile_path = str(
            tiles_dir / game_id / f"{segment}_frame_{frame_idx:06d}_r{row}_c{col}.jpg"
        )

        # Read label content
        content = label_file.read_text().strip()
        is_positive = 1 if content else 0
        cx = cy = w = h = pano_x = pano_y = None

        if is_positive:
            parts = content.split()
            if len(parts) >= 5:
                cx, cy, w, h = (
                    float(parts[1]),
                    float(parts[2]),
                    float(parts[3]),
                    float(parts[4]),
                )
                pano_x, pano_y = _tile_to_pano(cx, cy, row, col)

        batch.append(
            (
                game_id,
                segment,
                frame_idx,
                row,
                col,
                is_positive,
                cx,
                cy,
                w,
                h,
                pano_x,
                pano_y,
                tile_path,
                str(label_file),
                ts_start,
                ts_end,
            )
        )
        stats["total"] += 1
        stats["positives"] += is_positive

    # Bulk insert
    conn.executemany(
        """INSERT INTO labels
           (game_id, segment, frame_idx, row, col,
            is_positive, cx, cy, w, h, pano_x, pano_y,
            tile_path, label_path, timestamp_start, timestamp_end)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        batch,
    )

    # Store game metadata
    conn.execute(
        """INSERT OR REPLACE INTO game_meta
           (game_id, total_labels, total_positives, qa_status)
           VALUES (?, ?, ?, 'indexed')""",
        (game_id, stats["total"], stats["positives"]),
    )
    conn.commit()

    return stats


def find_mid_game_frame(labels_dir: Path, game_id: str) -> tuple[str, int] | None:
    """Find a segment and frame_idx near the middle of the game."""
    game_dir = labels_dir / game_id
    segments = set()

    for f in game_dir.iterdir():
        if f.suffix != ".txt":
            continue
        parsed = parse_tile_filename(f.stem)
        if parsed:
            segments.add(parsed[0])

    if not segments:
        return None

    sorted_segs = sorted(segments)
    mid_seg = sorted_segs[len(sorted_segs) // 2]

    frame_indices = set()
    for f in game_dir.iterdir():
        if f.suffix != ".txt":
            continue
        parsed = parse_tile_filename(f.stem)
        if parsed and parsed[0] == mid_seg:
            frame_indices.add(parsed[1])

    if not frame_indices:
        return None

    sorted_frames = sorted(frame_indices)
    mid_frame = sorted_frames[len(sorted_frames) // 2]
    return mid_seg, mid_frame


def apply_field_mask(
    conn: sqlite3.Connection,
    game_id: str,
    polygon: list[list[float]],
    margin: float = 50.0,
) -> dict:
    """Apply a field mask polygon to all labels for a game.

    Args:
        conn: Database connection
        game_id: Game to update
        polygon: List of [x, y] points in panoramic pixel coordinates
        margin: Extra margin in pixels outside polygon to still accept

    Returns:
        Stats dict with counts.
    """
    poly_array = np.array(polygon, dtype=np.float32).reshape(-1, 1, 2)
    polygon_json = json.dumps(polygon)

    # Get all positive labels for this game
    rows = conn.execute(
        "SELECT id, pano_x, pano_y FROM labels WHERE game_id = ? AND is_positive = 1",
        (game_id,),
    ).fetchall()

    in_field_count = 0
    updates = []
    for label_id, pano_x, pano_y in rows:
        if pano_x is None or pano_y is None:
            updates.append((None, label_id))
            continue
        dist = cv2.pointPolygonTest(poly_array, (pano_x, pano_y), measureDist=True)
        in_field = 1 if dist >= -margin else 0
        updates.append((in_field, label_id))
        in_field_count += in_field

    conn.executemany("UPDATE labels SET in_field = ? WHERE id = ?", updates)
    conn.execute(
        "UPDATE game_meta SET field_mask_json = ?, positives_in_field = ? WHERE game_id = ?",
        (polygon_json, in_field_count, game_id),
    )
    conn.commit()

    logger.info(
        "%s field mask: %d/%d positives in-field (margin=%dpx)",
        game_id,
        in_field_count,
        len(rows),
        margin,
    )
    return {"total": len(rows), "in_field": in_field_count}


def build_cache(
    labels_dir: Path,
    tiles_dir: Path,
    db_path: Path,
    rebuild: bool = False,
    games: list[str] | None = None,
) -> None:
    """Build the SQLite cache (index labels + save panoramics for agent review)."""
    conn = create_db(db_path, rebuild=rebuild)

    # Discover games
    game_dirs = sorted(
        d.name
        for d in labels_dir.iterdir()
        if d.is_dir() and (games is None or d.name in games)
    )
    logger.info("Found %d games to index: %s", len(game_dirs), game_dirs)

    total_start = time.time()

    for game_id in game_dirs:
        game_start = time.time()
        logger.info("=== Processing %s ===", game_id)

        # Check if already indexed (skip unless rebuild)
        if not rebuild:
            existing = conn.execute(
                "SELECT total_labels FROM game_meta WHERE game_id = ?", (game_id,)
            ).fetchone()
            if existing:
                logger.info(
                    "Skipping %s (already indexed: %d labels)", game_id, existing[0]
                )
                continue

        # Save a panoramic frame for Sonnet field mask + phase classification
        mid_frame_info = find_mid_game_frame(labels_dir, game_id)
        if mid_frame_info:
            segment, frame_idx = mid_frame_info
            logger.info("Reconstructing panoramic: %s frame %d", segment, frame_idx)
            pano = reconstruct_panoramic(tiles_dir, game_id, segment, frame_idx)
            if pano is not None:
                pano_dir = db_path.parent / game_id
                pano_dir.mkdir(parents=True, exist_ok=True)
                pano_path = pano_dir / "pano_sample.jpg"
                cv2.imwrite(str(pano_path), pano, [cv2.IMWRITE_JPEG_QUALITY, 85])
                logger.info("Saved panoramic sample: %s", pano_path)

        # Index all labels (no field mask yet — applied later by agents)
        stats = index_game(conn, labels_dir, tiles_dir, game_id)

        elapsed = time.time() - game_start
        logger.info(
            "%s: %d labels, %d positives (%.1fs)",
            game_id,
            stats.get("total", 0),
            stats.get("positives", 0),
            elapsed,
        )
        if stats.get("parse_errors", 0) > 0:
            logger.warning("  %d parse errors", stats["parse_errors"])

    # Print summary
    total_elapsed = time.time() - total_start
    summary = conn.execute("SELECT COUNT(*), SUM(is_positive) FROM labels").fetchone()
    logger.info(
        "\n=== Cache complete (%.1fs) ===\n"
        "  Total labels: %d\n"
        "  Positives: %d\n"
        "  Field mask: pending (run Sonnet agents, then apply-field-mask)",
        total_elapsed,
        summary[0] or 0,
        summary[1] or 0,
    )

    conn.close()


def cmd_apply_field_mask(args):
    """CLI handler for apply-field-mask subcommand."""
    conn = sqlite3.connect(str(args.db))
    mask_path = args.db.parent / args.game / "field_mask.json"

    if not mask_path.exists():
        logger.error("Field mask not found: %s", mask_path)
        return

    polygon = json.loads(mask_path.read_text())
    apply_field_mask(conn, args.game, polygon, margin=args.margin)
    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Build SQLite cache for label QA")
    subparsers = parser.add_subparsers(dest="command")

    # Default: build cache
    parser.add_argument(
        "--labels",
        type=Path,
        default=DEFAULT_LABELS_DIR,
        help="Labels directory (default: %(default)s)",
    )
    parser.add_argument(
        "--tiles",
        type=Path,
        default=DEFAULT_TILES_DIR,
        help="Tiles directory (default: %(default)s)",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help="Database path (default: %(default)s)",
    )
    parser.add_argument(
        "--rebuild", action="store_true", help="Drop and rebuild from scratch"
    )
    parser.add_argument("--games", nargs="+", help="Only index specific games")

    # Subcommand: apply-field-mask
    mask_parser = subparsers.add_parser(
        "apply-field-mask", help="Apply a field mask polygon to a game"
    )
    mask_parser.add_argument("--game", required=True, help="Game ID")
    mask_parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help="Database path (default: %(default)s)",
    )
    mask_parser.add_argument(
        "--margin",
        type=float,
        default=50.0,
        help="Margin in pixels outside polygon (default: 50)",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.command == "apply-field-mask":
        cmd_apply_field_mask(args)
    else:
        build_cache(
            labels_dir=args.labels,
            tiles_dir=args.tiles,
            db_path=args.db,
            rebuild=args.rebuild,
            games=args.games,
        )


if __name__ == "__main__":
    main()
