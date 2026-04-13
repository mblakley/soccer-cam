"""Game registry — lightweight SQLite DB tracking all games and their pipeline state.

registry.db is the single source of truth for "what games exist and where are
they in the pipeline." Only the orchestrator writes to it. Workers read it.

Usage:
    from training.pipeline.registry import GameRegistry

    reg = GameRegistry("D:/training_data/registry.db")
    reg.register_game("flash__2024.06.01_vs_IYSA_home", team="flash", ...)
    games = reg.get_games_in_state("TILED")
    reg.set_state("flash__2024.06.01_vs_IYSA_home", "LABELING")
"""

import logging
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS games (
    game_id TEXT PRIMARY KEY,
    team TEXT,
    date TEXT,
    opponent TEXT,
    location TEXT,
    video_path TEXT,
    needs_flip INTEGER DEFAULT 0,
    game_type TEXT,
    camera_type TEXT DEFAULT 'dahua',
    trainable INTEGER DEFAULT 1,

    -- Pipeline state
    pipeline_state TEXT DEFAULT 'REGISTERED',
    pipeline_updated REAL,
    pipeline_error TEXT,
    pipeline_attempts INTEGER DEFAULT 0,

    -- Stats (cached from per-game manifest)
    tile_count INTEGER DEFAULT 0,
    label_count INTEGER DEFAULT 0,
    positive_count INTEGER DEFAULT 0,
    segment_count INTEGER DEFAULT 0,
    coverage REAL DEFAULT 0.0,

    -- Timestamps
    created_at REAL,
    staged_at REAL,
    tiled_at REAL,
    labeled_at REAL,
    qa_done_at REAL,
    last_trained_at REAL
);

CREATE INDEX IF NOT EXISTS idx_games_state ON games(pipeline_state);
CREATE INDEX IF NOT EXISTS idx_games_team ON games(team);
"""


class GameRegistry:
    """Lightweight game registry backed by SQLite."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path), timeout=10)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA_SQL)
        return self._conn

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_game(
        self,
        game_id: str,
        *,
        team: str | None = None,
        date: str | None = None,
        opponent: str | None = None,
        location: str | None = None,
        video_path: str | None = None,
        needs_flip: bool = False,
        game_type: str | None = None,
        camera_type: str = "dahua",
        trainable: bool = True,
        pipeline_state: str = "REGISTERED",
    ):
        """Register a new game or update an existing one."""
        conn = self._get_conn()
        now = time.time()
        conn.execute(
            """INSERT INTO games
               (game_id, team, date, opponent, location, video_path,
                needs_flip, game_type, camera_type, trainable,
                pipeline_state, pipeline_updated, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(game_id) DO UPDATE SET
                   team=COALESCE(excluded.team, team),
                   date=COALESCE(excluded.date, date),
                   opponent=COALESCE(excluded.opponent, opponent),
                   location=COALESCE(excluded.location, location),
                   video_path=COALESCE(excluded.video_path, video_path),
                   needs_flip=excluded.needs_flip,
                   game_type=COALESCE(excluded.game_type, game_type),
                   camera_type=excluded.camera_type,
                   trainable=excluded.trainable,
                   pipeline_updated=excluded.pipeline_updated""",
            (
                game_id,
                team,
                date,
                opponent,
                location,
                video_path,
                1 if needs_flip else 0,
                game_type,
                camera_type,
                1 if trainable else 0,
                pipeline_state,
                now,
                now,
            ),
        )
        conn.commit()

    # ------------------------------------------------------------------
    # State management
    # ------------------------------------------------------------------

    def get_state(self, game_id: str) -> str | None:
        """Get current pipeline state for a game."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT pipeline_state FROM games WHERE game_id = ?", (game_id,)
        ).fetchone()
        return row["pipeline_state"] if row else None

    def set_state(
        self,
        game_id: str,
        new_state: str,
        *,
        error: str | None = None,
    ):
        """Update pipeline state for a game."""
        conn = self._get_conn()
        now = time.time()

        # Update the state-specific timestamp
        timestamp_col = {
            "STAGING": "staged_at",
            "TILED": "tiled_at",
            "LABELED": "labeled_at",
            "QA_DONE": "qa_done_at",
        }.get(new_state)

        if timestamp_col:
            conn.execute(
                f"UPDATE games SET pipeline_state=?, pipeline_updated=?, pipeline_error=?, {timestamp_col}=? WHERE game_id=?",
                (new_state, now, error, now, game_id),
            )
        else:
            conn.execute(
                "UPDATE games SET pipeline_state=?, pipeline_updated=?, pipeline_error=? WHERE game_id=?",
                (new_state, now, error, game_id),
            )
        conn.commit()
        logger.info(
            "Game %s → %s%s", game_id, new_state, f" ({error})" if error else ""
        )

    def increment_attempts(self, game_id: str):
        """Increment the failure attempt counter."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE games SET pipeline_attempts = pipeline_attempts + 1 WHERE game_id = ?",
            (game_id,),
        )
        conn.commit()

    def reset_attempts(self, game_id: str):
        """Reset failure counter (after successful advancement)."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE games SET pipeline_attempts = 0, pipeline_error = NULL WHERE game_id = ?",
            (game_id,),
        )
        conn.commit()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_game(self, game_id: str) -> dict | None:
        """Get full game record."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM games WHERE game_id = ?", (game_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_games_in_state(self, state: str) -> list[dict]:
        """Get all games in a given pipeline state."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM games WHERE pipeline_state = ? ORDER BY game_id",
            (state,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_games(self) -> list[dict]:
        """Get all games sorted by game_id."""
        conn = self._get_conn()
        rows = conn.execute("SELECT * FROM games ORDER BY game_id").fetchall()
        return [dict(r) for r in rows]

    def get_trainable_games(self) -> list[dict]:
        """Get all games in TRAINABLE state."""
        return self.get_games_in_state("TRAINABLE")

    def get_state_counts(self) -> dict[str, int]:
        """Get count of games per pipeline state."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT pipeline_state, COUNT(*) as cnt FROM games GROUP BY pipeline_state"
        ).fetchall()
        return {r["pipeline_state"]: r["cnt"] for r in rows}

    def get_failed_games(self) -> list[dict]:
        """Get all games in FAILED:* states."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM games WHERE pipeline_state LIKE 'FAILED:%' ORDER BY game_id"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_games_needing_work(self) -> list[dict]:
        """Get games that need work enqueued (not terminal, not on hold)."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT * FROM games
               WHERE pipeline_state NOT IN ('TRAINABLE', 'EXCLUDED', 'HOLD')
                 AND trainable = 1
               ORDER BY pipeline_state, game_id""",
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Stats updates (called after tasks complete)
    # ------------------------------------------------------------------

    def update_stats(
        self,
        game_id: str,
        *,
        tile_count: int | None = None,
        label_count: int | None = None,
        positive_count: int | None = None,
        segment_count: int | None = None,
        coverage: float | None = None,
        video_path: str | None = None,
        **kwargs,
    ):
        """Update cached stats for a game."""
        conn = self._get_conn()
        updates = []
        params = []
        if tile_count is not None:
            updates.append("tile_count = ?")
            params.append(tile_count)
        if label_count is not None:
            updates.append("label_count = ?")
            params.append(label_count)
        if positive_count is not None:
            updates.append("positive_count = ?")
            params.append(positive_count)
        if segment_count is not None:
            updates.append("segment_count = ?")
            params.append(segment_count)
        if coverage is not None:
            updates.append("coverage = ?")
            params.append(coverage)
        if video_path is not None:
            updates.append("video_path = ?")
            params.append(video_path)
        if "needs_flip" in kwargs:
            updates.append("needs_flip = ?")
            params.append(1 if kwargs["needs_flip"] else 0)

        if updates:
            params.append(game_id)
            conn.execute(
                f"UPDATE games SET {', '.join(updates)} WHERE game_id = ?",
                params,
            )
            conn.commit()
