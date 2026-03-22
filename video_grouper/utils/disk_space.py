"""Disk space checking utility.

Provides a simple check before downloads to prevent filling the disk.
"""

import logging
import shutil

logger = logging.getLogger(__name__)

# Default minimum free space in GB
DEFAULT_MIN_FREE_GB = 2.0


def check_disk_space(
    path: str, min_free_gb: float = DEFAULT_MIN_FREE_GB
) -> tuple[bool, float]:
    """Check if there is enough free disk space at *path*.

    Args:
        path: Directory path to check (must exist).
        min_free_gb: Minimum required free space in GB.

    Returns:
        Tuple of (has_enough_space, free_gb).
    """
    try:
        usage = shutil.disk_usage(path)
        free_gb = usage.free / (1024**3)
        return free_gb >= min_free_gb, round(free_gb, 2)
    except OSError as exc:
        logger.error(f"Could not check disk space for {path}: {exc}")
        # Be conservative: assume space is available so we don't block on
        # transient errors, but log the issue.
        return True, -1.0
