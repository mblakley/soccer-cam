"""Worker — autonomous pull-based task executor.

Each machine runs one worker process. The worker:
1. Checks if the machine is idle (no games running)
2. Checks resource availability (GPU temp, disk space)
3. Claims the highest-priority work item via HTTP API
4. Pulls data to local SSD, processes, pushes results back
5. Reports status and heartbeat via HTTP API

Usage:
    uv run python -m training.worker run
    uv run python -m training.worker run --once
    uv run python -m training.worker status
"""

import logging
import os
import platform
import signal
import threading
import time
import tomllib
from pathlib import Path

from training.pipeline.client import PipelineClient
from training.worker.resources import ResourceMonitor, ResourceState

logger = logging.getLogger(__name__)

# Graceful shutdown
_shutdown = threading.Event()


def _handle_signal(signum, frame):
    logger.info("Received signal %d, shutting down gracefully...", signum)
    _shutdown.set()


class Worker:
    """Autonomous pull-based task executor."""

    def __init__(
        self,
        hostname: str,
        capabilities: list[str],
        api_url: str,
        local_work_dir: str,
        server_share: str = "",
        local_models_dir: str = "",
        max_gpu_temp: int = 85,
        min_disk_free_gb: int = 20,
        gpu_device: int = 0,
        idle_games: list[str] | None = None,
        heartbeat_interval: int = 30,
    ):
        self.hostname = hostname
        self.capabilities = capabilities
        self.api = PipelineClient(api_url)
        self.server_share = server_share
        self.local_work_dir = Path(local_work_dir)
        self.local_models_dir = Path(local_models_dir) if local_models_dir else None
        self.max_gpu_temp = max_gpu_temp
        self.min_disk_free_gb = min_disk_free_gb
        self.heartbeat_interval = heartbeat_interval

        self.monitor = ResourceMonitor(
            idle_games=idle_games or [],
            work_dir=local_work_dir,
            gpu_device=gpu_device,
        )

        # Ensure work dir exists
        self.local_work_dir.mkdir(parents=True, exist_ok=True)

        self._current_task_id: int | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._heartbeat_stop: threading.Event | None = None

    @classmethod
    def from_config(cls, config_path: Path) -> "Worker":
        """Create a worker from a TOML config file."""
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)

        w = raw.get("worker", {})
        r = raw.get("resources", w.get("resources", {}))

        # Add CUDA DLL path if configured (needed for remote machines
        # where torch is installed in system Python, not the venv)
        cuda_path = r.get("cuda_path", "")
        if cuda_path and cuda_path not in os.environ.get("PATH", ""):
            os.environ["PATH"] = cuda_path + os.pathsep + os.environ.get("PATH", "")
            logger.info("Added CUDA path to PATH: %s", cuda_path)

        return cls(
            hostname=w.get("hostname", platform.node()),
            capabilities=w.get("capabilities", []),
            api_url=w.get("api_url", "http://192.168.86.152:8643"),
            local_work_dir=w.get("local_work_dir", "C:/soccer-cam-label/work"),
            server_share=w.get("server_share", ""),
            local_models_dir=w.get("local_models_dir", ""),
            max_gpu_temp=r.get("max_gpu_temp", 85),
            min_disk_free_gb=r.get("min_disk_free_gb", 20),
            gpu_device=r.get("gpu_device", 0),
            idle_games=r.get("idle_games", w.get("idle_games", [])),
            heartbeat_interval=raw.get("heartbeat", {}).get("interval", 30),
        )

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self, once: bool = False):
        """Main worker loop — pull work, execute, repeat."""
        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

        logger.info(
            "Worker %s starting (capabilities: %s, api: %s)",
            self.hostname,
            ", ".join(self.capabilities),
            self.api.base_url,
        )

        # Wait for API to be available
        while not _shutdown.is_set() and not self.api.is_available():
            logger.info("Waiting for API server...")
            _shutdown.wait(timeout=10)

        # Release any tasks orphaned by a previous instance of this worker.
        # When we restart (kill + start), running tasks stay in "running"
        # status with stale heartbeats. Fail them now so they get re-queued.
        self._release_orphaned_tasks()

        while not _shutdown.is_set():
            try:
                self._tick()
            except Exception:
                logger.exception("Worker tick failed")
                time.sleep(30)

            if once:
                break

            _shutdown.wait(timeout=10)

        logger.info("Worker %s stopped", self.hostname)

    def _tick(self):
        """One iteration of the worker loop."""
        # 1. Check resources
        state = self.monitor.check()
        self._report_status(state)

        # 2. Am I idle?
        if not state.is_user_idle:
            logger.debug("Machine busy (%s), sleeping...", state.running_game)
            self._report_status(state, status="yielded")
            _shutdown.wait(timeout=60)
            return

        # 3. Can I work? (temp, disk)
        if state.gpu_temp_c > self.max_gpu_temp:
            logger.warning(
                "GPU too hot (%.0fC > %dC), cooling down...",
                state.gpu_temp_c,
                self.max_gpu_temp,
            )
            self._report_status(state, status="idle")
            _shutdown.wait(timeout=60)
            return

        if state.disk_free_gb < self.min_disk_free_gb:
            logger.warning(
                "Disk low (%.1fGB < %dGB), skipping work...",
                state.disk_free_gb,
                self.min_disk_free_gb,
            )
            self._report_status(state, status="idle")
            _shutdown.wait(timeout=60)
            return

        # 4. Determine what task types I can handle right now
        available = self._available_capabilities(state)
        if not available:
            logger.debug("No capabilities available right now")
            _shutdown.wait(timeout=30)
            return

        # 5. Claim work via API
        item = self.api.claim(available, self.hostname)
        if item is None:
            logger.debug("No work available, sleeping...")
            self._report_status(state, status="idle")
            _shutdown.wait(timeout=30)
            return

        # 6. Execute
        self._execute(item, state)

    def _available_capabilities(self, state: ResourceState) -> list[str]:
        """Filter capabilities based on current resource state."""
        available = []
        for cap in self.capabilities:
            if cap in ("train", "label") and state.gpu_util_pct > 50:
                continue
            available.append(cap)
        return available

    # ------------------------------------------------------------------
    # Task execution
    # ------------------------------------------------------------------

    def _execute(self, item: dict, state: ResourceState):
        """Execute a work item with heartbeat monitoring."""
        task_type = item["task_type"]
        game_id = item.get("game_id") or "pipeline"
        item_id = item["id"]

        logger.info("Starting %s for %s (id=%d)", task_type, game_id, item_id)

        self._cleanup_stale_work_dirs()
        self.api.start(item_id)
        self._current_task_id = item_id
        self._report_status(state, status="working", task_id=item_id)
        self._start_heartbeat(item_id)

        try:
            result = self._run_task(task_type, item)
            self.api.complete(item_id, result)
            logger.info("Completed %s for %s (id=%d)", task_type, game_id, item_id)
        except Exception as e:
            logger.exception("Failed %s for %s (id=%d)", task_type, game_id, item_id)
            self.api.fail(item_id, str(e))
        finally:
            self._stop_heartbeat()
            self._current_task_id = None
            self._cleanup_work_dir(item)

    def _run_task(self, task_type: str, item: dict) -> dict:
        """Dispatch to the appropriate task handler."""
        from training.tasks import get_task_handler

        handler = get_task_handler(task_type)
        if handler is None:
            raise ValueError(f"Unknown task type: {task_type}")

        return handler(
            item=item,
            local_work_dir=self.local_work_dir,
            server_share=self.server_share,
            local_models_dir=self.local_models_dir,
        )

    def _release_orphaned_tasks(self):
        """Release any tasks this worker held from a previous instance.

        When a worker is killed (taskkill, service restart), its running
        tasks stay in 'running' status with stale heartbeats. On startup,
        we tell the API to re-queue them so they get picked up immediately
        instead of waiting for the 2-hour stale timeout.
        """
        try:
            released = self.api.release_worker_tasks(self.hostname)
            if released:
                logger.info(
                    "Released %d orphaned task(s) from previous instance", released
                )
        except Exception as e:
            logger.warning("Failed to release orphaned tasks: %s", e)

    def _cleanup_work_dir(self, item: dict):
        """Clean up local working files after task completion.

        Force-closes any open SQLite connections first — tasks open
        manifest.db but may not close it on exception, leaving WAL/SHM
        files locked. Since we're in the same process, gc.collect()
        releases the unreferenced connection objects.
        """
        game_id = item.get("game_id")
        if game_id:
            game_work = self.local_work_dir / game_id
            if game_work.exists():
                import gc
                import shutil

                # Force Python to finalize any unreferenced SQLite connections
                # that the failed task left open. This releases file locks on
                # manifest.db-wal and manifest.db-shm.
                gc.collect()

                try:
                    shutil.rmtree(game_work)
                    logger.debug("Cleaned up %s", game_work)
                except Exception as e:
                    logger.warning("Failed to clean up %s: %s", game_work, e)

    def _cleanup_stale_work_dirs(self):
        """Clean up leftover work dirs from previous failed tasks.

        Runs before each new task. Any game dir in the work dir is stale
        — the previous task either failed cleanup or had locked files.
        gc.collect() first to release any lingering SQLite connections.
        """
        import gc
        import shutil

        if not self.local_work_dir.exists():
            return

        gc.collect()

        # "training" subdir is used by the train task for datasets — skip it
        skip = {"training"}
        cleaned = 0
        for entry in self.local_work_dir.iterdir():
            if not entry.is_dir() or entry.name in skip:
                continue
            try:
                shutil.rmtree(entry)
                cleaned += 1
                logger.info("Cleaned stale work dir: %s", entry.name)
            except Exception as e:
                logger.debug("Could not clean %s: %s", entry.name, e)

        if cleaned:
            logger.info("Cleaned %d stale work dir(s)", cleaned)

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def _start_heartbeat(self, item_id: int):
        """Start background heartbeat thread.

        Uses a per-heartbeat stop event (not the global _shutdown) so that
        stopping the heartbeat between tasks doesn't signal the main loop
        to exit.  Also updates worker_status.last_seen on every beat so the
        orchestrator doesn't think the worker is stale during long tasks.
        """
        self._stop_heartbeat()

        stop = threading.Event()
        self._heartbeat_stop = stop

        def _beat():
            while not stop.is_set() and not _shutdown.is_set():
                try:
                    self.api.heartbeat(item_id)
                except Exception as e:
                    logger.warning("Heartbeat failed: %s", e)
                # Also refresh worker_status.last_seen so orchestrator
                # doesn't flag us as stale during long-running tasks.
                try:
                    state = self.monitor.check()
                    self._report_status(state, status="working", task_id=item_id)
                except Exception:
                    pass
                stop.wait(timeout=self.heartbeat_interval)

        self._heartbeat_thread = threading.Thread(target=_beat, daemon=True)
        self._heartbeat_thread.start()

    def _stop_heartbeat(self):
        """Stop background heartbeat thread."""
        if self._heartbeat_stop is not None:
            self._heartbeat_stop.set()
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=5)
        self._heartbeat_thread = None
        self._heartbeat_stop = None

    # ------------------------------------------------------------------
    # Status reporting
    # ------------------------------------------------------------------

    def _report_status(
        self,
        state: ResourceState,
        *,
        status: str = "idle",
        task_id: int | None = None,
    ):
        """Report worker status via API."""
        try:
            self.api.report_status(
                self.hostname,
                status=status,
                current_task_id=task_id or self._current_task_id,
                gpu_name=state.gpu_name,
                gpu_util_pct=state.gpu_util_pct,
                gpu_temp_c=state.gpu_temp_c,
                gpu_memory_used_mb=state.gpu_memory_used_mb,
                gpu_memory_total_mb=state.gpu_memory_total_mb,
                cpu_util_pct=state.cpu_util_pct,
                ram_used_gb=state.ram_used_gb,
                ram_total_gb=state.ram_total_gb,
                disk_free_gb=state.disk_free_gb,
                is_user_idle=state.is_user_idle,
            )
        except Exception as e:
            logger.debug("Failed to report status: %s", e)
