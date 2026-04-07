"""SQLite manifest for label storage — replaces ~500K small .txt files.

Schema stores YOLO-format bounding boxes per tile, with game-level metadata.
One database per dataset: manifest.db

Usage:
    # Migrate existing .txt labels into manifest.db
    uv run python -m training.data_prep.manifest migrate
    uv run python -m training.data_prep.manifest migrate --games flash__2024.05.01_vs_RNYFC_away
    uv run python -m training.data_prep.manifest migrate --rebuild

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
    last_updated REAL
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
"""


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def open_db(db_path: Path = DEFAULT_DB_PATH, *, create: bool = False) -> sqlite3.Connection:
    """Open manifest database. Creates schema if *create* is True."""
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
    rows = conn.execute(
        "SELECT * FROM games ORDER BY game_id"
    ).fetchall()
    conn.row_factory = None
    return [dict(r) for r in rows]


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
        lines = [f"{c} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}" for c, cx, cy, w, h in detections]
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
            detections.append((
                int(parts[0]),
                float(parts[1]),
                float(parts[2]),
                float(parts[3]),
                float(parts[4]),
            ))
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
        return {"total_files": 0, "positive_files": 0, "labels_inserted": 0, "skipped": 0}

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
        d.name for d in labels_dir.iterdir()
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
                logger.info("Skipping %s (already migrated: %d labeled)", game_id, existing["labeled_count"])
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
    (1, 0): 3, (1, 1): 2, (1, 2): 2, (1, 3): 2, (1, 4): 2, (1, 5): 2, (1, 6): 3,
    (2, 0): 1, (2, 1): 2, (2, 2): 2, (2, 3): 2, (2, 4): 2, (2, 5): 2, (2, 6): 1,
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
        total_tiles, scan_time, total_excluded,
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
        "train_images": 0, "val_images": 0,
        "train_labeled": 0, "val_labeled": 0,
        "train_hard_neg": 0, "train_random_neg": 0,
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
                labels_by_stem.setdefault(tile_stem, []).append((class_id, cx, cy, w, h))

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
            k for k in all_train_stems
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
        stats["train_labeled"], stats["train_hard_neg"], stats.get("train_random_neg", 0),
        len(train_paths),
        len(val_positives), n_val_neg, len(val_paths),
        output_dir / "dataset.yaml",
    )

    logger.info(
        "Dataset built: train=%d images (%d labeled), val=%d images (%d labeled)",
        stats["train_images"], stats["train_labeled"],
        stats["val_images"], stats["val_labeled"],
    )
    return stats


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def print_stats(db_path: Path = DEFAULT_DB_PATH) -> None:
    """Print summary statistics from the manifest."""
    conn = open_db(db_path)

    total_labels = conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0]
    total_games = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    distinct_stems = conn.execute("SELECT COUNT(DISTINCT tile_stem) FROM labels").fetchone()[0]

    print(f"\nManifest: {db_path}")
    print(f"  Games:          {total_games}")
    print(f"  Labeled tiles:  {distinct_stems}")
    print(f"  Total labels:   {total_labels}")
    print()

    games = list_games(conn)
    if games:
        print(f"  {'Game':<55} {'Tiles':>8} {'Labeled':>8} {'Labels':>8}")
        print(f"  {'-'*55} {'-'*8} {'-'*8} {'-'*8}")
        for g in games:
            label_count = conn.execute(
                "SELECT COUNT(*) FROM labels WHERE game_id = ?", (g["game_id"],)
            ).fetchone()[0]
            print(
                f"  {g['game_id']:<55} {g['tile_count'] or 0:>8} "
                f"{g['labeled_count'] or 0:>8} {label_count:>8}"
            )
    print()
    conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="SQLite manifest for YOLO labels")
    parser.add_argument(
        "--db", type=Path, default=DEFAULT_DB_PATH, help="Database path (default: %(default)s)"
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
    mig.add_argument("--rebuild", action="store_true", help="Drop and rebuild from scratch")
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
    bld.add_argument("--no-negatives", action="store_true", help="Exclude unlabeled tiles")
    bld.add_argument("--no-weights", action="store_true", help="Disable spatial weighting")
    bld.add_argument("--no-exclude", action="store_true", help="Don't exclude any tile rows")

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
