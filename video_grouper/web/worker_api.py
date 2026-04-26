"""Master-side worker coordination API at ``/api/work/*``.

Workers register here, then poll for tasks, send heartbeats, and report
results. The master persists a worker registry plus per-task state in
``shared_data/workers/`` so it survives a service restart.

This is a v1 scaffold: registration + simple FIFO claim/complete/fail +
heartbeat tracking. File streaming for inputs/outputs is reserved
(GET/POST endpoints exist but the orchestrator-side queue → worker
work-item glue is a follow-up — that's where the design split between
"option 2: HTTP files" and "option 3: stage-process-return" gets made).
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    node_id: str  # client-chosen stable id (e.g. hostname)
    capabilities: list[str] = []
    version: Optional[str] = None


class RegisterResponse(BaseModel):
    node_id: str
    token: str  # bearer token for subsequent calls


class TaskOffer(BaseModel):
    task_id: str
    task_type: str  # 'combine' | 'trim' | 'ball_tracking' | etc.
    payload: dict


class HeartbeatResponse(BaseModel):
    ok: bool


class CompleteRequest(BaseModel):
    outputs: dict = {}


class FailRequest(BaseModel):
    error: str
    retry: bool = True


# ---------------------------------------------------------------------------
# In-memory registry (persisted to disk on each mutation)
# ---------------------------------------------------------------------------


def _registry_path(storage_path: str | Path) -> Path:
    return Path(storage_path) / "workers" / "registry.json"


def _load_registry(storage_path: str | Path) -> dict:
    p = _registry_path(storage_path)
    if not p.exists():
        return {"workers": {}, "tasks": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("WORKER_API: bad registry, resetting: %s", exc)
        return {"workers": {}, "tasks": {}}


def _save_registry(storage_path: str | Path, registry: dict) -> None:
    p = _registry_path(storage_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(registry, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def build_router(storage_path: str | Path) -> APIRouter:
    """Build the FastAPI router for the worker API.

    The host-allowlist + Origin/Referer middleware on the parent app
    is the perimeter; this router adds bearer-token auth on top so a
    rogue process on the same host can't claim work.
    """
    router = APIRouter(prefix="/api/work")

    def _require_token(authorization: str | None = Header(default=None)) -> str:
        """FastAPI dependency: parse ``Authorization: Bearer <token>``
        and return the worker's node_id, raising 401 if invalid.
        """
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        token = authorization.split(" ", 1)[1].strip()
        registry = _load_registry(storage_path)
        for node_id, info in registry["workers"].items():
            if info.get("token") == token:
                return node_id
        raise HTTPException(status_code=401, detail="invalid token")

    @router.post("/register", response_model=RegisterResponse)
    def register(req: RegisterRequest) -> RegisterResponse:
        registry = _load_registry(storage_path)
        existing = registry["workers"].get(req.node_id)
        if existing:
            # Re-registration (worker restart) — keep the existing token.
            token = existing["token"]
        else:
            token = secrets.token_urlsafe(32)
        registry["workers"][req.node_id] = {
            "token": token,
            "capabilities": req.capabilities,
            "version": req.version,
            "registered_at": datetime.now(timezone.utc).isoformat(),
            "last_heartbeat": time.time(),
        }
        _save_registry(storage_path, registry)
        logger.info("WORKER_API: registered node %s", req.node_id)
        return RegisterResponse(node_id=req.node_id, token=token)

    @router.get("/next")
    def next_task(node_id: str = Depends(_require_token)) -> JSONResponse:
        """Return one available task matching the worker's capabilities,
        or 204 if there's nothing to do."""
        registry = _load_registry(storage_path)
        worker = registry["workers"].get(node_id, {})
        capabilities = set(worker.get("capabilities", []))

        for task_id, task in registry.get("tasks", {}).items():
            if task.get("status") != "queued":
                continue
            if task.get("task_type") not in capabilities and capabilities:
                continue
            # Claim it
            task["status"] = "in_progress"
            task["assigned_to"] = node_id
            task["claimed_at"] = time.time()
            _save_registry(storage_path, registry)
            return JSONResponse(
                {
                    "task_id": task_id,
                    "task_type": task.get("task_type"),
                    "payload": task.get("payload", {}),
                }
            )

        return JSONResponse(content=None, status_code=204)

    @router.post("/{task_id}/heartbeat")
    def heartbeat(
        task_id: str, node_id: str = Depends(_require_token)
    ) -> HeartbeatResponse:
        registry = _load_registry(storage_path)
        task = registry.get("tasks", {}).get(task_id)
        if task is None or task.get("assigned_to") != node_id:
            raise HTTPException(status_code=404, detail="not your task")
        task["last_heartbeat"] = time.time()
        # Bump worker heartbeat too so the dashboard can show "online".
        if node_id in registry["workers"]:
            registry["workers"][node_id]["last_heartbeat"] = time.time()
        _save_registry(storage_path, registry)
        return HeartbeatResponse(ok=True)

    @router.post("/{task_id}/complete")
    def complete(
        task_id: str,
        req: CompleteRequest,
        node_id: str = Depends(_require_token),
    ) -> JSONResponse:
        registry = _load_registry(storage_path)
        task = registry.get("tasks", {}).get(task_id)
        if task is None or task.get("assigned_to") != node_id:
            raise HTTPException(status_code=404, detail="not your task")
        task["status"] = "complete"
        task["outputs"] = req.outputs
        task["completed_at"] = time.time()
        _save_registry(storage_path, registry)
        logger.info("WORKER_API: task %s completed by %s", task_id, node_id)
        return JSONResponse({"ok": True})

    @router.post("/{task_id}/fail")
    def fail_task(
        task_id: str,
        req: FailRequest,
        node_id: str = Depends(_require_token),
    ) -> JSONResponse:
        registry = _load_registry(storage_path)
        task = registry.get("tasks", {}).get(task_id)
        if task is None or task.get("assigned_to") != node_id:
            raise HTTPException(status_code=404, detail="not your task")
        attempts = task.get("attempts", 0) + 1
        task["attempts"] = attempts
        task["last_error"] = req.error
        if req.retry and attempts < 3:
            task["status"] = "queued"
            task["assigned_to"] = None
        else:
            task["status"] = "error"
        _save_registry(storage_path, registry)
        logger.warning(
            "WORKER_API: task %s failed by %s (attempt %d): %s",
            task_id,
            node_id,
            attempts,
            req.error,
        )
        return JSONResponse({"ok": True, "status": task["status"]})

    @router.get("/_workers")
    def list_workers(
        request: Request, node_id: str = Depends(_require_token)
    ) -> JSONResponse:
        """Read-only snapshot used by the dashboard (master-side only)."""
        registry = _load_registry(storage_path)
        return JSONResponse(
            {
                "workers": [
                    {
                        "node_id": nid,
                        "capabilities": info.get("capabilities", []),
                        "last_heartbeat": info.get("last_heartbeat"),
                        "version": info.get("version"),
                    }
                    for nid, info in registry.get("workers", {}).items()
                ]
            }
        )

    return router


# ---------------------------------------------------------------------------
# Helpers used by the orchestrator to enqueue work for remote workers.
# (Phase 4 v1: orchestrator code calls these to publish tasks; binding
# them to the actual queue processors is a follow-up.)
# ---------------------------------------------------------------------------


def enqueue_task(storage_path: str | Path, task_type: str, payload: dict) -> str:
    """Add a task to the master's queue. Returns the task_id."""
    registry = _load_registry(storage_path)
    task_id = secrets.token_urlsafe(12)
    registry.setdefault("tasks", {})[task_id] = {
        "task_id": task_id,
        "task_type": task_type,
        "payload": payload,
        "status": "queued",
        "assigned_to": None,
        "attempts": 0,
        "created_at": time.time(),
    }
    _save_registry(storage_path, registry)
    return task_id
