"""Pipeline HTTP client for workers and orchestrator.

Uses httpx for reliable HTTP on Windows (stdlib urllib has known issues
with localhost connections in non-interactive contexts like Scheduled Tasks).

Usage:
    from training.pipeline.client import PipelineClient

    api = PipelineClient("http://192.168.86.152:8643")
    item = api.claim(["label", "tile"], "FORTNITE-OP")
    if item:
        api.start(item["id"])
        api.heartbeat(item["id"])
        api.complete(item["id"], {"labels": 500})
"""

import logging

import httpx

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "http://127.0.0.1:8643"
TIMEOUT = 10  # seconds — API calls should be instant


class PipelineClient:
    """HTTP client for the Pipeline API."""

    def __init__(self, base_url: str = DEFAULT_API_URL):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self.base_url, timeout=TIMEOUT)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        silent_404: bool = False,
    ) -> dict | list | None:
        """Make an API request. Returns parsed JSON or None on error."""
        try:
            r = self._client.request(method, path, json=json)
            if r.status_code == 204:
                return None
            if r.status_code == 404 and silent_404:
                return None
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            logger.warning(
                "API %s %s returned %d: %s",
                method,
                path,
                e.response.status_code,
                e.response.text[:200],
            )
            return None
        except Exception as e:
            logger.warning("API %s %s failed: %s", method, path, e)
            return None

    def _post(self, path: str, data: dict | None = None) -> dict | None:
        return self._request("POST", path, json=data or {})

    def _get(self, path: str) -> dict | list | None:
        return self._request("GET", path, silent_404=True)

    def _delete(self, path: str) -> dict | None:
        return self._request("DELETE", path)

    def _patch(self, path: str, data: dict | None = None) -> dict | None:
        return self._request("PATCH", path, json=data or {})

    # --- Queue operations ---

    def claim(self, capabilities: list[str], hostname: str) -> dict | None:
        """Claim the next available work item. Returns item dict or None."""
        return self._post(
            "/api/claim",
            {"capabilities": capabilities, "hostname": hostname},
        )

    def start(self, item_id: int):
        """Mark item as actively running."""
        self._post(f"/api/start/{item_id}")

    def heartbeat(self, item_id: int):
        """Send heartbeat for a running task."""
        self._post(f"/api/heartbeat/{item_id}")

    def complete(self, item_id: int, result: dict | None = None):
        """Mark task as successfully completed."""
        self._post(f"/api/complete/{item_id}", {"result": result})

    def fail(self, item_id: int, error: str):
        """Mark task as failed."""
        self._post(f"/api/fail/{item_id}", {"error": error})

    def archive(self, item_id: int):
        """Move a done item to archived status."""
        self._post(f"/api/archive/{item_id}")

    # --- Status ---

    def report_status(
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
        """Report worker resource status."""
        self._post(
            "/api/worker-status",
            {
                "hostname": hostname,
                "status": status,
                "current_task_id": current_task_id,
                "gpu_name": gpu_name,
                "gpu_util_pct": gpu_util_pct,
                "gpu_temp_c": gpu_temp_c,
                "gpu_memory_used_mb": gpu_memory_used_mb,
                "gpu_memory_total_mb": gpu_memory_total_mb,
                "cpu_util_pct": cpu_util_pct,
                "ram_used_gb": ram_used_gb,
                "ram_total_gb": ram_total_gb,
                "disk_free_gb": disk_free_gb,
                "is_user_idle": is_user_idle,
            },
        )

    def get_status(self) -> dict | None:
        """Get full pipeline status."""
        return self._get("/api/status")

    def get_game(self, game_id: str) -> dict | None:
        """Get game details including file share paths."""
        return self._get(f"/api/game/{game_id}")

    def is_available(self) -> bool:
        """Check if the API server is reachable."""
        return self._get("/api/status") is not None

    # --- Orchestrator operations ---

    def enqueue(
        self,
        task_type: str,
        *,
        game_id: str | None = None,
        priority: int = 50,
        target_machine: str | None = None,
        payload: dict | None = None,
        max_attempts: int = 3,
    ) -> int | None:
        """Enqueue a work item. Returns item ID."""
        result = self._post(
            "/api/enqueue",
            {
                "task_type": task_type,
                "game_id": game_id,
                "priority": priority,
                "target_machine": target_machine,
                "payload": payload,
                "max_attempts": max_attempts,
            },
        )
        return result.get("id") if result else None

    def has_active_item(self, task_type: str, game_id: str | None = None) -> bool:
        """Check if a queued/claimed/running item exists."""
        params = f"?game_id={game_id}" if game_id else ""
        result = self._get(f"/api/has-active/{task_type}{params}")
        return result.get("active", False) if result else False

    def get_queue_depth(self, task_types: list[str] | None = None) -> int:
        """Count active (queued + claimed + running) items."""
        params = f"?task_types={','.join(task_types)}" if task_types else ""
        result = self._get(f"/api/queue-depth{params}")
        return result.get("depth", 0) if result else 0

    def reclaim_stale(self, timeout: int = 7200) -> list:
        """Reclaim stale work items."""
        result = self._post(f"/api/reclaim-stale?timeout={timeout}")
        return result.get("items", []) if result else []

    def set_game_state(self, game_id: str, state: str, error: str | None = None):
        """Set pipeline state for a game."""
        self._post(f"/api/game/{game_id}/state", {"state": state, "error": error})

    def reset_attempts(self, game_id: str):
        self._post(f"/api/game/{game_id}/reset-attempts")

    def increment_attempts(self, game_id: str):
        self._post(f"/api/game/{game_id}/increment-attempts")

    def update_game_stats(self, game_id: str, **stats):
        self._post(f"/api/game/{game_id}/stats", stats)

    def release_worker_tasks(self, hostname: str) -> int:
        """Release all tasks held by this worker (orphan cleanup on startup)."""
        result = self._post(f"/api/release-worker/{hostname}")
        return result.get("released", 0) if result else 0

    def delete_worker(self, hostname: str):
        """Remove a worker status entry."""
        self._delete(f"/api/workers/{hostname}")

    def set_priority(self, item_id: int, priority: int):
        """Update a queue item's priority."""
        self._patch(f"/api/queue/{item_id}/priority", {"priority": priority})

    def delete_item(self, item_id: int):
        """Delete a queue item."""
        self._delete(f"/api/queue/{item_id}")

    def get_events(self, **params) -> list:
        """Get pipeline events with optional filters."""
        qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        return self._get(f"/api/events?{qs}") or []

    def get_games_needing_work(self) -> list:
        return self._get("/api/games/needing-work") or []

    def get_all_games(self) -> list:
        return self._get("/api/games") or []

    def get_trainable_games(self) -> list:
        return self._get("/api/games/trainable") or []

    def get_state_counts(self) -> dict:
        return self._get("/api/state-counts") or {}

    def get_queue_items(self, status: str | None = None, limit: int = 50) -> list:
        params = f"?limit={limit}"
        if status:
            params += f"&status={status}"
        return self._get(f"/api/queue{params}") or []

    def log_event(self, level: str, message: str, **kwargs):
        self._post("/api/log-event", {"level": level, "message": message, **kwargs})

    def maybe_checkpoint(self):
        """Request a WAL checkpoint if the server supports it."""
        self._post("/api/maintenance/checkpoint", {})
