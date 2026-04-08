"""SQLite manifest — single source of truth for tiles and labels.

Schema hierarchy: games → segments → frames → tiles → labels
- tiles table: complete census of every .jpg tile on disk
- labels table: YOLO-format bounding boxes per tile
- segments/frames: pre-aggregated stats for fast verification queries

Usage:
    # Migrate existing .txt labels into manifest.db
    uv run python -m training.data_prep.manifest migrate
    uv run python -m training.data_prep.manifest migrate --games flash__2024.05.01_vs_RNYFC_away
    uv run python -m training.data_prep.manifest migrate --rebuild

    # Catalog tiles from disk into manifest (one-time scan per game)
    uv run python -m training.data_prep.manifest catalog
    uv run python -m training.data_prep.manifest catalog --games flash__2024.05.01_vs_RNYFC_away
    uv run python -m training.data_prep.manifest catalog --rescan

    # Show stats
    uv run python -m training.data_prep.manifest stats

    # Export labels back to .txt (for validation)
    uv run python -m training.data_prep.manifest export --game flash__2024.05.01_vs_RNYFC_away --out-dir /tmp/labels
"""

import argparse
import logging
import os
import random
import re
import shutil
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("D:/training_data/manifest.db")
DEFAULT_LABELS_DIR = Path("D:/training_data/labels_640_ext")
DEFAULT_TILES_DIR = Path("D:/training_data/tiles_640")

_TILE_RE = re.compile(r"^(.+)_frame_(\d{6})_r(\d+)_c(\d+)$")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS games (
    game_id TEXT PRIMARY KEY,
    tile_count INTEGER DEFAULT 0,
    labeled_count INTEGER DEFAULT 0,
    tile_dir TEXT,
    last_updated REAL,
    tiles_cataloged REAL
);

CREATE TABLE IF NOT EXISTS labels (
    id INTEGER PRIMARY KEY,
    game_id TEXT NOT NULL,
    tile_stem TEXT NOT NULL,
    class_id INTEGER DEFAULT 0,
    cx REAL NOT NULL,
    cy REAL NOT NULL,
    w REAL NOT NULL,
    h REAL NOT NULL,
    source TEXT,
    confidence REAL,
    FOREIGN KEY (game_id) REFERENCES games(game_id)
);

CREATE INDEX IF NOT EXISTS idx_labels_game ON labels(game_id);
CREATE INDEX IF NOT EXISTS idx_labels_stem ON labels(tile_stem);
CREATE UNIQUE INDEX IF NOT EXISTS idx_labels_unique
    ON labels(game_id, tile_stem, class_id, cx, cy);

CREATE TABLE IF NOT EXISTS segments (
    game_id TEXT NOT NULL,
    segment TEXT NOT NULL,
    frame_count INTEGER DEFAULT 0,
    tile_count INTEGER DEFAULT 0,
    frame_min INTEGER,
    frame_max INTEGER,
    max_gap INTEGER DEFAULT 0,
    PRIMARY KEY (game_id, segment),
    FOREIGN KEY (game_id) REFERENCES games(game_id)
);

CREATE TABLE IF NOT EXISTS frames (
    game_id TEXT NOT NULL,
    segment TEXT NOT NULL,
    frame_idx INTEGER NOT NULL,
    tile_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (game_id, segment, frame_idx),
    FOREIGN KEY (game_id, segment) REFERENCES segments(game_id, segment)
);

CREATE TABLE IF NOT EXISTS tiles (
    game_id TEXT NOT NULL,
    segment TEXT NOT NULL,
    frame_idx INTEGER NOT NULL,
    row INTEGER NOT NULL,
    col INTEGER NOT NULL,
    pack_file TEXT,
    pack_offset INTEGER,
    pack_size INTEGER,
    PRIMARY KEY (game_id, segment, frame_idx, row, col),
    FOREIGN KEY (game_id, segment, frame_idx) REFERENCES frames(game_id, segment, frame_idx)
);

CREATE INDEX IF NOT EXISTS idx_frames_game ON frames(game_id);
CREATE INDEX IF NOT EXISTS idx_tiles_game ON tiles(game_id);
"""


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------


def _ensure_schema_v2(conn: sqlite3.Connection) -> None:
    """Add segments/frames/tiles tables and new columns if missing.

    Purely additive — safe to run on existing databases. Idempotent.
    """
    # New tables use CREATE IF NOT EXISTS in SCHEMA_SQL, so just re-run it
    conn.executescript(SCHEMA_SQL)
    # Add new columns to existing tables (each wrapped in try/except for idempotency)
    for alter in [
        "ALTER TABLE games ADD COLUMN tiles_cataloged REAL",
        "ALTER TABLE tiles ADD COLUMN pack_file TEXT",
        "ALTER TABLE tiles ADD COLUMN pack_offset INTEGER",
        "ALTER TABLE tiles ADD COLUMN pack_size INTEGER",
    ]:
        try:
            conn.execute(alter)
        except sqlite3.OperationalError:
            pass  # Column already exists
    conn.commit()


def open_db(
    db_path: Path = DEFAULT_DB_PATH, *, create: bool = False
) -> sqlite3.Connection:
    """Open manifest database. Creates/upgrades schema as needed."""
    if not create and not db_path.exists():
        raise FileNotFoundError(f"Manifest database not found: {db_path}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    if create:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    _ensure_schema_v2(conn)
    return conn


def reset_db(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Drop and recreate the database."""
    if db_path.exists():
        db_path.unlink()
    return open_db(db_path, create=True)


# ---------------------------------------------------------------------------
# CRUD — labels
# ---------------------------------------------------------------------------


def upsert_label(
    conn: sqlite3.Connection,
    game_id: str,
    tile_stem: str,
    class_id: int,
    cx: float,
    cy: float,
    w: float,
    h: float,
    source: str | None = None,
    confidence: float | None = None,
) -> None:
    """Insert or update a single label (upsert on unique constraint)."""
    conn.execute(
        """INSERT INTO labels (game_id, tile_stem, class_id, cx, cy, w, h, source, confidence)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(game_id, tile_stem, class_id, cx, cy)
           DO UPDATE SET w=excluded.w, h=excluded.h,
                         source=excluded.source, confidence=excluded.confidence""",
        (game_id, tile_stem, class_id, cx, cy, w, h, source, confidence),
    )


def bulk_insert_labels(
    conn: sqlite3.Connection,
    rows: list[tuple],
) -> int:
    """Bulk insert label rows: (game_id, tile_stem, class_id, cx, cy, w, h, source, confidence).

    Uses INSERT OR IGNORE to skip duplicates. Returns number inserted.
    """
    cursor = conn.executemany(
        """INSERT OR IGNORE INTO labels
           (game_id, tile_stem, class_id, cx, cy, w, h, source, confidence)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    return cursor.rowcount


def get_labels_for_tile(
    conn: sqlite3.Connection,
    game_id: str,
    tile_stem: str,
) -> list[tuple[int, float, float, float, float]]:
    """Return list of (class_id, cx, cy, w, h) for a tile."""
    rows = conn.execute(
        "SELECT class_id, cx, cy, w, h FROM labels WHERE game_id = ? AND tile_stem = ?",
        (game_id, tile_stem),
    ).fetchall()
    return rows


def get_labels_for_game(
    conn: sqlite3.Connection,
    game_id: str,
) -> list[tuple[str, int, float, float, float, float]]:
    """Return all labels for a game as (tile_stem, class_id, cx, cy, w, h)."""
    return conn.execute(
        "SELECT tile_stem, class_id, cx, cy, w, h FROM labels WHERE game_id = ?",
        (game_id,),
    ).fetchall()


def get_labeled_stems(conn: sqlite3.Connection, game_id: str) -> set[str]:
    """Return set of tile_stems that have at least one label in a game."""
    rows = conn.execute(
        "SELECT DISTINCT tile_stem FROM labels WHERE game_id = ?",
        (game_id,),
    ).fetchall()
    return {r[0] for r in rows}


def delete_labels_for_game(conn: sqlite3.Connection, game_id: str) -> int:
    """Delete all labels for a game. Returns count deleted."""
    cursor = conn.execute("DELETE FROM labels WHERE game_id = ?", (game_id,))
    conn.execute("DELETE FROM games WHERE game_id = ?", (game_id,))
    conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# CRUD — games
# ---------------------------------------------------------------------------


def upsert_game(
    conn: sqlite3.Connection,
    game_id: str,
    tile_dir: str | None = None,
    tile_count: int | None = None,
    labeled_count: int | None = None,
) -> None:
    """Insert or update game metadata."""
    conn.execute(
        """INSERT INTO games (game_id, tile_dir, tile_count, labeled_count, last_updated)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(game_id)
           DO UPDATE SET tile_dir=COALESCE(excluded.tile_dir, tile_dir),
                         tile_count=COALESCE(excluded.tile_count, tile_count),
                         labeled_count=COALESCE(excluded.labeled_count, labeled_count),
                         last_updated=excluded.last_updated""",
        (game_id, tile_dir, tile_count, labeled_count, time.time()),
    )


def get_game(conn: sqlite3.Connection, game_id: str) -> dict | None:
    """Return game metadata as dict, or None."""
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM games WHERE game_id = ?", (game_id,)).fetchone()
    conn.row_factory = None
    return dict(row) if row else None


def list_games(conn: sqlite3.Connection) -> list[dict]:
    """Return all games with their metadata."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM games ORDER BY game_id").fetchall()
    conn.row_factory = None
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Tile catalog — census of every .jpg tile on disk
# ---------------------------------------------------------------------------


def is_game_cataloged(conn: sqlite3.Connection, game_id: str) -> bool:
    """Check if a game's tiles have been cataloged into the manifest."""
    row = conn.execute(
        "SELECT tiles_cataloged FROM games WHERE game_id = ?", (game_id,)
    ).fetchone()
    return row is not None and row[0] is not None


def catalog_game_tiles(
    conn: sqlite3.Connection,
    game_id: str,
    tile_dir: Path,
) -> dict:
    """Scan a tile directory and insert every tile into the manifest.

    This is a complete census: every .jpg file gets a row in the tiles table.
    Existing data for this game is deleted first (idempotent rescan).
    Segments and frames tables are populated with pre-aggregated stats.

    Returns: {segments, frames, tiles, unparsed, elapsed}
    """
    t0 = time.time()
    tile_dir = Path(tile_dir)

    # Delete existing catalog for this game (within transaction)
    conn.execute("DELETE FROM tiles WHERE game_id = ?", (game_id,))
    conn.execute("DELETE FROM frames WHERE game_id = ?", (game_id,))
    conn.execute("DELETE FROM segments WHERE game_id = ?", (game_id,))

    # Scan directory — every .jpg is a tile
    filenames = [f for f in os.listdir(tile_dir) if f.endswith(".jpg")]

    # Parse filenames and collect tile rows
    # seg_frames: segment -> frame_idx -> set of (row, col)
    from collections import defaultdict

    seg_frames: dict[str, dict[int, set[tuple[int, int]]]] = defaultdict(
        lambda: defaultdict(set)
    )
    tile_rows = []
    unparsed = 0

    for fname in filenames:
        stem = fname[:-4]  # strip .jpg
        m = _TILE_RE.match(stem)
        if not m:
            unparsed += 1
            continue
        segment, frame_idx = m.group(1), int(m.group(2))
        row, col = int(m.group(3)), int(m.group(4))
        tile_rows.append((game_id, segment, frame_idx, row, col))
        seg_frames[segment][frame_idx].add((row, col))

    # Insert in FK order: game → segments → frames → tiles
    total_tiles = len(tile_rows)

    # 1. Game (parent for segments FK)
    conn.execute(
        """INSERT INTO games (game_id, tile_dir, tile_count, tiles_cataloged, last_updated)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(game_id)
           DO UPDATE SET tile_dir=excluded.tile_dir,
                         tile_count=excluded.tile_count,
                         tiles_cataloged=excluded.tiles_cataloged,
                         last_updated=excluded.last_updated""",
        (game_id, str(tile_dir), total_tiles, time.time(), time.time()),
    )

    # 2. Segments (parent for frames FK)
    for segment in sorted(seg_frames):
        frames_dict = seg_frames[segment]
        frame_indices = sorted(frames_dict.keys())
        frame_count = len(frame_indices)
        tile_count = sum(len(tiles) for tiles in frames_dict.values())
        frame_min = frame_indices[0] if frame_indices else 0
        frame_max = frame_indices[-1] if frame_indices else 0

        max_gap = 0
        if len(frame_indices) > 1:
            max_gap = max(
                frame_indices[i + 1] - frame_indices[i]
                for i in range(len(frame_indices) - 1)
            )

        conn.execute(
            "INSERT INTO segments (game_id, segment, frame_count, tile_count, "
            "frame_min, frame_max, max_gap) VALUES (?,?,?,?,?,?,?)",
            (game_id, segment, frame_count, tile_count, frame_min, frame_max, max_gap),
        )

    # 3. Frames (parent for tiles FK)
    frame_rows = []
    for segment in sorted(seg_frames):
        for frame_idx in sorted(seg_frames[segment]):
            tile_count = len(seg_frames[segment][frame_idx])
            frame_rows.append((game_id, segment, frame_idx, tile_count))

    if frame_rows:
        conn.executemany(
            "INSERT INTO frames (game_id, segment, frame_idx, tile_count) VALUES (?,?,?,?)",
            frame_rows,
        )

    # 4. Tiles
    if tile_rows:
        conn.executemany(
            "INSERT INTO tiles (game_id, segment, frame_idx, row, col) VALUES (?,?,?,?,?)",
            tile_rows,
        )

    conn.commit()

    elapsed = time.time() - t0
    return {
        "segments": len(seg_frames),
        "frames": len(frame_rows),
        "tiles": total_tiles,
        "unparsed": unparsed,
        "elapsed": elapsed,
    }


def get_segments_for_game(conn: sqlite3.Connection, game_id: str) -> list[str]:
    """Return distinct segment stems cataloged for a game."""
    rows = conn.execute(
        "SELECT segment FROM segments WHERE game_id = ? ORDER BY segment",
        (game_id,),
    ).fetchall()
    return [r[0] for r in rows]


def get_frame_summary(conn: sqlite3.Connection, game_id: str) -> list[dict]:
    """Return per-segment summary from pre-aggregated segments table.

    Each dict: {segment, frame_count, tile_count, frame_min, frame_max, max_gap}
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT segment, frame_count, tile_count, frame_min, frame_max, max_gap "
        "FROM segments WHERE game_id = ? ORDER BY segment",
        (game_id,),
    ).fetchall()
    conn.row_factory = None
    return [dict(r) for r in rows]


def get_incomplete_frames(
    conn: sqlite3.Connection, game_id: str, expected_tiles: int = 21
) -> list[tuple[str, int, int]]:
    """Return (segment, frame_idx, tile_count) for frames with fewer than expected tiles."""
    return conn.execute(
        "SELECT segment, frame_idx, tile_count FROM frames "
        "WHERE game_id = ? AND tile_count < ? ORDER BY segment, frame_idx",
        (game_id, expected_tiles),
    ).fetchall()


def get_frame_indices(
    conn: sqlite3.Connection, game_id: str, segment: str
) -> list[int]:
    """Return sorted frame indices for a segment (for gap detection)."""
    rows = conn.execute(
        "SELECT frame_idx FROM frames WHERE game_id = ? AND segment = ? ORDER BY frame_idx",
        (game_id, segment),
    ).fetchall()
    return [r[0] for r in rows]


def get_missing_tiles_for_frame(
    conn: sqlite3.Connection,
    game_id: str,
    segment: str,
    frame_idx: int,
    expected_rows: int = 3,
    expected_cols: int = 7,
) -> list[tuple[int, int]]:
    """Return list of (row, col) that are missing from a specific frame."""
    existing = set(
        conn.execute(
            "SELECT row, col FROM tiles "
            "WHERE game_id = ? AND segment = ? AND frame_idx = ?",
            (game_id, segment, frame_idx),
        ).fetchall()
    )
    expected = {(r, c) for r in range(expected_rows) for c in range(expected_cols)}
    return sorted(expected - existing)


def tile_exists(
    conn: sqlite3.Connection,
    game_id: str,
    segment: str,
    frame_idx: int,
    row: int,
    col: int,
) -> bool:
    """Check if a specific tile exists in the manifest."""
    row_result = conn.execute(
        "SELECT 1 FROM tiles WHERE game_id=? AND segment=? AND frame_idx=? AND row=? AND col=?",
        (game_id, segment, frame_idx, row, col),
    ).fetchone()
    return row_result is not None


def catalog_all_games(
    conn: sqlite3.Connection,
    tiles_dir: Path = DEFAULT_TILES_DIR,
    rescan: bool = False,
    games: list[str] | None = None,
) -> None:
    """Catalog tiles for all game directories found in tiles_dir."""
    game_dirs = sorted(
        d.name
        for d in tiles_dir.iterdir()
        if d.is_dir() and (games is None or d.name in games)
    )
    logger.info("Cataloging %d games from %s", len(game_dirs), tiles_dir)

    for game_id in game_dirs:
        if not rescan and is_game_cataloged(conn, game_id):
            logger.info("  %s: already cataloged, skipping", game_id)
            continue

        game_dir = tiles_dir / game_id
        logger.info("  %s: scanning...", game_id)
        stats = catalog_game_tiles(conn, game_id, game_dir)
        logger.info(
            "  %s: %d segments, %d frames, %d tiles (%.1fs)%s",
            game_id,
            stats["segments"],
            stats["frames"],
            stats["tiles"],
            stats["elapsed"],
            f" ({stats['unparsed']} unparsed)" if stats["unparsed"] else "",
        )


# ---------------------------------------------------------------------------
# Pack files — segment-level binary archives for fast tile reads
# ---------------------------------------------------------------------------

DEFAULT_PACK_DIR = Path("D:/training_data/tile_packs")


def pack_segment(
    conn: sqlite3.Connection,
    game_id: str,
    segment: str,
    tiles_dir: Path = DEFAULT_TILES_DIR,
    pack_dir: Path = DEFAULT_PACK_DIR,
    delete_loose: bool = False,
    source_override: Path | None = None,
) -> dict:
    """Pack all tiles for one segment into a single .pack file.

    Reads loose JPEGs from tiles_dir (or source_override if provided),
    concatenates them into {pack_dir}/{game_id}/{segment}.pack, and updates
    the tiles table with (pack_file, pack_offset, pack_size) for each tile.

    If delete_loose=True, removes loose .jpg files from tiles_dir (the
    original location, not source_override).

    Returns: {tiles_packed, pack_size, loose_deleted, elapsed}
    """
    t0 = time.time()
    game_tile_dir = source_override if source_override else (tiles_dir / game_id)
    out_dir = pack_dir / game_id
    out_dir.mkdir(parents=True, exist_ok=True)
    pack_path = out_dir / f"{segment}.pack"

    # Get all tiles for this segment from manifest (sorted for deterministic packing)
    rows = conn.execute(
        "SELECT frame_idx, row, col FROM tiles "
        "WHERE game_id = ? AND segment = ? ORDER BY frame_idx, row, col",
        (game_id, segment),
    ).fetchall()

    updates = []
    source_paths = []
    source_sizes_sum = 0
    offset = 0
    pack_path_str = str(pack_path)

    # Build file list upfront
    file_list = []
    for frame_idx, row, col in rows:
        stem = f"{segment}_frame_{frame_idx:06d}_r{row}_c{col}"
        src = game_tile_dir / f"{stem}.jpg"
        file_list.append((src, frame_idx, row, col))

    # Read files concurrently with a thread pool for better HDD scheduling,
    # but write sequentially to maintain deterministic pack order.
    from concurrent.futures import ThreadPoolExecutor, as_completed

    READAHEAD = 32  # number of concurrent file reads

    def _read_file(item):
        src, fidx, r, c = item
        return (fidx, r, c, src, src.read_bytes())

    with open(pack_path, "wb") as pf:
        # Process in batches to keep memory bounded
        for batch_start in range(0, len(file_list), READAHEAD * 4):
            batch = file_list[batch_start : batch_start + READAHEAD * 4]
            # Read batch concurrently
            results = {}
            with ThreadPoolExecutor(max_workers=READAHEAD) as pool:
                futures = {
                    pool.submit(_read_file, item): i for i, item in enumerate(batch)
                }
                for future in as_completed(futures):
                    idx = futures[future]
                    results[idx] = future.result()

            # Write in order
            for i in range(len(batch)):
                fidx, r, c, src, data = results[i]
                pf.write(data)
                size = len(data)
                source_sizes_sum += size
                source_paths.append(src)
                updates.append(
                    (pack_path_str, offset, size, game_id, segment, fidx, r, c)
                )
                offset += size

    # Verify pack file size matches sum of source files
    actual_size = os.path.getsize(pack_path)
    if actual_size != source_sizes_sum:
        raise RuntimeError(
            f"Pack verification failed for {game_id}/{segment}: "
            f"expected {source_sizes_sum} bytes, got {actual_size}"
        )

    # Batch update tiles table with pack info
    conn.executemany(
        "UPDATE tiles SET pack_file=?, pack_offset=?, pack_size=? "
        "WHERE game_id=? AND segment=? AND frame_idx=? AND row=? AND col=?",
        updates,
    )
    conn.commit()

    # Delete loose files after successful pack + DB update
    deleted = 0
    if delete_loose:
        for src in source_paths:
            src.unlink()
            deleted += 1

    elapsed = time.time() - t0
    return {
        "tiles_packed": len(rows),
        "pack_size": offset,
        "loose_deleted": deleted,
        "elapsed": elapsed,
    }


def pack_game(
    conn: sqlite3.Connection,
    game_id: str,
    tiles_dir: Path = DEFAULT_TILES_DIR,
    pack_dir: Path = DEFAULT_PACK_DIR,
    delete_loose: bool = False,
    ssd_staging: Path | None = None,
) -> dict:
    """Pack all segments for a game. Returns aggregate stats.

    If ssd_staging is provided, copies tiles to SSD first for faster reads,
    packs from there, then cleans up the SSD copy.
    """
    segments = get_segments_for_game(conn, game_id)
    total = {
        "tiles_packed": 0,
        "pack_size": 0,
        "loose_deleted": 0,
        "elapsed": 0.0,
        "segments": 0,
    }

    source_dir = None
    if ssd_staging:
        import shutil
        import subprocess

        ssd_game_dir = ssd_staging / game_id
        hdd_game_dir = tiles_dir / game_id
        if hdd_game_dir.exists():
            logger.info("    Staging %s to SSD (%s)...", game_id, ssd_staging)
            t_stage = time.time()
            # robocopy is fastest for bulk file copy on Windows
            result = subprocess.run(
                [
                    "robocopy",
                    str(hdd_game_dir),
                    str(ssd_game_dir),
                    "*.jpg",
                    "/E",
                    "/J",
                    "/MT:4",
                    "/R:1",
                    "/W:1",
                    "/NP",
                    "/NFL",
                    "/NDL",
                ],
                capture_output=True,
                text=True,
            )
            stage_elapsed = time.time() - t_stage
            n_files = (
                sum(1 for _ in ssd_game_dir.glob("*.jpg"))
                if ssd_game_dir.exists()
                else 0
            )
            logger.info("    Staged %d files to SSD in %.0fs", n_files, stage_elapsed)
            source_dir = ssd_game_dir

    for segment in segments:
        stats = pack_segment(
            conn,
            game_id,
            segment,
            tiles_dir,
            pack_dir,
            delete_loose=delete_loose,
            source_override=source_dir,
        )
        total["tiles_packed"] += stats["tiles_packed"]
        total["pack_size"] += stats["pack_size"]
        total["loose_deleted"] += stats["loose_deleted"]
        total["elapsed"] += stats["elapsed"]
        total["segments"] += 1
        deleted_info = f", {stats['loose_deleted']} deleted" if delete_loose else ""
        logger.info(
            "    %s: %d tiles, %.1fMB (%.1fs)%s",
            segment,
            stats["tiles_packed"],
            stats["pack_size"] / 1024 / 1024,
            stats["elapsed"],
            deleted_info,
        )

    # Clean up SSD staging
    if source_dir and source_dir.exists():
        import shutil

        shutil.rmtree(source_dir, ignore_errors=True)
        logger.info("    Cleaned SSD staging for %s", game_id)

    return total


def pack_all_games(
    conn: sqlite3.Connection,
    tiles_dir: Path = DEFAULT_TILES_DIR,
    pack_dir: Path = DEFAULT_PACK_DIR,
    games: list[str] | None = None,
    delete_loose: bool = False,
    ssd_staging: Path | None = None,
) -> None:
    """Pack all cataloged games into segment pack files.

    If delete_loose=True, removes loose .jpg files after packing each segment.
    If ssd_staging is set, copies tiles to SSD before packing for faster reads.
    """
    game_rows = conn.execute(
        "SELECT DISTINCT game_id FROM segments ORDER BY game_id"
    ).fetchall()
    game_ids = [r[0] for r in game_rows]
    if games:
        game_ids = [g for g in game_ids if g in games]

    logger.info(
        "Packing %d games into %s (delete_loose=%s, ssd=%s)",
        len(game_ids),
        pack_dir,
        delete_loose,
        ssd_staging,
    )

    for game_id in game_ids:
        # Check if already packed
        row = conn.execute(
            "SELECT COUNT(*) FROM tiles WHERE game_id = ? AND pack_file IS NULL",
            (game_id,),
        ).fetchone()
        if row[0] == 0:
            logger.info("  %s: already packed, skipping", game_id)
            continue

        logger.info("  %s: packing...", game_id)
        stats = pack_game(
            conn,
            game_id,
            tiles_dir,
            pack_dir,
            delete_loose=delete_loose,
            ssd_staging=ssd_staging,
        )
        logger.info(
            "  %s: %d segments, %d tiles, %.1fMB (%.1fs)",
            game_id,
            stats["segments"],
            stats["tiles_packed"],
            stats["pack_size"] / 1024 / 1024,
            stats["elapsed"],
        )


def read_tile_bytes(
    conn: sqlite3.Connection,
    game_id: str,
    segment: str,
    frame_idx: int,
    row: int,
    col: int,
) -> bytes:
    """Read a single tile's JPEG bytes from its pack file.

    Falls back to reading the loose file if pack info is not available.
    """
    result = conn.execute(
        "SELECT pack_file, pack_offset, pack_size FROM tiles "
        "WHERE game_id=? AND segment=? AND frame_idx=? AND row=? AND col=?",
        (game_id, segment, frame_idx, row, col),
    ).fetchone()

    if result is None:
        raise FileNotFoundError(
            f"Tile not in manifest: {game_id}/{segment}_frame_{frame_idx:06d}_r{row}_c{col}"
        )

    pack_file, pack_offset, pack_size = result
    if pack_file and pack_offset is not None:
        with open(pack_file, "rb") as f:
            f.seek(pack_offset)
            return f.read(pack_size)

    # Fallback: read loose file
    game_meta = get_game(conn, game_id)
    if not game_meta or not game_meta.get("tile_dir"):
        raise FileNotFoundError(f"No tile_dir for game {game_id}")
    stem = f"{segment}_frame_{frame_idx:06d}_r{row}_c{col}"
    path = Path(game_meta["tile_dir"]) / f"{stem}.jpg"
    return path.read_bytes()


def read_tiles_batch(
    conn: sqlite3.Connection,
    game_id: str,
    tile_keys: list[tuple[str, int, int, int]],
) -> list[tuple[tuple[str, int, int, int], bytes]]:
    """Read multiple tiles efficiently, sorted by pack offset for sequential HDD reads.

    tile_keys: list of (segment, frame_idx, row, col)
    Returns: list of ((segment, frame_idx, row, col), jpeg_bytes)
    """
    if not tile_keys:
        return []

    # Batch lookup
    results = []
    for seg, fidx, r, c in tile_keys:
        row = conn.execute(
            "SELECT pack_file, pack_offset, pack_size FROM tiles "
            "WHERE game_id=? AND segment=? AND frame_idx=? AND row=? AND col=?",
            (game_id, seg, fidx, r, c),
        ).fetchone()
        if row:
            results.append(((seg, fidx, r, c), row[0], row[1], row[2]))

    # Sort by pack_file then offset for sequential reads
    results.sort(key=lambda x: (x[1] or "", x[2] or 0))

    output = []
    current_fh = None
    current_path = None
    try:
        for key, pack_file, pack_offset, pack_size in results:
            if pack_file and pack_offset is not None:
                if pack_file != current_path:
                    if current_fh:
                        current_fh.close()
                    current_fh = open(pack_file, "rb")
                    current_path = pack_file
                current_fh.seek(pack_offset)
                data = current_fh.read(pack_size)
            else:
                # Fallback to loose file
                game_meta = get_game(conn, game_id)
                seg, fidx, r, c = key
                stem = f"{seg}_frame_{fidx:06d}_r{r}_c{c}"
                path = Path(game_meta["tile_dir"]) / f"{stem}.jpg"
                data = path.read_bytes()
            output.append((key, data))
    finally:
        if current_fh:
            current_fh.close()

    return output


# ---------------------------------------------------------------------------
# CRUD — query helpers for dataset building
# ---------------------------------------------------------------------------


def generate_train_list(
    conn: sqlite3.Connection,
    game_ids: list[str] | None = None,
    tiles_dir: Path = DEFAULT_TILES_DIR,
) -> list[str]:
    """Generate list of absolute image paths for labeled tiles.

    This is what goes into train.txt / val.txt for YOLO.
    """
    if game_ids:
        placeholders = ",".join("?" * len(game_ids))
        stems = conn.execute(
            f"SELECT DISTINCT game_id, tile_stem FROM labels WHERE game_id IN ({placeholders})",
            game_ids,
        ).fetchall()
    else:
        stems = conn.execute(
            "SELECT DISTINCT game_id, tile_stem FROM labels"
        ).fetchall()

    paths = []
    for game_id, tile_stem in stems:
        img_path = tiles_dir / game_id / f"{tile_stem}.jpg"
        paths.append(str(img_path))
    return paths


def export_labels_to_txt(
    conn: sqlite3.Connection,
    game_id: str,
    out_dir: Path,
) -> int:
    """Write YOLO .txt label files from manifest. Returns count of files written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    labels = get_labels_for_game(conn, game_id)

    # Group by tile_stem
    by_stem: dict[str, list[tuple[int, float, float, float, float]]] = {}
    for tile_stem, class_id, cx, cy, w, h in labels:
        by_stem.setdefault(tile_stem, []).append((class_id, cx, cy, w, h))

    for tile_stem, detections in by_stem.items():
        lines = [
            f"{c} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}" for c, cx, cy, w, h in detections
        ]
        (out_dir / f"{tile_stem}.txt").write_text("\n".join(lines) + "\n")

    return len(by_stem)


# ---------------------------------------------------------------------------
# Migration — ingest existing .txt label files
# ---------------------------------------------------------------------------


def _parse_label_file(path: Path) -> list[tuple[int, float, float, float, float]]:
    """Parse a YOLO .txt label file into list of (class_id, cx, cy, w, h)."""
    content = path.read_text().strip()
    if not content:
        return []
    detections = []
    for line in content.splitlines():
        parts = line.split()
        if len(parts) >= 5:
            detections.append(
                (
                    int(parts[0]),
                    float(parts[1]),
                    float(parts[2]),
                    float(parts[3]),
                    float(parts[4]),
                )
            )
    return detections


def migrate_game(
    conn: sqlite3.Connection,
    labels_dir: Path,
    tiles_dir: Path,
    game_id: str,
    source: str = "migrated",
) -> dict:
    """Migrate all .txt labels for one game into the manifest.

    Returns stats dict: {total_files, positive_files, labels_inserted, skipped}.
    """
    game_dir = labels_dir / game_id
    if not game_dir.is_dir():
        logger.warning("Label directory not found: %s", game_dir)
        return {
            "total_files": 0,
            "positive_files": 0,
            "labels_inserted": 0,
            "skipped": 0,
        }

    stats = {"total_files": 0, "positive_files": 0, "labels_inserted": 0, "skipped": 0}
    batch = []

    for label_file in game_dir.iterdir():
        if label_file.suffix != ".txt":
            continue
        stats["total_files"] += 1
        stem = label_file.stem

        # Validate filename matches tile pattern
        if not _TILE_RE.match(stem):
            stats["skipped"] += 1
            continue

        detections = _parse_label_file(label_file)
        if not detections:
            continue  # empty label = negative tile, nothing to store

        stats["positive_files"] += 1
        for class_id, cx, cy, w, h in detections:
            batch.append((game_id, stem, class_id, cx, cy, w, h, source, None))

    # Ensure game row exists before inserting labels (foreign key)
    tile_dir = tiles_dir / game_id
    upsert_game(
        conn,
        game_id,
        tile_dir=str(tile_dir),
        labeled_count=stats["positive_files"],
    )

    # Bulk insert labels
    inserted = bulk_insert_labels(conn, batch)
    stats["labels_inserted"] = inserted
    conn.commit()

    return stats


def migrate_all(
    labels_dir: Path = DEFAULT_LABELS_DIR,
    tiles_dir: Path = DEFAULT_TILES_DIR,
    db_path: Path = DEFAULT_DB_PATH,
    rebuild: bool = False,
    games: list[str] | None = None,
) -> None:
    """Migrate all games from .txt labels into manifest.db."""
    conn = reset_db(db_path) if rebuild else open_db(db_path, create=True)

    # Discover games
    game_dirs = sorted(
        d.name
        for d in labels_dir.iterdir()
        if d.is_dir() and (games is None or d.name in games)
    )
    logger.info("Migrating %d games from %s", len(game_dirs), labels_dir)

    total_start = time.time()
    grand = {"total_files": 0, "positive_files": 0, "labels_inserted": 0}

    for game_id in game_dirs:
        # Skip if already migrated (unless rebuild)
        if not rebuild:
            existing = get_game(conn, game_id)
            if existing and existing["labeled_count"] and existing["labeled_count"] > 0:
                logger.info(
                    "Skipping %s (already migrated: %d labeled)",
                    game_id,
                    existing["labeled_count"],
                )
                continue

        t0 = time.time()
        stats = migrate_game(conn, labels_dir, tiles_dir, game_id)
        elapsed = time.time() - t0

        for k in grand:
            grand[k] += stats.get(k, 0)

        logger.info(
            "  %s: %d files, %d positive, %d labels inserted (%.1fs)%s",
            game_id,
            stats["total_files"],
            stats["positive_files"],
            stats["labels_inserted"],
            elapsed,
            f" ({stats['skipped']} skipped)" if stats.get("skipped") else "",
        )

    total_elapsed = time.time() - total_start
    logger.info(
        "\nMigration complete (%.1fs): %d files scanned, %d positive, %d labels inserted",
        total_elapsed,
        grand["total_files"],
        grand["positive_files"],
        grand["labels_inserted"],
    )
    conn.close()


# ---------------------------------------------------------------------------
# Dataset building
# ---------------------------------------------------------------------------

# Tile position pattern: _r{row}_c{col} at end of stem
_TILE_POS_RE = re.compile(r"_r(\d+)_c(\d+)$")

DEFAULT_EXCLUDE_ROWS = {0}
DEFAULT_TILE_WEIGHTS = {
    (1, 0): 3,
    (1, 1): 2,
    (1, 2): 2,
    (1, 3): 2,
    (1, 4): 2,
    (1, 5): 2,
    (1, 6): 3,
    (2, 0): 1,
    (2, 1): 2,
    (2, 2): 2,
    (2, 3): 2,
    (2, 4): 2,
    (2, 5): 2,
    (2, 6): 1,
}
DEFAULT_VAL_SPLIT = 0.15
DEFAULT_NEG_RATIO = 1.0
ROW_2_OVERSAMPLE = 2


def _parse_tile_position(stem: str) -> tuple[int, int] | None:
    m = _TILE_POS_RE.search(stem)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _link_or_copy(src: Path, dst: Path):
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _find_hard_negatives(
    positive_stems: set[str],
    all_stems: set[str],
) -> set[str]:
    """Find tiles spatially/temporally adjacent to positive tiles."""
    hard = set()
    for stem in positive_stems:
        parts = stem.split("/", 1)
        if len(parts) != 2:
            continue
        game_id, tile_stem = parts
        m = _TILE_RE.match(tile_stem)
        if not m:
            continue
        segment, frame_idx = m.group(1), int(m.group(2))
        row, col = int(m.group(3)), int(m.group(4))

        # Spatial neighbors (same frame, adjacent tiles)
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                nr, nc = row + dr, col + dc
                if nr < 0 or nc < 0:
                    continue
                neighbor = f"{game_id}/{segment}_frame_{frame_idx:06d}_r{nr}_c{nc}"
                if neighbor in all_stems and neighbor not in positive_stems:
                    hard.add(neighbor)

        # Temporal neighbors (same tile, ±2 extraction intervals = ±8 frames)
        for df in (-8, 8):
            nf = frame_idx + df
            if nf < 0:
                continue
            neighbor = f"{game_id}/{segment}_frame_{nf:06d}_r{row}_c{col}"
            if neighbor in all_stems and neighbor not in positive_stems:
                hard.add(neighbor)

    return hard


def build_dataset(
    conn: sqlite3.Connection,
    tiles_dir: Path,
    output_dir: Path,
    val_split: float = DEFAULT_VAL_SPLIT,
    neg_ratio: float = DEFAULT_NEG_RATIO,
    seed: int = 42,
    exclude_rows: set[int] | None = None,
    tile_weights: dict[tuple[int, int], int] | None = None,
    filter_games: list[str] | None = None,
    include_negatives: bool = True,
) -> dict[str, int]:
    """Build a YOLO dataset from manifest, with smart sampling.

    Hardlinks tile images from tiles_dir and writes .txt labels from SQLite.
    Produces train.txt/val.txt with spatial weighting and hard-negative sampling.
    """
    random.seed(seed)
    if exclude_rows is None:
        exclude_rows = DEFAULT_EXCLUDE_ROWS
    if tile_weights is None:
        tile_weights = DEFAULT_TILE_WEIGHTS

    # --- Discover games from manifest ---
    games_meta = list_games(conn)
    if filter_games:
        games_meta = [g for g in games_meta if g["game_id"] in filter_games]

    # Filter to games that actually have tiles on disk
    game_ids = []
    for g in games_meta:
        td = tiles_dir / g["game_id"]
        if td.is_dir():
            game_ids.append(g["game_id"])
        else:
            logger.warning("Skipping %s: tile dir not found at %s", g["game_id"], td)

    logger.info("Building dataset from %d games", len(game_ids))

    # --- Load all labeled stems from manifest (instant) ---
    t0 = time.time()
    labeled_by_game: dict[str, set[str]] = {}
    for gid in game_ids:
        labeled_by_game[gid] = get_labeled_stems(conn, gid)
    label_query_time = time.time() - t0
    total_labeled = sum(len(s) for s in labeled_by_game.values())
    logger.info("Queried %d labeled stems in %.2fs", total_labeled, label_query_time)

    # --- Collect tile paths ---
    # When include_negatives=False, we only need labeled tiles (manifest-driven,
    # no directory scan needed — just verify files exist). Much faster on HDD.
    t0 = time.time()
    game_tiles: dict[str, list[tuple[Path, str, bool]]] = {}
    total_tiles = 0
    total_excluded = 0

    if not include_negatives:
        # Manifest-driven: only check labeled tile files exist
        for gid in game_ids:
            td = tiles_dir / gid
            tiles = []
            for stem in labeled_by_game[gid]:
                pos = _parse_tile_position(stem)
                if pos and pos[0] in exclude_rows:
                    total_excluded += 1
                    continue
                tile_path = td / f"{stem}.jpg"
                if tile_path.exists():
                    tiles.append((tile_path, stem, True))
            if tiles:
                game_tiles[gid] = tiles
                total_tiles += len(tiles)
    else:
        # Full directory scan (needed for negative sampling)
        for gid in game_ids:
            td = tiles_dir / gid
            labeled_stems = labeled_by_game[gid]
            tiles = []
            for entry in os.scandir(td):
                if not entry.name.endswith(".jpg"):
                    continue
                stem = entry.name[:-4]  # strip .jpg
                pos = _parse_tile_position(stem)
                if pos and pos[0] in exclude_rows:
                    total_excluded += 1
                    continue
                has_label = stem in labeled_stems
                tiles.append((Path(entry.path), stem, has_label))
            if tiles:
                game_tiles[gid] = tiles
                total_tiles += len(tiles)

    scan_time = time.time() - t0
    logger.info(
        "Collected %d tiles in %.1fs (%d excluded, mode=%s)",
        total_tiles,
        scan_time,
        total_excluded,
        "positives-only" if not include_negatives else "full-scan",
    )

    # --- Game-level train/val split ---
    gids = list(game_tiles.keys())
    random.shuffle(gids)
    val_target = int(total_tiles * val_split)
    val_games = set()
    val_count = 0
    for gid in gids:
        if val_count >= val_target:
            break
        val_games.add(gid)
        val_count += len(game_tiles[gid])

    train_game_ids = [g for g in gids if g not in val_games]
    val_game_ids = list(val_games)
    logger.info(
        "Split: %d train games (%d tiles), %d val games (%d tiles)",
        len(train_game_ids),
        sum(len(game_tiles[g]) for g in train_game_ids),
        len(val_game_ids),
        sum(len(game_tiles[g]) for g in val_game_ids),
    )

    # --- Create output directories ---
    for split in ("train", "val"):
        (output_dir / "images" / split).mkdir(parents=True, exist_ok=True)
        (output_dir / "labels" / split).mkdir(parents=True, exist_ok=True)

    stats = {
        "train_images": 0,
        "val_images": 0,
        "train_labeled": 0,
        "val_labeled": 0,
        "train_hard_neg": 0,
        "train_random_neg": 0,
    }

    # --- Link tiles and write labels ---
    t0 = time.time()

    # Build lookup for smart sampling: "game_id/stem" → image path
    train_tile_index: dict[str, Path] = {}
    train_positive_stems: set[str] = set()

    for split_name, split_gids in [("train", train_game_ids), ("val", val_game_ids)]:
        for gid in split_gids:
            img_dir = output_dir / "images" / split_name / gid
            lbl_dir = output_dir / "labels" / split_name / gid
            img_dir.mkdir(parents=True, exist_ok=True)
            lbl_dir.mkdir(parents=True, exist_ok=True)

            # Batch-fetch all labels for this game from manifest
            labels_for_game = get_labels_for_game(conn, gid)
            labels_by_stem: dict[str, list[tuple[int, float, float, float, float]]] = {}
            for tile_stem, class_id, cx, cy, w, h in labels_for_game:
                labels_by_stem.setdefault(tile_stem, []).append(
                    (class_id, cx, cy, w, h)
                )

            for tile_path, stem, has_label in game_tiles[gid]:
                dst_img = img_dir / f"{stem}.jpg"
                dst_lbl = lbl_dir / f"{stem}.txt"

                if not dst_img.exists():
                    _link_or_copy(tile_path, dst_img)
                stats[f"{split_name}_images"] += 1

                if has_label and stem in labels_by_stem:
                    lines = [
                        f"{c} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}"
                        for c, cx, cy, w, h in labels_by_stem[stem]
                    ]
                    dst_lbl.write_text("\n".join(lines) + "\n")
                    stats[f"{split_name}_labeled"] += 1
                else:
                    dst_lbl.touch()

                # Track for smart sampling
                if split_name == "train":
                    key = f"{gid}/{stem}"
                    train_tile_index[key] = dst_img
                    if has_label:
                        train_positive_stems.add(key)

    link_time = time.time() - t0
    logger.info("Linked tiles + wrote labels in %.1fs", link_time)

    # --- Smart sampling: build train.txt ---
    t0 = time.time()
    train_paths = []

    # All positives
    for key in sorted(train_positive_stems):
        path_str = str(train_tile_index[key]).replace("\\", "/")
        pos = _parse_tile_position(key.split("/")[-1])
        weight = tile_weights.get(pos, 1) if pos else 1
        for _ in range(weight):
            train_paths.append(path_str)

    # Hard negatives
    all_train_stems = set(train_tile_index.keys())
    hard_negs = _find_hard_negatives(train_positive_stems, all_train_stems)
    for key in sorted(hard_negs):
        path_str = str(train_tile_index[key]).replace("\\", "/")
        pos = _parse_tile_position(key.split("/")[-1])
        repeat = ROW_2_OVERSAMPLE if (pos and pos[0] == 2) else 1
        for _ in range(repeat):
            train_paths.append(path_str)
    stats["train_hard_neg"] = len(hard_negs)

    # Random negatives to fill to target ratio
    target_negatives = int(len(train_positive_stems) * neg_ratio)
    remaining = target_negatives - len(hard_negs)
    if remaining > 0:
        random_pool = [
            k
            for k in all_train_stems
            if k not in train_positive_stems and k not in hard_negs
        ]
        n_random = min(remaining, len(random_pool))
        if n_random > 0:
            for key in random.sample(random_pool, n_random):
                train_paths.append(str(train_tile_index[key]).replace("\\", "/"))
            stats["train_random_neg"] = n_random

    random.shuffle(train_paths)

    # --- Val sampling ---
    val_paths = []
    val_all = set()
    val_positives = set()
    for gid in val_game_ids:
        for _, stem, has_label in game_tiles[gid]:
            img_path = output_dir / "images" / "val" / gid / f"{stem}.jpg"
            key = f"{gid}/{stem}"
            val_all.add((key, str(img_path).replace("\\", "/")))
            if has_label:
                val_positives.add((key, str(img_path).replace("\\", "/")))

    for _, path_str in sorted(val_positives):
        val_paths.append(path_str)

    val_neg_pool = [(k, p) for k, p in val_all if (k, p) not in val_positives]
    n_val_neg = min(int(len(val_positives) * neg_ratio), len(val_neg_pool))
    if n_val_neg > 0:
        for _, path_str in random.sample(val_neg_pool, n_val_neg):
            val_paths.append(path_str)

    random.shuffle(val_paths)

    # Write train.txt, val.txt
    (output_dir / "train.txt").write_text("\n".join(train_paths) + "\n")
    (output_dir / "val.txt").write_text("\n".join(val_paths) + "\n")

    # Write dataset.yaml
    yaml_content = (
        f"path: {output_dir}\n"
        f"train: train.txt\n"
        f"val: val.txt\n"
        f"\n"
        f"nc: 1\n"
        f"names: ['ball']\n"
    )
    (output_dir / "dataset.yaml").write_text(yaml_content)

    sample_time = time.time() - t0
    logger.info(
        "Smart sampling in %.1fs:\n"
        "  Train: %d positive (weighted), %d hard neg, %d random neg = %d entries\n"
        "  Val: %d positive, %d negative = %d entries\n"
        "  Dataset: %s",
        sample_time,
        stats["train_labeled"],
        stats["train_hard_neg"],
        stats.get("train_random_neg", 0),
        len(train_paths),
        len(val_positives),
        n_val_neg,
        len(val_paths),
        output_dir / "dataset.yaml",
    )

    logger.info(
        "Dataset built: train=%d images (%d labeled), val=%d images (%d labeled)",
        stats["train_images"],
        stats["train_labeled"],
        stats["val_images"],
        stats["val_labeled"],
    )
    return stats


# ---------------------------------------------------------------------------
# Backup and merge
# ---------------------------------------------------------------------------


def backup_db(db_path: Path = DEFAULT_DB_PATH) -> Path:
    """Create a timestamped backup of the manifest database.

    Returns the backup path.
    """
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup_path = db_path.parent / f"{db_path.stem}_backup_{timestamp}{db_path.suffix}"
    # Use SQLite backup API for a consistent snapshot (safe even during WAL writes)
    src = sqlite3.connect(str(db_path))
    dst = sqlite3.connect(str(backup_path))
    src.backup(dst)
    dst.close()
    src.close()
    logger.info(
        "Backup created: %s (%.1fMB)",
        backup_path,
        backup_path.stat().st_size / 1024 / 1024,
    )
    return backup_path


def merge_labels_from(
    conn: sqlite3.Connection,
    remote_db_path: Path,
) -> dict:
    """Merge labels from a remote manifest.db into this one.

    Reads all labels from remote_db_path and inserts them into conn
    using INSERT OR IGNORE (skips duplicates on the unique constraint).
    Also merges game metadata if games don't exist locally.

    Returns: {games_merged, labels_inserted, labels_skipped}
    """
    remote = sqlite3.connect(str(remote_db_path))
    remote.execute("PRAGMA journal_mode=WAL")

    # Get all labels from remote
    remote_labels = remote.execute(
        "SELECT game_id, tile_stem, class_id, cx, cy, w, h, source, confidence FROM labels"
    ).fetchall()

    # Ensure game rows exist for any new game_ids
    remote_games = remote.execute("SELECT DISTINCT game_id FROM labels").fetchall()
    games_merged = 0
    for (gid,) in remote_games:
        existing = conn.execute(
            "SELECT 1 FROM games WHERE game_id = ?", (gid,)
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO games (game_id, last_updated) VALUES (?, ?)",
                (gid, time.time()),
            )
            games_merged += 1

    # Bulk insert labels (skips duplicates)
    before = conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0]
    conn.executemany(
        """INSERT OR IGNORE INTO labels
           (game_id, tile_stem, class_id, cx, cy, w, h, source, confidence)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        remote_labels,
    )
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0]

    # Update labeled_count for affected games
    for (gid,) in remote_games:
        labeled = conn.execute(
            "SELECT COUNT(DISTINCT tile_stem) FROM labels WHERE game_id = ?", (gid,)
        ).fetchone()[0]
        conn.execute(
            "UPDATE games SET labeled_count = ?, last_updated = ? WHERE game_id = ?",
            (labeled, time.time(), gid),
        )
    conn.commit()

    remote.close()

    inserted = after - before
    skipped = len(remote_labels) - inserted
    logger.info(
        "Merged from %s: %d labels inserted, %d skipped (duplicates), %d new games",
        remote_db_path,
        inserted,
        skipped,
        games_merged,
    )
    return {
        "games_merged": games_merged,
        "labels_inserted": inserted,
        "labels_skipped": skipped,
    }


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def print_stats(db_path: Path = DEFAULT_DB_PATH) -> None:
    """Print summary statistics from the manifest."""
    conn = open_db(db_path)

    total_labels = conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0]
    total_games = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    distinct_stems = conn.execute(
        "SELECT COUNT(DISTINCT tile_stem) FROM labels"
    ).fetchone()[0]
    total_tiles_cat = conn.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
    total_frames_cat = conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
    total_segments_cat = conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
    packed_tiles = conn.execute(
        "SELECT COUNT(*) FROM tiles WHERE pack_file IS NOT NULL"
    ).fetchone()[0]

    print(f"\nManifest: {db_path}")
    print(f"  Games:          {total_games}")
    print(f"  Segments:       {total_segments_cat}")
    print(f"  Frames:         {total_frames_cat:,}")
    print(f"  Tiles:          {total_tiles_cat:,}")
    print(f"  Packed tiles:   {packed_tiles:,}")
    print(f"  Labeled tiles:  {distinct_stems:,}")
    print(f"  Total labels:   {total_labels:,}")
    print()

    games = list_games(conn)
    if games:
        print(f"  {'Game':<50} {'Segs':>5} {'Tiles':>10} {'Packed':>8} {'Labels':>8}")
        print(f"  {'-' * 50} {'-' * 5} {'-' * 10} {'-' * 8} {'-' * 8}")
        for g in games:
            gid = g["game_id"]
            seg_count = conn.execute(
                "SELECT COUNT(*) FROM segments WHERE game_id = ?", (gid,)
            ).fetchone()[0]
            tile_count = conn.execute(
                "SELECT COUNT(*) FROM tiles WHERE game_id = ?", (gid,)
            ).fetchone()[0]
            pack_count = conn.execute(
                "SELECT COUNT(*) FROM tiles WHERE game_id = ? AND pack_file IS NOT NULL",
                (gid,),
            ).fetchone()[0]
            label_count = conn.execute(
                "SELECT COUNT(*) FROM labels WHERE game_id = ?", (gid,)
            ).fetchone()[0]
            cataloged = "Y" if g.get("tiles_cataloged") else " "
            print(
                f"  {gid:<50} {seg_count:>5} {tile_count:>10,} "
                f"{pack_count:>8,} {label_count:>8,}"
            )
    print()
    conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="SQLite manifest for YOLO labels")
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help="Database path (default: %(default)s)",
    )
    sub = parser.add_subparsers(dest="command")

    # migrate
    mig = sub.add_parser("migrate", help="Ingest .txt label files into manifest.db")
    mig.add_argument(
        "--labels", type=Path, default=DEFAULT_LABELS_DIR, help="Labels directory"
    )
    mig.add_argument(
        "--tiles", type=Path, default=DEFAULT_TILES_DIR, help="Tiles directory"
    )
    mig.add_argument(
        "--rebuild", action="store_true", help="Drop and rebuild from scratch"
    )
    mig.add_argument("--games", nargs="+", help="Only migrate specific games")

    # build-dataset
    bld = sub.add_parser("build-dataset", help="Build YOLO dataset from manifest")
    bld.add_argument(
        "--tiles", type=Path, default=DEFAULT_TILES_DIR, help="Tiles directory"
    )
    bld.add_argument(
        "-o", "--output", type=Path, required=True, help="Output dataset root"
    )
    bld.add_argument("--val-split", type=float, default=DEFAULT_VAL_SPLIT)
    bld.add_argument("--neg-ratio", type=float, default=DEFAULT_NEG_RATIO)
    bld.add_argument("--seed", type=int, default=42)
    bld.add_argument("--games", nargs="+", help="Only include these games")
    bld.add_argument(
        "--no-negatives", action="store_true", help="Exclude unlabeled tiles"
    )
    bld.add_argument(
        "--no-weights", action="store_true", help="Disable spatial weighting"
    )
    bld.add_argument(
        "--no-exclude", action="store_true", help="Don't exclude any tile rows"
    )

    # catalog
    cat = sub.add_parser("catalog", help="Catalog tiles from disk into manifest")
    cat.add_argument(
        "--tiles", type=Path, default=DEFAULT_TILES_DIR, help="Tiles directory"
    )
    cat.add_argument("--rescan", action="store_true", help="Force re-catalog all games")
    cat.add_argument("--games", nargs="+", help="Only catalog specific games")

    # pack
    pak = sub.add_parser("pack", help="Build segment pack files from loose tiles")
    pak.add_argument(
        "--tiles", type=Path, default=DEFAULT_TILES_DIR, help="Tiles directory"
    )
    pak.add_argument(
        "--pack-dir", type=Path, default=DEFAULT_PACK_DIR, help="Pack output directory"
    )
    pak.add_argument("--games", nargs="+", help="Only pack specific games")
    pak.add_argument(
        "--delete-loose",
        action="store_true",
        help="Delete loose .jpg files after packing each segment (saves disk space)",
    )
    pak.add_argument(
        "--ssd",
        type=Path,
        default=None,
        help="SSD staging directory — copies tiles here before packing for faster reads",
    )

    # backup
    sub.add_parser(
        "backup", help="Create a timestamped backup of the manifest database"
    )

    # merge
    mrg = sub.add_parser("merge", help="Merge labels from a remote manifest.db")
    mrg.add_argument(
        "remote_db", type=Path, help="Path to remote manifest.db to merge from"
    )

    # stats
    sub.add_parser("stats", help="Show manifest statistics")

    # export
    exp = sub.add_parser("export", help="Export labels back to .txt files")
    exp.add_argument("--game", required=True, help="Game ID to export")
    exp.add_argument("--out-dir", type=Path, required=True, help="Output directory")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.command == "migrate":
        migrate_all(
            labels_dir=args.labels,
            tiles_dir=args.tiles,
            db_path=args.db,
            rebuild=args.rebuild,
            games=args.games,
        )
    elif args.command == "catalog":
        conn = open_db(args.db, create=True)
        catalog_all_games(
            conn, tiles_dir=args.tiles, rescan=args.rescan, games=args.games
        )
        conn.close()
    elif args.command == "pack":
        conn = open_db(args.db)
        pack_all_games(
            conn,
            tiles_dir=args.tiles,
            pack_dir=args.pack_dir,
            games=args.games,
            delete_loose=args.delete_loose,
            ssd_staging=args.ssd,
        )
        conn.close()
    elif args.command == "build-dataset":
        conn = open_db(args.db)
        build_dataset(
            conn,
            tiles_dir=args.tiles,
            output_dir=args.output,
            val_split=args.val_split,
            neg_ratio=args.neg_ratio,
            seed=args.seed,
            exclude_rows=set() if args.no_exclude else DEFAULT_EXCLUDE_ROWS,
            tile_weights=None if args.no_weights else DEFAULT_TILE_WEIGHTS,
            filter_games=args.games,
            include_negatives=not args.no_negatives,
        )
        conn.close()
    elif args.command == "backup":
        backup_db(args.db)
    elif args.command == "merge":
        backup_db(args.db)  # Always backup before merge
        conn = open_db(args.db)
        result = merge_labels_from(conn, args.remote_db)
        conn.close()
        print(
            f"Merged: {result['labels_inserted']} labels inserted, "
            f"{result['labels_skipped']} skipped, {result['games_merged']} new games"
        )
    elif args.command == "stats":
        print_stats(args.db)
    elif args.command == "export":
        conn = open_db(args.db)
        count = export_labels_to_txt(conn, args.game, args.out_dir)
        logger.info("Exported %d label files to %s", count, args.out_dir)
        conn.close()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
