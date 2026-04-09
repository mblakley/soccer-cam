"""Work queue — SQLite-based pull queue for distributed task execution.

Workers on any machine claim items atomically. The orchestrator enqueues work.
SQLite WAL mode allows concurrent readers with serialized writes.

Usage:
    from training.pipeline.queue import WorkQueue

    q = WorkQueue("D:/training_data/work_queue.db")
    q.enqueue("tile", game_id="flash__2024.06.01_vs_IYSA_home", priority=30)

    item = q.claim(capabilities=["tile", "label"], hostname="jared-laptop")
    if item:
        q.heartbeat(item["id"])
        # ... do work ...
        q.complete(item["id"], result={"tiles": 12345})

    q.reclaim_stale(timeout=7200)
"""

import json
import logging
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS work_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,
    game_id TEXT,
    priority INTEGER DEFAULT 50,
    status TEXT DEFAULT 'queued',
    target_machine TEXT,
    requires TEXT,
    payload TEXT,
    created_at REAL,
    claimed_at REAL,
    claimed_by TEXT,
    started_at REAL,
    completed_at REAL,
    heartbeat_at REAL,
    result TEXT,
    attempts INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_queue_status ON work_items(status, priority);
CREATE INDEX IF NOT EXISTS idx_queue_game ON work_items(game_id, task_type);
CREATE INDEX IF NOT EXISTS idx_queue_claimed ON work_items(claimed_by, status);

CREATE TABLE IF NOT EXISTS worker_status (
    hostname TEXT PRIMARY KEY,
    last_seen REAL,
    status TEXT,
    current_task_id INTEGER,
    gpu_name TEXT,
    gpu_util_pct REAL,
    gpu_temp_c REAL,
    gpu_memory_used_mb REAL,
    gpu_memory_total_mb REAL,
    cpu_util_pct REAL,
    ram_used_gb REAL,
    ram_total_gb REAL,
    disk_free_gb REAL,
    is_user_idle INTEGER,
    FOREIGN KEY (current_task_id) REFERENCES work_items(id)
);

CREATE TABLE IF NOT EXISTS event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    level TEXT NOT NULL,
    category TEXT,
    message TEXT NOT NULL,
    game_id TEXT,
    hostname TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_time ON event_log(timestamp DESC);
"""


class WorkQueue:
    """Pull-based work queue backed by SQLite."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self.db_path),
                timeout=30,  # wait up to 30s for lock (network share contention)
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.executescript(SCHEMA_SQL)
        return self._conn

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Enqueue (orchestrator writes)
    # ------------------------------------------------------------------

    def enqueue(
        self,
        task_type: str,
        *,
        game_id: str | None = None,
        priority: int = 50,
        target_machine: str | None = None,
        requires: dict | None = None,
        payload: dict | None = None,
        max_attempts: int = 3,
    ) -> int:
        """Add a work item to the queue. Returns the item ID."""
        conn = self._get_conn()
        cursor = conn.execute(
            """INSERT INTO work_items
               (task_type, game_id, priority, status, target_machine,
                requires, payload, created_at, max_attempts)
               VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?)""",
            (
                task_type,
                game_id,
                priority,
                target_machine,
                json.dumps(requires) if requires else None,
                json.dumps(payload) if payload else None,
                time.time(),
                max_attempts,
            ),
        )
        conn.commit()
        item_id = cursor.lastrowid
        logger.info(
            "Enqueued %s for %s (id=%d, priority=%d, target=%s)",
            task_type,
            game_id or "pipeline",
            item_id,
            priority,
            target_machine or "any",
        )
        return item_id

    def has_active_item(self, task_type: str, game_id: str | None = None) -> bool:
        """Check if a queued/claimed/running item exists for this task+game."""
        conn = self._get_conn()
        if game_id:
            row = conn.execute(
                """SELECT 1 FROM work_items
                   WHERE task_type = ? AND game_id = ?
                     AND status IN ('queued', 'claimed', 'running')
                   LIMIT 1""",
                (task_type, game_id),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT 1 FROM work_items
                   WHERE task_type = ? AND game_id IS NULL
                     AND status IN ('queued', 'claimed', 'running')
                   LIMIT 1""",
                (task_type,),
            ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # Claim (worker pulls)
    # ------------------------------------------------------------------

    def claim(
        self,
        capabilities: list[str],
        hostname: str,
    ) -> dict | None:
        """Atomically claim the highest-priority item this worker can handle.

        Returns the claimed item as a dict, or None if nothing available.
        """
        conn = self._get_conn()
        now = time.time()

        # Build placeholders for capabilities
        placeholders = ",".join("?" for _ in capabilities)

        # Atomic claim: SELECT + UPDATE in one transaction
        # The re-check on status='queued' prevents races between workers.
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"""SELECT id FROM work_items
                    WHERE status = 'queued'
                      AND (target_machine IS NULL OR target_machine = ?)
                      AND task_type IN ({placeholders})
                    ORDER BY priority ASC, created_at ASC
                    LIMIT 1""",
                (hostname, *capabilities),
            ).fetchone()

            if row is None:
                conn.execute("ROLLBACK")
                return None

            item_id = row["id"]
            conn.execute(
                """UPDATE work_items
                   SET status = 'claimed', claimed_at = ?, claimed_by = ?,
                       heartbeat_at = ?, attempts = attempts + 1
                   WHERE id = ? AND status = 'queued'""",
                (now, hostname, now, item_id),
            )
            conn.commit()
        except sqlite3.OperationalError:
            # Lock contention — another worker got it, try again next loop
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            return None

        # Re-read the full item
        item = conn.execute(
            "SELECT * FROM work_items WHERE id = ?", (item_id,)
        ).fetchone()

        if item is None:
            return None

        result = dict(item)
        # Parse JSON fields
        for json_field in ("requires", "payload", "result"):
            if result.get(json_field):
                try:
                    result[json_field] = json.loads(result[json_field])
                except (json.JSONDecodeError, TypeError):
                    pass

        logger.info(
            "%s claimed %s for %s (id=%d, attempt %d/%d)",
            hostname,
            result["task_type"],
            result.get("game_id") or "pipeline",
            item_id,
            result["attempts"],
            result["max_attempts"],
        )
        return result

    # ------------------------------------------------------------------
    # Worker lifecycle
    # ------------------------------------------------------------------

    def start(self, item_id: int):
        """Mark item as actively running (after pull-local setup)."""
        conn = self._get_conn()
        now = time.time()
        conn.execute(
            "UPDATE work_items SET status = 'running', started_at = ?, heartbeat_at = ? WHERE id = ?",
            (now, now, item_id),
        )
        conn.commit()

    def heartbeat(self, item_id: int):
        """Update heartbeat timestamp to prove worker is still alive."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE work_items SET heartbeat_at = ? WHERE id = ?",
            (time.time(), item_id),
        )
        conn.commit()

    def complete(self, item_id: int, *, result: dict | None = None):
        """Mark item as successfully completed."""
        conn = self._get_conn()
        now = time.time()
        conn.execute(
            """UPDATE work_items
               SET status = 'done', completed_at = ?,
                   result = ?
               WHERE id = ?""",
            (now, json.dumps(result) if result else None, item_id),
        )
        conn.commit()
        logger.info("Item %d completed", item_id)

    def fail(self, item_id: int, error: str):
        """Mark item as failed. Will be retried if under max_attempts."""
        conn = self._get_conn()
        now = time.time()
        # Check if we should retry or permanently fail
        row = conn.execute(
            "SELECT attempts, max_attempts FROM work_items WHERE id = ?",
            (item_id,),
        ).fetchone()
        if row and row["attempts"] < row["max_attempts"]:
            # Re-queue for retry
            conn.execute(
                """UPDATE work_items
                   SET status = 'queued', claimed_by = NULL, claimed_at = NULL,
                       heartbeat_at = NULL, error = ?
                   WHERE id = ?""",
                (error, item_id),
            )
            logger.warning("Item %d failed (attempt %d/%d), re-queued: %s",
                           item_id, row["attempts"], row["max_attempts"], error)
        else:
            conn.execute(
                """UPDATE work_items
                   SET status = 'failed', completed_at = ?, error = ?
                   WHERE id = ?""",
                (now, error, item_id),
            )
            logger.error("Item %d permanently failed: %s", item_id, error)
        conn.commit()

    # ------------------------------------------------------------------
    # Stale detection (orchestrator runs)
    # ------------------------------------------------------------------

    def reclaim_stale(self, timeout: int = 7200) -> list[dict]:
        """Find items with stale heartbeats and re-queue or fail them.

        Returns list of affected items.
        """
        conn = self._get_conn()
        cutoff = time.time() - timeout
        stale = conn.execute(
            """SELECT id, task_type, game_id, attempts, max_attempts, claimed_by
               FROM work_items
               WHERE status IN ('claimed', 'running')
                 AND heartbeat_at < ?""",
            (cutoff,),
        ).fetchall()

        reclaimed = []
        for row in stale:
            row = dict(row)
            if row["attempts"] < row["max_attempts"]:
                conn.execute(
                    """UPDATE work_items
                       SET status = 'queued', claimed_by = NULL, claimed_at = NULL,
                           heartbeat_at = NULL,
                           error = ?
                       WHERE id = ?""",
                    (f"stale heartbeat - worker {row['claimed_by']} presumed dead", row["id"]),
                )
                logger.warning(
                    "Reclaimed stale item %d (%s %s) from %s",
                    row["id"], row["task_type"], row.get("game_id", ""), row["claimed_by"],
                )
            else:
                conn.execute(
                    """UPDATE work_items
                       SET status = 'failed', completed_at = ?,
                           error = ?
                       WHERE id = ?""",
                    (time.time(), f"exhausted {row['max_attempts']} attempts", row["id"]),
                )
                logger.error(
                    "Item %d permanently failed after %d attempts",
                    row["id"], row["max_attempts"],
                )
            reclaimed.append(row)

        if reclaimed:
            conn.commit()
        return reclaimed

    # ------------------------------------------------------------------
    # Worker status
    # ------------------------------------------------------------------

    def update_worker_status(
        self,
        hostname: str,
        *,
        status: str = "idle",
        current_task_id: int | None = None,
        gpu_name: str | None = None,
        gpu_util_pct: float | None = None,
        gpu_temp_c: float | None = None,
        gpu_memory_used_mb: float | None = None,
        gpu_memory_total_mb: float | None = None,
        cpu_util_pct: float | None = None,
        ram_used_gb: float | None = None,
        ram_total_gb: float | None = None,
        disk_free_gb: float | None = None,
        is_user_idle: bool = True,
    ):
        """Upsert worker resource status."""
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO worker_status
               (hostname, last_seen, status, current_task_id,
                gpu_name, gpu_util_pct, gpu_temp_c,
                gpu_memory_used_mb, gpu_memory_total_mb,
                cpu_util_pct, ram_used_gb, ram_total_gb,
                disk_free_gb, is_user_idle)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(hostname) DO UPDATE SET
                   last_seen=excluded.last_seen,
                   status=excluded.status,
                   current_task_id=excluded.current_task_id,
                   gpu_name=excluded.gpu_name,
                   gpu_util_pct=excluded.gpu_util_pct,
                   gpu_temp_c=excluded.gpu_temp_c,
                   gpu_memory_used_mb=excluded.gpu_memory_used_mb,
                   gpu_memory_total_mb=excluded.gpu_memory_total_mb,
                   cpu_util_pct=excluded.cpu_util_pct,
                   ram_used_gb=excluded.ram_used_gb,
                   ram_total_gb=excluded.ram_total_gb,
                   disk_free_gb=excluded.disk_free_gb,
                   is_user_idle=excluded.is_user_idle""",
            (
                hostname, time.time(), status, current_task_id,
                gpu_name, gpu_util_pct, gpu_temp_c,
                gpu_memory_used_mb, gpu_memory_total_mb,
                cpu_util_pct, ram_used_gb, ram_total_gb,
                disk_free_gb, 1 if is_user_idle else 0,
            ),
        )
        conn.commit()

    def get_worker_status(self, hostname: str | None = None) -> list[dict]:
        """Get worker status for one or all workers."""
        conn = self._get_conn()
        if hostname:
            rows = conn.execute(
                "SELECT * FROM worker_status WHERE hostname = ?", (hostname,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM worker_status").fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Event log
    # ------------------------------------------------------------------

    def log_event(
        self,
        level: str,
        message: str,
        *,
        category: str | None = None,
        game_id: str | None = None,
        hostname: str | None = None,
    ):
        """Write a pipeline event for the status dashboard."""
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO event_log (timestamp, level, category, message, game_id, hostname)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (time.time(), level, category, message, game_id, hostname),
        )
        conn.commit()

    def get_recent_events(self, limit: int = 20) -> list[dict]:
        """Get the most recent pipeline events."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM event_log ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def get_queue_stats(self) -> dict:
        """Get counts by status."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM work_items GROUP BY status"
        ).fetchall()
        return {r["status"]: r["cnt"] for r in rows}

    def get_items(
        self,
        *,
        status: str | None = None,
        task_type: str | None = None,
        game_id: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Query work items with optional filters."""
        conn = self._get_conn()
        conditions = []
        params = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if task_type:
            conditions.append("task_type = ?")
            params.append(task_type)
        if game_id:
            conditions.append("game_id = ?")
            params.append(game_id)

        where = " AND ".join(conditions) if conditions else "1=1"
        rows = conn.execute(
            f"SELECT * FROM work_items WHERE {where} ORDER BY priority ASC, created_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_failed_items(self) -> list[dict]:
        """Get all permanently failed items."""
        return self.get_items(status="failed")
