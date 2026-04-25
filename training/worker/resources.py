"""Resource monitoring — GPU, CPU, disk, and idle detection.

Provides a unified interface for checking machine resources. Used by
workers to decide whether to claim work, and to report status to the queue.

Usage:
    from training.worker.resources import ResourceMonitor

    mon = ResourceMonitor(idle_games=["FortniteClient-Win64-Shipping"])
    state = mon.check()
    print(state.gpu_temp_c, state.disk_free_gb, state.is_user_idle)
"""

import logging
import shutil
import subprocess
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ResourceState:
    """Snapshot of machine resources."""

    gpu_name: str = "unknown"
    gpu_util_pct: float = 0.0
    gpu_temp_c: float = 0.0
    gpu_memory_used_mb: float = 0.0
    gpu_memory_total_mb: float = 0.0
    cpu_util_pct: float = 0.0
    ram_used_gb: float = 0.0
    ram_total_gb: float = 0.0
    disk_free_gb: float = 0.0
    is_user_idle: bool = True
    running_game: str | None = None


class ResourceMonitor:
    """Monitor local machine resources."""

    def __init__(
        self,
        idle_games: list[str] | None = None,
        work_dir: str = "C:/",
        gpu_device: int = 0,
    ):
        self.idle_games = idle_games or [
            "FortniteClient-Win64-Shipping",
            "RobloxPlayerBeta",
            "RocketLeague",
        ]
        self.work_dir = work_dir
        self.gpu_device = gpu_device

    def check(self) -> ResourceState:
        """Check all resources and return current state."""
        state = ResourceState()

        # GPU
        try:
            gpu = self._check_gpu()
            state.gpu_name = gpu.get("name", "unknown")
            state.gpu_util_pct = gpu.get("util", 0.0)
            state.gpu_temp_c = gpu.get("temp", 0.0)
            state.gpu_memory_used_mb = gpu.get("mem_used", 0.0)
            state.gpu_memory_total_mb = gpu.get("mem_total", 0.0)
        except Exception as e:
            logger.debug("GPU check failed: %s", e)

        # CPU / RAM
        try:
            cpu_ram = self._check_cpu_ram()
            state.cpu_util_pct = cpu_ram.get("cpu", 0.0)
            state.ram_used_gb = cpu_ram.get("ram_used", 0.0)
            state.ram_total_gb = cpu_ram.get("ram_total", 0.0)
        except Exception as e:
            logger.debug("CPU/RAM check failed: %s", e)

        # Disk
        try:
            total, used, free = shutil.disk_usage(self.work_dir)
            state.disk_free_gb = free / (1024**3)
        except Exception as e:
            logger.debug("Disk check failed: %s", e)

        # Idle (game detection)
        running_game = self._check_idle()
        state.is_user_idle = running_game is None
        state.running_game = running_game

        return state

    def _check_gpu(self) -> dict:
        """Query nvidia-smi for GPU stats."""
        result = subprocess.run(
            [
                "nvidia-smi",
                f"--id={self.gpu_device}",
                "--query-gpu=name,utilization.gpu,temperature.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return {}

        parts = [p.strip() for p in result.stdout.strip().split(",")]
        if len(parts) < 5:
            return {}

        return {
            "name": parts[0],
            "util": float(parts[1]),
            "temp": float(parts[2]),
            "mem_used": float(parts[3]),
            "mem_total": float(parts[4]),
        }

    def _check_cpu_ram(self) -> dict:
        """Get CPU and RAM usage."""
        try:
            import psutil

            return {
                "cpu": psutil.cpu_percent(interval=0.1),
                "ram_used": psutil.virtual_memory().used / (1024**3),
                "ram_total": psutil.virtual_memory().total / (1024**3),
            }
        except ImportError:
            # psutil not available — use wmic on Windows
            try:
                result = subprocess.run(
                    ["wmic", "OS", "get", "FreePhysicalMemory,TotalVisibleMemorySize", "/value"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                values = {}
                for line in result.stdout.strip().splitlines():
                    if "=" in line:
                        k, v = line.split("=", 1)
                        values[k.strip()] = int(v.strip())
                total_kb = values.get("TotalVisibleMemorySize", 0)
                free_kb = values.get("FreePhysicalMemory", 0)
                return {
                    "cpu": 0.0,  # can't easily get without psutil
                    "ram_total": total_kb / (1024**2),
                    "ram_used": (total_kb - free_kb) / (1024**2),
                }
            except Exception:
                return {}

    def _check_idle(self) -> str | None:
        """Check if any game processes are running. Returns game name or None."""
        try:
            import psutil

            for proc in psutil.process_iter(["name"]):
                try:
                    name = proc.info["name"].lower()
                    for game in self.idle_games:
                        if game.lower() in name:
                            return proc.info["name"]
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            return None
        except ImportError:
            # Fallback: tasklist
            try:
                result = subprocess.run(
                    ["tasklist", "/FO", "CSV", "/NH"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                for line in result.stdout.splitlines():
                    name = line.split(",")[0].strip('"').lower()
                    for game in self.idle_games:
                        if game.lower() in name:
                            return line.split(",")[0].strip('"')
                return None
            except Exception:
                return None
