"""Pipeline HTTP client — stdlib-only client for remote workers.

No external dependencies (no requests, no httpx). Uses urllib.request
so it works on machines with only Python + opencv + onnxruntime.

Usage:
    from training.pipeline.client import PipelineClient

    api = PipelineClient("http://192.168.86.152:8643")
    item = api.claim(["label", "tile"], "FORTNITE-OP")
    if item:
        api.start(item["id"])
        api.heartbeat(item["id"])
        api.complete(item["id"], {"labels": 500})
"""

import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)


class PipelineClient:
    """HTTP client for the Pipeline API. Stdlib only."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def _post(self, path: str, data: dict | None = None) -> dict | None:
        """POST JSON to the API. Returns parsed response or None."""
        url = f"{self.base_url}{path}"
        body = json.dumps(data).encode() if data else b"{}"
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status == 204:
                    return None
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 204:
                return None
            logger.warning("API %s returned %d: %s", path, e.code, e.read().decode()[:200])
            return None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            logger.warning("API %s failed: %s", path, e)
            return None

    def _get(self, path: str) -> dict | list | None:
        """GET from the API. Returns parsed response or None."""
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            logger.warning("API GET %s failed: %s", path, e)
            return None

    # --- Queue operations ---

    def claim(self, capabilities: list[str], hostname: str) -> dict | None:
        """Claim the next available work item. Returns item dict or None."""
        return self._post("/api/claim", {
            "capabilities": capabilities,
            "hostname": hostname,
        })

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
        self._post("/api/worker-status", {
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
        })

    def get_status(self) -> dict | None:
        """Get full pipeline status."""
        return self._get("/api/status")

    def get_game(self, game_id: str) -> dict | None:
        """Get game details including file share paths."""
        return self._get(f"/api/game/{game_id}")

    def is_available(self) -> bool:
        """Check if the API server is reachable."""
        try:
            result = self._get("/api/status")
            return result is not None
        except Exception:
            return False

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
        result = self._post("/api/enqueue", {
            "task_type": task_type,
            "game_id": game_id,
            "priority": priority,
            "target_machine": target_machine,
            "payload": payload,
            "max_attempts": max_attempts,
        })
        return result.get("id") if result else None

    def has_active_item(self, task_type: str, game_id: str | None = None) -> bool:
        """Check if a queued/claimed/running item exists."""
        params = f"?game_id={game_id}" if game_id else ""
        result = self._get(f"/api/has-active/{task_type}{params}")
        return result.get("active", False) if result else False

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

    def get_games_needing_work(self) -> list:
        return self._get("/api/games/needing-work") or []

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
