"""Pipeline API — HTTP interface to WorkQueue and GameRegistry.

Runs inside the orchestrator process. Workers on any machine talk to it
over HTTP instead of accessing SQLite directly over SMB.

Endpoints:
    POST /api/claim              — worker claims next task
    POST /api/start/{id}         — mark task as running
    POST /api/heartbeat/{id}     — worker heartbeat
    POST /api/complete/{id}      — mark task done
    POST /api/fail/{id}          — mark task failed
    POST /api/worker-status      — report machine resources
    GET  /api/status             — full pipeline dashboard
    GET  /api/games              — all games with states
    GET  /api/game/{game_id}     — single game detail + file paths
    GET  /api/queue              — current work items
"""

import logging
import threading
import time

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from starlette.responses import Response
from pydantic import BaseModel

from training.pipeline.config import load_config
from training.pipeline.queue import WorkQueue
from training.pipeline.registry import GameRegistry

logger = logging.getLogger(__name__)

app = FastAPI(title="Pipeline API", version="1.0.0")

# Paths get set by start_api(); API creates its own DB connections per-request
_queue_db_path: str = ""
_registry_db_path: str = ""
_cfg = None


def init_app(queue: WorkQueue, registry: GameRegistry, cfg=None):
    """Store DB paths so the API can create its own connections (thread-safe)."""
    global _queue_db_path, _registry_db_path, _cfg
    _queue_db_path = str(queue.db_path)
    _registry_db_path = str(registry.db_path)
    _cfg = cfg


def _get_queue() -> WorkQueue:
    return WorkQueue(_queue_db_path)


def _get_registry() -> GameRegistry:
    return GameRegistry(_registry_db_path)


# --- Request models ---


class ClaimRequest(BaseModel):
    capabilities: list[str]
    hostname: str


class CompleteRequest(BaseModel):
    result: dict | None = None


class FailRequest(BaseModel):
    error: str


class WorkerStatusRequest(BaseModel):
    hostname: str
    status: str = "idle"
    current_task_id: int | None = None
    gpu_name: str | None = None
    gpu_util_pct: float | None = None
    gpu_temp_c: float | None = None
    gpu_memory_used_mb: float | None = None
    gpu_memory_total_mb: float | None = None
    cpu_util_pct: float | None = None
    ram_used_gb: float | None = None
    ram_total_gb: float | None = None
    disk_free_gb: float | None = None
    is_user_idle: bool = True


# --- Queue endpoints ---


@app.post("/api/claim")
def claim(req: ClaimRequest):
    item = _get_queue().claim(req.capabilities, req.hostname)
    if item is None:
        return Response(status_code=204)
    return item


@app.post("/api/start/{item_id}")
def start(item_id: int):
    _get_queue().start(item_id)
    return {"ok": True}


@app.post("/api/heartbeat/{item_id}")
def heartbeat(item_id: int):
    _get_queue().heartbeat(item_id)
    return {"ok": True}


@app.post("/api/complete/{item_id}")
def complete(item_id: int, req: CompleteRequest):
    _get_queue().complete(item_id, result=req.result)
    return {"ok": True}


@app.post("/api/fail/{item_id}")
def fail(item_id: int, req: FailRequest):
    _get_queue().fail(item_id, req.error)
    return {"ok": True}


# --- Status endpoints ---


@app.post("/api/worker-status")
def worker_status(req: WorkerStatusRequest):
    _get_queue().update_worker_status(
        req.hostname,
        status=req.status,
        current_task_id=req.current_task_id,
        gpu_name=req.gpu_name,
        gpu_util_pct=req.gpu_util_pct,
        gpu_temp_c=req.gpu_temp_c,
        gpu_memory_used_mb=req.gpu_memory_used_mb,
        gpu_memory_total_mb=req.gpu_memory_total_mb,
        cpu_util_pct=req.cpu_util_pct,
        ram_used_gb=req.ram_used_gb,
        ram_total_gb=req.ram_total_gb,
        disk_free_gb=req.disk_free_gb,
        is_user_idle=req.is_user_idle,
    )
    return {"ok": True}


@app.get("/api/status")
def status():
    q = _get_queue()
    workers = q.get_worker_status()
    queue_stats = q.get_queue_stats()
    state_counts = _get_registry().get_state_counts()
    events = q.get_recent_events(limit=50)
    return {
        "workers": workers,
        "queue": queue_stats,
        "games": state_counts,
        "events": events,
    }


@app.get("/api/events")
def get_events(
    since: float | None = None,
    until: float | None = None,
    category: str | None = None,
    limit: int = 200,
):
    """Query pipeline events by time range and/or category.

    Usage:
        /api/events?since=1775800000&until=1775900000
        /api/events?category=state_change&limit=50
        /api/events?since=1775800000  (everything since timestamp)
    """
    return _get_queue().get_events(
        since=since, until=until, category=category, limit=limit
    )


@app.get("/api/games")
def games():
    return _get_registry().get_all_games()


@app.get("/api/game/{game_id}")
def game_detail(game_id: str):
    game = _get_registry().get_game(game_id)
    if not game:
        raise HTTPException(404, f"Game not found: {game_id}")

    # Add file share paths for SMB copy
    if _cfg:
        games_dir = _cfg.paths.games_dir
        game["packs_share"] = (
            f"{_cfg.server.share_training}\\games\\{game_id}\\tile_packs"
        )
        game["packs_local"] = f"{games_dir}\\{game_id}\\tile_packs"
        if game.get("video_path"):
            # Convert F: path to video share path
            vpath = game["video_path"]
            if vpath.startswith("F:"):
                game["video_share"] = vpath.replace("F:", _cfg.server.share_video, 1)
            elif vpath.startswith("F:/"):
                game["video_share"] = vpath.replace(
                    "F:/", _cfg.server.share_video + "/", 1
                )
            else:
                game["video_share"] = vpath
    return game


@app.get("/api/queue")
def queue_items(status: str | None = None, limit: int = 50):
    return _get_queue().get_items(status=status, limit=limit)


# --- Orchestrator endpoints (used by orchestrator loop via API) ---


class EnqueueRequest(BaseModel):
    task_type: str
    game_id: str | None = None
    priority: int = 50
    target_machine: str | None = None
    payload: dict | None = None
    max_attempts: int = 3


class SetStateRequest(BaseModel):
    state: str
    error: str | None = None


@app.post("/api/enqueue")
def enqueue(req: EnqueueRequest):
    item_id = _get_queue().enqueue(
        req.task_type,
        game_id=req.game_id,
        priority=req.priority,
        target_machine=req.target_machine,
        payload=req.payload,
        max_attempts=req.max_attempts,
    )
    return {"id": item_id}


@app.post("/api/archive/{item_id}")
def archive_item(item_id: int):
    """Move a done or failed item to archived status so it's not re-processed."""
    q = _get_queue()
    conn = q._get_conn()
    conn.execute(
        "UPDATE work_items SET status = 'archived' WHERE id = ? AND status IN ('done', 'failed')",
        (item_id,),
    )
    conn.commit()
    return {"ok": True}


@app.delete("/api/workers/{hostname}")
def delete_worker(hostname: str):
    """Remove stale worker status entries.

    If duplicate rows exist (WAL corruption), deletes the oldest one
    by last_seen timestamp, keeping the freshest.
    """
    q = _get_queue()
    conn = q._get_conn()
    # Count entries for this hostname
    count = conn.execute(
        "SELECT COUNT(*) FROM worker_status WHERE hostname = ?", (hostname,)
    ).fetchone()[0]
    if count > 1:
        # Keep only the freshest entry
        conn.execute(
            """DELETE FROM worker_status WHERE hostname = ? AND rowid NOT IN (
                SELECT rowid FROM worker_status WHERE hostname = ?
                ORDER BY last_seen DESC LIMIT 1
            )""",
            (hostname, hostname),
        )
    else:
        conn.execute("DELETE FROM worker_status WHERE hostname = ?", (hostname,))
    conn.commit()
    return {"ok": True, "hostname": hostname, "deleted": count}


@app.post("/api/maintenance/checkpoint")
def checkpoint():
    """Force WAL checkpoint on both databases."""
    results = {}
    for name, db in [("queue", _get_queue()), ("registry", _get_registry())]:
        try:
            conn = db._get_conn()
            r = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            results[name] = {"busy": r[0], "log": r[1], "checkpointed": r[2]}
        except Exception as e:
            results[name] = {"error": str(e)}
    return {"ok": True, **results}


@app.post("/api/maintenance/rebuild-workers")
def rebuild_workers():
    """Rebuild worker_status table to fix duplicate rows from WAL corruption."""
    q = _get_queue()
    conn = q._get_conn()
    # Save fresh entries (most recent per hostname)
    rows = conn.execute(
        """SELECT hostname, MAX(last_seen) as last_seen, status,
                  current_task_id, gpu_name, gpu_util_pct, gpu_temp_c,
                  gpu_memory_used_mb, gpu_memory_total_mb, cpu_util_pct,
                  ram_used_gb, ram_total_gb, disk_free_gb, is_user_idle
           FROM worker_status GROUP BY hostname"""
    ).fetchall()
    # Drop and recreate (without foreign key — task_ids may be archived)
    conn.execute("DROP TABLE IF EXISTS worker_status")
    conn.execute("""CREATE TABLE worker_status (
        hostname TEXT PRIMARY KEY,
        last_seen REAL, status TEXT, current_task_id INTEGER,
        gpu_name TEXT, gpu_util_pct REAL, gpu_temp_c REAL,
        gpu_memory_used_mb REAL, gpu_memory_total_mb REAL,
        cpu_util_pct REAL, ram_used_gb REAL, ram_total_gb REAL,
        disk_free_gb REAL, is_user_idle INTEGER
    )""")
    # Re-insert deduplicated rows
    for r in rows:
        conn.execute(
            """INSERT INTO worker_status VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(r),
        )
    conn.commit()
    return {"ok": True, "workers_kept": len(rows)}


@app.patch("/api/queue/{item_id}/priority")
def update_priority(item_id: int, req: dict):
    priority = req.get("priority")
    if priority is None:
        raise HTTPException(400, "Missing 'priority'")
    q = _get_queue()
    conn = q._get_conn()
    conn.execute("UPDATE work_items SET priority = ? WHERE id = ?", (priority, item_id))
    conn.commit()
    return {"ok": True, "id": item_id, "priority": priority}


@app.delete("/api/queue/{item_id}")
def delete_queue_item(item_id: int):
    q = _get_queue()
    conn = q._get_conn()
    row = conn.execute(
        "SELECT status FROM work_items WHERE id = ?", (item_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "Item not found")
    if row["status"] == "running":
        raise HTTPException(409, "Cannot delete a running item — fail it first")
    conn.execute("DELETE FROM work_items WHERE id = ?", (item_id,))
    conn.commit()
    return {"ok": True, "id": item_id, "deleted": True}


@app.get("/api/has-active/{task_type}")
def has_active(task_type: str, game_id: str | None = None):
    return {"active": _get_queue().has_active_item(task_type, game_id)}


@app.get("/api/queue-depth")
def queue_depth(task_types: str | None = None):
    """Count active (queued + claimed + running) items.

    Optional task_types parameter: comma-separated list to filter by.
    """
    types = task_types.split(",") if task_types else None
    return {"depth": _get_queue().count_active(types)}


@app.post("/api/reclaim-stale")
def reclaim_stale(timeout: int = 7200):
    reclaimed = _get_queue().reclaim_stale(timeout)
    return {"reclaimed": len(reclaimed), "items": reclaimed}


@app.post("/api/release-worker/{hostname}")
def release_worker_tasks(hostname: str):
    """Release all tasks held by a worker (called on worker startup)."""
    count = _get_queue().release_worker_tasks(hostname)
    return {"ok": True, "released": count}


@app.post("/api/game/{game_id}/state")
def set_game_state(game_id: str, req: SetStateRequest):
    _get_registry().set_state(game_id, req.state, error=req.error)
    return {"ok": True}


@app.post("/api/game/{game_id}/reset-attempts")
def reset_attempts(game_id: str):
    _get_registry().reset_attempts(game_id)
    return {"ok": True}


@app.post("/api/game/{game_id}/increment-attempts")
def increment_attempts(game_id: str):
    _get_registry().increment_attempts(game_id)
    return {"ok": True}


@app.post("/api/game/{game_id}/stats")
def update_stats(game_id: str, stats: dict):
    _get_registry().update_stats(game_id, **stats)
    return {"ok": True}


@app.get("/api/games/needing-work")
def games_needing_work():
    return _get_registry().get_games_needing_work()


@app.get("/api/games/trainable")
def trainable_games():
    return _get_registry().get_trainable_games()


@app.get("/api/state-counts")
def state_counts():
    return _get_registry().get_state_counts()


@app.post("/api/log-event")
def log_event(event: dict):
    _get_queue().log_event(
        event.get("level", "info"),
        event.get("message", ""),
        category=event.get("category"),
        game_id=event.get("game_id"),
        hostname=event.get("hostname"),
    )
    return {"ok": True}


# --- Server management ---


def start_api(queue: WorkQueue, registry: GameRegistry, cfg=None, port: int = 8643):
    """Start the API server in a background thread."""
    init_app(queue, registry, cfg)

    def _run():
        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        server.run()

    thread = threading.Thread(target=_run, daemon=True, name="pipeline-api")
    thread.start()
    logger.info("Pipeline API started on port %d", port)
    return thread
