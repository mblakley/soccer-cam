"""System metrics collection using psutil.

Valuable independently of TTT — can be displayed in tray UI
or included in NTFY notifications."""

import logging
import time

logger = logging.getLogger(__name__)

try:
    import psutil

    _PSUTIL_AVAILABLE = True
except ImportError:
    _PSUTIL_AVAILABLE = False
    logger.info("psutil not installed — system metrics unavailable")


def get_system_metrics() -> dict:
    """Collect current system metrics.

    Returns empty dict if psutil is not available.
    """
    if not _PSUTIL_AVAILABLE:
        return {}

    try:
        disk = psutil.disk_usage("/")
        return {
            "cpu_usage_percent": psutil.cpu_percent(interval=0.1),
            "memory_usage_percent": psutil.virtual_memory().percent,
            "disk_free_gb": round(disk.free / (1024**3), 2),
            "disk_total_gb": round(disk.total / (1024**3), 2),
            "uptime_seconds": int(time.time() - psutil.boot_time()),
        }
    except Exception as e:
        logger.warning(f"Failed to collect system metrics: {e}")
        return {}


def get_disk_free_gb(path: str | None = None) -> float | None:
    """Get free disk space in GB for a specific path.

    Useful for checking the recording storage directory specifically.
    """
    if not _PSUTIL_AVAILABLE:
        return None
    try:
        disk = psutil.disk_usage(path or "/")
        return round(disk.free / (1024**3), 2)
    except Exception:
        return None
