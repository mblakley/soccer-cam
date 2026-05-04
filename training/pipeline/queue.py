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
    error TEXT,
    failed_workers TEXT DEFAULT ''
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
        # Only create parent dirs for local paths (not UNC shares)
        if not str(self.db_path).startswith("\\\\"):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._last_checkpoint: float = 0.0

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self.db_path),
                timeout=30,
                check_same_thread=False,  # API + orchestrator share connection from different threads
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
            # Checkpoint WAL every 100 pages (~400KB) instead of default 1000
            self._conn.execute("PRAGMA wal_autocheckpoint=100")
            self._conn.executescript(SCHEMA_SQL)
            self._migrate(self._conn)
            self._verify_integrity()
        return self._conn

    @staticmethod
    def _migrate(conn):
        """Add columns that may not exist in older DBs."""
        cols = {r[1] for r in conn.execute("PRAGMA table_info(work_items)").fetchall()}
        if "failed_workers" not in cols:
            conn.execute(
                "ALTER TABLE work_items ADD COLUMN failed_workers TEXT DEFAULT ''"
            )
            conn.commit()

    def _verify_integrity(self):
        """Check DB integrity on first connection. Rebuild if corrupt."""
        try:
            result = self._conn.execute("PRAGMA integrity_check").fetchone()
            if result[0] != "ok":
                logger.error(
                    "Database integrity check FAILED: %s — attempting recovery",
                    result[0],
                )
                self._rebuild_from_dump()
        except sqlite3.DatabaseError as e:
            logger.error("Database corrupt: %s — attempting recovery", e)
            self._rebuild_from_dump()

    def _rebuild_from_dump(self):
        """Rebuild the database from SQL dump to fix corruption."""
        import shutil

        self._conn.close()
        self._conn = None

        backup = self.db_path.with_suffix(".db.corrupt")
        new_path = self.db_path.with_suffix(".db.rebuilt")

        # Remove stale WAL/SHM
        for ext in ["-wal", "-shm"]:
            p = Path(str(self.db_path) + ext)
            if p.exists():
                p.unlink()

        old_conn = sqlite3.connect(str(self.db_path))
        new_conn = sqlite3.connect(str(new_path))
        recovered = 0
        try:
            for line in old_conn.iterdump():
                try:
                    new_conn.execute(line)
                    recovered += 1
                except sqlite3.Error:
                    pass  # skip duplicate/constraint violations
            new_conn.commit()
        except Exception as e:
            logger.error("Recovery dump failed: %s — starting fresh", e)
            new_conn.close()
            new_path.unlink(missing_ok=True)
            new_conn = sqlite3.connect(str(new_path))
            new_conn.executescript(SCHEMA_SQL)
            new_conn.commit()
        finally:
            old_conn.close()
            new_conn.close()

        shutil.move(str(self.db_path), str(backup))
        shutil.move(str(new_path), str(self.db_path))
        logger.info(
            "Rebuilt %s (%d statements recovered, backup at %s)",
            self.db_path.name,
            recovered,
            backup.name,
        )

        # Re-open the clean DB
        self._conn = sqlite3.connect(
            str(self.db_path), timeout=30, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA wal_autocheckpoint=100")

    def checkpoint(self):
        """Force a WAL checkpoint to merge WAL into main DB file."""
        conn = self._get_conn()
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        self._last_checkpoint = time.time()

    def maybe_checkpoint(self):
        """Checkpoint if more than 5 minutes since last one."""
        if time.time() - self._last_checkpoint > 300:
            self.checkpoint()

    def close(self):
        if self._conn:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
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
        """Add a work item to the queue. Returns the item ID.

        Skips if an identical item (same task_type + game_id) is already
        queued/claimed/running — prevents duplicate work items.
        """
        conn = self._get_conn()

        # Dedup: don't enqueue if already active
        if game_id:
            existing = conn.execute(
                """SELECT id FROM work_items
                   WHERE task_type = ? AND game_id = ?
                     AND status IN ('queued', 'claimed', 'running')
                   LIMIT 1""",
                (task_type, game_id),
            ).fetchone()
        else:
            existing = conn.execute(
                """SELECT id FROM work_items
                   WHERE task_type = ?
                     AND status IN ('queued', 'claimed', 'running')
                   LIMIT 1""",
                (task_type,),
            ).fetchone()

        if existing:
            logger.debug(
                "Skipped duplicate enqueue: %s for %s (existing id=%d)",
                task_type,
                game_id or "pipeline",
                existing[0],
            )
            return existing[0]

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
        """Check if a queued/claimed/running item exists for this task type.

        If game_id is provided, checks for that specific game.
        If game_id is None, checks for ANY active item of this task type.
        """
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
                   WHERE task_type = ?
                     AND status IN ('queued', 'claimed', 'running')
                   LIMIT 1""",
                (task_type,),
            ).fetchone()
        return row is not None

    def count_active(self, task_types: list[str] | None = None) -> int:
        """Count queued + claimed + running items, optionally filtered by task types."""
        conn = self._get_conn()
        if task_types:
            placeholders = ",".join("?" for _ in task_types)
            row = conn.execute(
                f"""SELECT COUNT(*) FROM work_items
                   WHERE status IN ('queued', 'claimed', 'running')
                     AND task_type IN ({placeholders})""",
                task_types,
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT COUNT(*) FROM work_items
                   WHERE status IN ('queued', 'claimed', 'running')"""
            ).fetchone()
        return row[0] if row else 0

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
                      AND (failed_workers = '' OR failed_workers NOT LIKE ?)
                    ORDER BY priority ASC, created_at ASC
                    LIMIT 1""",
                (hostname, *capabilities, f"%{hostname}%"),
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
        """Mark item as failed. Will be retried if under max_attempts.

        If max_attempts exhausted, marks item as failed and creates a fresh
        queue item for the same game+task so work is never silently dropped.
        Tracks which workers failed so the task avoids them on retry.
        """
        conn = self._get_conn()
        now = time.time()
        # Check if we should retry or permanently fail
        row = dict(
            conn.execute(
                "SELECT task_type, game_id, priority, attempts, max_attempts, payload, "
                "claimed_by, failed_workers "
                "FROM work_items WHERE id = ?",
                (item_id,),
            ).fetchone()
            or {}
        )
        if not row:
            return

        # Track which worker failed
        failed_workers = row.get("failed_workers", "") or ""
        worker = row.get("claimed_by", "")
        if worker and worker not in failed_workers:
            failed_workers = f"{failed_workers},{worker}" if failed_workers else worker

        if row["attempts"] < row["max_attempts"]:
            # Re-queue for retry, avoiding workers that already failed
            conn.execute(
                """UPDATE work_items
                   SET status = 'queued', claimed_by = NULL, claimed_at = NULL,
                       heartbeat_at = NULL, error = ?, failed_workers = ?
                   WHERE id = ?""",
                (error, failed_workers, item_id),
            )
            logger.warning(
                "Item %d failed (attempt %d/%d), re-queued: %s",
                item_id,
                row["attempts"],
                row["max_attempts"],
                error,
            )
        else:
            conn.execute(
                """UPDATE work_items
                   SET status = 'failed', completed_at = ?, error = ?
                   WHERE id = ?""",
                (now, error, item_id),
            )
            # Create a fresh item so the work isn't silently dropped
            if row.get("game_id"):
                self._reenqueue_fresh(conn, row, error)
            logger.error("Item %d permanently failed: %s", item_id, error)
        conn.commit()

    def _reenqueue_fresh(self, conn, old_item: dict, error: str):
        """Create a fresh queue item after permanent failure, or move to dead queue.

        Tracks total lifetime attempts across all re-enqueues. If the task has
        been re-enqueued too many times (total attempts >= max_attempts * max_reenqueues),
        it goes to 'dead' status instead — requiring manual intervention.
        """
        max_reenqueues = (
            3  # max times we'll create a fresh item (so 3 * 3 = 9 total attempts)
        )
        task_type = old_item["task_type"]
        game_id = old_item["game_id"]

        # Count how many times this game+task has already been enqueued (any status)
        total_items = conn.execute(
            "SELECT COUNT(*) FROM work_items WHERE task_type = ? AND game_id = ?",
            (task_type, game_id),
        ).fetchone()[0]

        if total_items > max_reenqueues:
            # Too many cycles — move to dead status
            conn.execute(
                """UPDATE work_items
                   SET status = 'dead', error = ?
                   WHERE id = ?""",
                (
                    f"dead after {total_items} enqueue cycles: {error}",
                    old_item.get("id", 0),
                ),
            )
            logger.error(
                "Task %s for %s is DEAD after %d enqueue cycles — needs manual intervention",
                task_type,
                game_id,
                total_items,
            )
            return

        # Carry over failed_workers so the fresh item avoids the same workers
        failed_workers = old_item.get("failed_workers", "") or ""
        worker = old_item.get("claimed_by", "")
        if worker and worker not in failed_workers:
            failed_workers = f"{failed_workers},{worker}" if failed_workers else worker

        conn.execute(
            """INSERT INTO work_items
               (task_type, game_id, priority, status, payload, created_at,
                attempts, max_attempts, failed_workers)
               VALUES (?, ?, ?, 'queued', ?, ?, 0, ?, ?)""",
            (
                task_type,
                game_id,
                old_item.get("priority", 50),
                old_item.get("payload"),
                time.time(),
                old_item.get("max_attempts", 3),
                failed_workers,
            ),
        )
        logger.info(
            "Re-enqueued fresh %s for %s (cycle %d/%d)",
            task_type,
            game_id,
            total_items,
            max_reenqueues,
        )

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
                    (
                        f"stale heartbeat - worker {row['claimed_by']} presumed dead",
                        row["id"],
                    ),
                )
                logger.warning(
                    "Reclaimed stale item %d (%s %s) from %s",
                    row["id"],
                    row["task_type"],
                    row.get("game_id", ""),
                    row["claimed_by"],
                )
            else:
                conn.execute(
                    """UPDATE work_items
                       SET status = 'failed', completed_at = ?,
                           error = ?
                       WHERE id = ?""",
                    (
                        time.time(),
                        f"exhausted {row['max_attempts']} attempts",
                        row["id"],
                    ),
                )
                if row.get("game_id"):
                    self._reenqueue_fresh(
                        conn, row, "stale heartbeat exhausted attempts"
                    )
                logger.error(
                    "Item %d permanently failed after %d attempts",
                    row["id"],
                    row["max_attempts"],
                )
            reclaimed.append(row)

        if reclaimed:
            conn.commit()
        return reclaimed

    def release_worker_tasks(self, hostname: str) -> int:
        """Fail all running/claimed tasks held by a worker.

        Called on worker startup to release tasks orphaned by a previous
        instance that was killed. Returns count of released items.
        """
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT id, task_type, game_id, attempts, max_attempts
               FROM work_items
               WHERE claimed_by = ? AND status IN ('claimed', 'running')""",
            (hostname,),
        ).fetchall()

        for row in rows:
            row = dict(row)
            error = f"released on worker {hostname} startup (previous instance died)"
            if row["attempts"] < row["max_attempts"]:
                conn.execute(
                    """UPDATE work_items
                       SET status = 'queued', claimed_by = NULL, claimed_at = NULL,
                           heartbeat_at = NULL, error = ?
                       WHERE id = ?""",
                    (error, row["id"]),
                )
                logger.info(
                    "Released orphan %d (%s %s) from %s — re-queued",
                    row["id"],
                    row["task_type"],
                    row.get("game_id", ""),
                    hostname,
                )
            else:
                conn.execute(
                    """UPDATE work_items
                       SET status = 'failed', completed_at = ?, error = ?
                       WHERE id = ?""",
                    (time.time(), error, row["id"]),
                )
                if row.get("game_id"):
                    self._reenqueue_fresh(conn, row, error)
                logger.info(
                    "Released orphan %d (%s %s) from %s — exhausted attempts, re-enqueued fresh",
                    row["id"],
                    row["task_type"],
                    row.get("game_id", ""),
                    hostname,
                )

        if rows:
            conn.commit()
        return len(rows)

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
                hostname,
                time.time(),
                status,
                current_task_id,
                gpu_name,
                gpu_util_pct,
                gpu_temp_c,
                gpu_memory_used_mb,
                gpu_memory_total_mb,
                cpu_util_pct,
                ram_used_gb,
                ram_total_gb,
                disk_free_gb,
                1 if is_user_idle else 0,
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

    def get_recent_events(self, limit: int = 50) -> list[dict]:
        """Get the most recent pipeline events."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM event_log ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_events(
        self,
        since: float | None = None,
        until: float | None = None,
        category: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        """Query pipeline events by time range and/or category."""
        conn = self._get_conn()
        query = "SELECT * FROM event_log WHERE 1=1"
        params: list = []
        if since:
            query += " AND timestamp >= ?"
            params.append(since)
        if until:
            query += " AND timestamp <= ?"
            params.append(until)
        if category:
            query += " AND category = ?"
            params.append(category)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
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
