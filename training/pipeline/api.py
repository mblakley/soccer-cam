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
from pydantic import BaseModel

from training.pipeline.config import load_config
from training.pipeline.queue import WorkQueue
from training.pipeline.registry import GameRegistry

logger = logging.getLogger(__name__)

app = FastAPI(title="Pipeline API", version="1.0.0")

# These get set by start_api() before the server starts
_queue: WorkQueue | None = None
_registry: GameRegistry | None = None
_cfg = None


def init_app(queue: WorkQueue, registry: GameRegistry, cfg=None):
    """Initialize the API with shared WorkQueue and GameRegistry instances."""
    global _queue, _registry, _cfg
    _queue = queue
    _registry = registry
    _cfg = cfg


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
    item = _queue.claim(req.capabilities, req.hostname)
    if item is None:
        return JSONResponse(status_code=204, content=None)
    return item


@app.post("/api/start/{item_id}")
def start(item_id: int):
    _queue.start(item_id)
    return {"ok": True}


@app.post("/api/heartbeat/{item_id}")
def heartbeat(item_id: int):
    _queue.heartbeat(item_id)
    return {"ok": True}


@app.post("/api/complete/{item_id}")
def complete(item_id: int, req: CompleteRequest):
    _queue.complete(item_id, result=req.result)
    return {"ok": True}


@app.post("/api/fail/{item_id}")
def fail(item_id: int, req: FailRequest):
    _queue.fail(item_id, req.error)
    return {"ok": True}


# --- Status endpoints ---


@app.post("/api/worker-status")
def worker_status(req: WorkerStatusRequest):
    _queue.update_worker_status(
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
    workers = _queue.get_worker_status()
    queue_stats = _queue.get_queue_stats()
    state_counts = _registry.get_state_counts()
    events = _queue.get_recent_events(limit=20)
    return {
        "workers": workers,
        "queue": queue_stats,
        "games": state_counts,
        "events": events,
    }


@app.get("/api/games")
def games():
    return _registry.get_all_games()


@app.get("/api/game/{game_id}")
def game_detail(game_id: str):
    game = _registry.get_game(game_id)
    if not game:
        raise HTTPException(404, f"Game not found: {game_id}")

    # Add file share paths for SMB copy
    if _cfg:
        games_dir = _cfg.paths.games_dir
        game["packs_share"] = f"{_cfg.server.share_training}\\games\\{game_id}\\tile_packs"
        game["packs_local"] = f"{games_dir}\\{game_id}\\tile_packs"
        if game.get("video_path"):
            # Convert F: path to video share path
            vpath = game["video_path"]
            if vpath.startswith("F:"):
                game["video_share"] = vpath.replace("F:", _cfg.server.share_video, 1)
            elif vpath.startswith("F:/"):
                game["video_share"] = vpath.replace("F:/", _cfg.server.share_video + "/", 1)
            else:
                game["video_share"] = vpath
    return game


@app.get("/api/queue")
def queue_items(status: str | None = None, limit: int = 50):
    return _queue.get_items(status=status, limit=limit)


# --- Server management ---


def start_api(queue: WorkQueue, registry: GameRegistry, cfg=None, port: int = 8643):
    """Start the API server in a background thread."""
    init_app(queue, registry, cfg)

    def _run():
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=port,
            log_level="warning",
        )

    thread = threading.Thread(target=_run, daemon=True, name="pipeline-api")
    thread.start()
    logger.info("Pipeline API started on port %d", port)
    return thread
