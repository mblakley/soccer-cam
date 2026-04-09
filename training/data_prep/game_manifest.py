"""Per-game manifest — small SQLite DB per game for tiles and labels.

Replaces the monolithic manifest.db. Each game gets its own DB at:
    D:/training_data/games/{game_id}/manifest.db

No game_id column needed in per-game tables (it's implicit from the path).
Single-writer guaranteed by the work queue claim mechanism.

Usage:
    from training.data_prep.game_manifest import GameManifest

    gm = GameManifest("D:/training_data/games/flash__2024.06.01_vs_IYSA_home")
    gm.open()
    gm.insert_tiles(tile_rows)
    gm.insert_labels(label_rows)
    stats = gm.get_stats()
    gm.close()
"""

import logging
import os
import re
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_TILE_RE = re.compile(r"^(.+)_frame_(\d{6})_r(\d+)_c(\d+)$")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS segments (
    segment TEXT PRIMARY KEY,
    frame_count INTEGER DEFAULT 0,
    tile_count INTEGER DEFAULT 0,
    frame_min INTEGER,
    frame_max INTEGER,
    max_gap INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS frames (
    segment TEXT NOT NULL,
    frame_idx INTEGER NOT NULL,
    tile_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (segment, frame_idx),
    FOREIGN KEY (segment) REFERENCES segments(segment)
);

CREATE TABLE IF NOT EXISTS tiles (
    segment TEXT NOT NULL,
    frame_idx INTEGER NOT NULL,
    row INTEGER NOT NULL,
    col INTEGER NOT NULL,
    pack_file TEXT,
    pack_offset INTEGER,
    pack_size INTEGER,
    PRIMARY KEY (segment, frame_idx, row, col),
    FOREIGN KEY (segment, frame_idx) REFERENCES frames(segment, frame_idx)
);

CREATE TABLE IF NOT EXISTS labels (
    id INTEGER PRIMARY KEY,
    tile_stem TEXT NOT NULL,
    class_id INTEGER DEFAULT 0,
    cx REAL NOT NULL,
    cy REAL NOT NULL,
    w REAL NOT NULL,
    h REAL NOT NULL,
    source TEXT,
    confidence REAL,
    qa_verdict TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_labels_unique
    ON labels(tile_stem, class_id, cx, cy);
CREATE INDEX IF NOT EXISTS idx_labels_stem ON labels(tile_stem);
CREATE INDEX IF NOT EXISTS idx_tiles_segment ON tiles(segment);

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS ball_events (
    id INTEGER PRIMARY KEY,
    segment TEXT NOT NULL,
    frame_idx INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    pano_x REAL,
    pano_y REAL,
    trajectory_id INTEGER,
    source TEXT,
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_ball_events_seg ON ball_events(segment, frame_idx);
"""


class GameManifest:
    """Per-game manifest backed by SQLite."""

    def __init__(self, game_dir: str | Path):
        """Initialize with the game directory path.

        The manifest.db will be at {game_dir}/manifest.db.
        Pack files will be at {game_dir}/tile_packs/.
        """
        self.game_dir = Path(game_dir)
        self.db_path = self.game_dir / "manifest.db"
        self.pack_dir = self.game_dir / "tile_packs"
        self._conn: sqlite3.Connection | None = None

    @property
    def game_id(self) -> str:
        return self.game_dir.name

    def open(self, create: bool = True) -> "GameManifest":
        """Open the manifest database."""
        if self._conn is not None:
            return self

        if not create and not self.db_path.exists():
            raise FileNotFoundError(f"Manifest not found: {self.db_path}")

        self.game_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        # FK enforcement off — we do bulk inserts into tiles before
        # frames/segments exist, then rebuild stats afterward.
        self._conn.execute("PRAGMA foreign_keys=OFF")
        self._conn.executescript(SCHEMA_SQL)
        return self

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Manifest not open. Call .open() first.")
        return self._conn

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *args):
        self.close()

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    def set_metadata(self, key: str, value: str):
        self.conn.execute(
            "INSERT INTO metadata (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    def get_metadata(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    # ------------------------------------------------------------------
    # Tiles
    # ------------------------------------------------------------------

    def insert_tiles(self, tile_rows: list[tuple]) -> int:
        """Bulk insert tile rows: (segment, frame_idx, row, col).

        Returns number inserted.
        """
        cursor = self.conn.executemany(
            """INSERT OR IGNORE INTO tiles (segment, frame_idx, row, col)
               VALUES (?, ?, ?, ?)""",
            tile_rows,
        )
        self.conn.commit()
        return cursor.rowcount

    def update_pack_info(
        self,
        segment: str,
        frame_idx: int,
        row: int,
        col: int,
        pack_file: str,
        pack_offset: int,
        pack_size: int,
    ):
        """Update pack file location for a tile."""
        self.conn.execute(
            """UPDATE tiles SET pack_file=?, pack_offset=?, pack_size=?
               WHERE segment=? AND frame_idx=? AND row=? AND col=?""",
            (pack_file, pack_offset, pack_size, segment, frame_idx, row, col),
        )

    def bulk_update_pack_info(self, updates: list[tuple]):
        """Bulk update pack info: (pack_file, pack_offset, pack_size, segment, frame_idx, row, col)."""
        self.conn.executemany(
            """UPDATE tiles SET pack_file=?, pack_offset=?, pack_size=?
               WHERE segment=? AND frame_idx=? AND row=? AND col=?""",
            updates,
        )
        self.conn.commit()

    def get_tile(self, segment: str, frame_idx: int, row: int, col: int) -> dict | None:
        """Get a single tile record."""
        row_result = self.conn.execute(
            "SELECT * FROM tiles WHERE segment=? AND frame_idx=? AND row=? AND col=?",
            (segment, frame_idx, row, col),
        ).fetchone()
        return dict(row_result) if row_result else None

    def get_tiles_for_segment(self, segment: str) -> list[dict]:
        """Get all tiles in a segment, sorted by frame/row/col."""
        rows = self.conn.execute(
            "SELECT * FROM tiles WHERE segment=? ORDER BY frame_idx, row, col",
            (segment,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_tile_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) as cnt FROM tiles").fetchone()
        return row["cnt"]

    def read_tile_from_pack(self, segment: str, frame_idx: int, row: int, col: int) -> bytes | None:
        """Read tile JPEG bytes from its pack file."""
        tile = self.get_tile(segment, frame_idx, row, col)
        if not tile or not tile["pack_file"]:
            return None
        with open(tile["pack_file"], "rb") as f:
            f.seek(tile["pack_offset"])
            return f.read(tile["pack_size"])

    # ------------------------------------------------------------------
    # Segments & Frames
    # ------------------------------------------------------------------

    def rebuild_segment_stats(self):
        """Rebuild segment and frame stats from tiles table."""
        # Clear existing
        self.conn.execute("DELETE FROM frames")
        self.conn.execute("DELETE FROM segments")

        # Rebuild frames
        self.conn.execute(
            """INSERT INTO frames (segment, frame_idx, tile_count)
               SELECT segment, frame_idx, COUNT(*) FROM tiles
               GROUP BY segment, frame_idx"""
        )

        # Rebuild segments
        self.conn.execute(
            """INSERT INTO segments (segment, frame_count, tile_count, frame_min, frame_max)
               SELECT segment, COUNT(DISTINCT frame_idx), COUNT(*),
                      MIN(frame_idx), MAX(frame_idx)
               FROM tiles GROUP BY segment"""
        )
        self.conn.commit()

    def get_segments(self) -> list[str]:
        """Return all segment names."""
        rows = self.conn.execute(
            "SELECT segment FROM segments ORDER BY segment"
        ).fetchall()
        return [r["segment"] for r in rows]

    def get_segment_summary(self) -> list[dict]:
        """Return per-segment stats."""
        rows = self.conn.execute(
            "SELECT * FROM segments ORDER BY segment"
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Labels
    # ------------------------------------------------------------------

    def upsert_label(
        self,
        tile_stem: str,
        class_id: int,
        cx: float,
        cy: float,
        w: float,
        h: float,
        source: str | None = None,
        confidence: float | None = None,
    ):
        """Insert or update a single label."""
        self.conn.execute(
            """INSERT INTO labels (tile_stem, class_id, cx, cy, w, h, source, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(tile_stem, class_id, cx, cy)
               DO UPDATE SET w=excluded.w, h=excluded.h,
                             source=excluded.source, confidence=excluded.confidence""",
            (tile_stem, class_id, cx, cy, w, h, source, confidence),
        )

    def bulk_insert_labels(self, rows: list[tuple]) -> int:
        """Bulk insert: (tile_stem, class_id, cx, cy, w, h, source, confidence).

        Uses INSERT OR IGNORE to skip duplicates. Returns count inserted.
        """
        cursor = self.conn.executemany(
            """INSERT OR IGNORE INTO labels
               (tile_stem, class_id, cx, cy, w, h, source, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        self.conn.commit()
        return cursor.rowcount

    def get_labels_for_tile(self, tile_stem: str) -> list[dict]:
        """Return all labels for a tile."""
        rows = self.conn.execute(
            "SELECT * FROM labels WHERE tile_stem = ?", (tile_stem,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_labeled_stems(self) -> set[str]:
        """Return set of tile_stems that have at least one label."""
        rows = self.conn.execute(
            "SELECT DISTINCT tile_stem FROM labels"
        ).fetchall()
        return {r["tile_stem"] for r in rows}

    def get_label_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) as cnt FROM labels").fetchone()
        return row["cnt"]

    def get_positive_tile_count(self) -> int:
        """Count distinct tiles that have at least one label."""
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT tile_stem) as cnt FROM labels"
        ).fetchone()
        return row["cnt"]

    def set_qa_verdict(self, tile_stem: str, verdict: str):
        """Update QA verdict for all labels on a tile."""
        self.conn.execute(
            "UPDATE labels SET qa_verdict = ? WHERE tile_stem = ?",
            (verdict, tile_stem),
        )
        self.conn.commit()

    def bulk_set_qa_verdicts(self, verdicts: list[tuple[str, str]]):
        """Bulk update: list of (verdict, tile_stem)."""
        self.conn.executemany(
            "UPDATE labels SET qa_verdict = ? WHERE tile_stem = ?",
            verdicts,
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # Ball events (out-of-play / back-in-play)
    # ------------------------------------------------------------------

    def insert_ball_event(
        self,
        segment: str,
        frame_idx: int,
        event_type: str,
        pano_x: float | None = None,
        pano_y: float | None = None,
        trajectory_id: int | None = None,
        source: str | None = None,
    ):
        """Insert a ball event (out_of_play, back_in_play, etc.)."""
        import time

        self.conn.execute(
            """INSERT INTO ball_events
               (segment, frame_idx, event_type, pano_x, pano_y, trajectory_id, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (segment, frame_idx, event_type, pano_x, pano_y, trajectory_id, source, time.time()),
        )
        self.conn.commit()

    def get_ball_events(
        self, segment: str | None = None, event_type: str | None = None
    ) -> list[dict]:
        """Query ball events, optionally filtered."""
        query = "SELECT * FROM ball_events WHERE 1=1"
        params: list = []
        if segment:
            query += " AND segment = ?"
            params.append(segment)
        if event_type:
            query += " AND event_type = ?"
            params.append(event_type)
        query += " ORDER BY segment, frame_idx"

        rows = self.conn.execute(query, params).fetchall()
        cols = [d[0] for d in self.conn.execute("PRAGMA table_info(ball_events)").fetchall()]
        # Handle case where table doesn't exist yet (older manifests)
        if not cols:
            return []
        col_names = [c[1] for c in self.conn.execute("PRAGMA table_info(ball_events)").fetchall()]
        return [dict(zip(col_names, row)) for row in rows]

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_stats(self) -> dict:
        """Get summary statistics for this game."""
        tiles = self.get_tile_count()
        labels = self.get_label_count()
        positives = self.get_positive_tile_count()
        segments = self.conn.execute("SELECT COUNT(*) as cnt FROM segments").fetchone()["cnt"]

        return {
            "game_id": self.game_id,
            "segments": segments,
            "tiles": tiles,
            "labels": labels,
            "positive_tiles": positives,
            "negative_tiles": tiles - positives,
        }

    # ------------------------------------------------------------------
    # Export (for training set building)
    # ------------------------------------------------------------------

    def export_labels_yolo(self, output_dir: Path):
        """Export all labels as YOLO .txt files for training.

        Writes one .txt file per tile_stem that has labels.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        rows = self.conn.execute(
            "SELECT tile_stem, class_id, cx, cy, w, h FROM labels ORDER BY tile_stem"
        ).fetchall()

        current_stem = None
        lines = []
        for row in rows:
            if row["tile_stem"] != current_stem:
                if current_stem and lines:
                    (output_dir / f"{current_stem}.txt").write_text("\n".join(lines) + "\n")
                current_stem = row["tile_stem"]
                lines = []
            lines.append(f"{row['class_id']} {row['cx']} {row['cy']} {row['w']} {row['h']}")

        if current_stem and lines:
            (output_dir / f"{current_stem}.txt").write_text("\n".join(lines) + "\n")
