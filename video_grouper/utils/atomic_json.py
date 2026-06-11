"""Crash-safe JSON read / write / read-modify-write under a ``FileLock``.

Extracted from ``DirectoryState._update_state_field`` so ``state.json`` and the
pipeline manifest (``pipeline_state.json``) share one proven atomic-write
implementation: take the lock, write to a temp file, then ``os.replace`` it
into place. A crash mid-write can never corrupt the target — the previous file
stays intact until the rename succeeds.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from typing import Any

from video_grouper.utils.locking import FileLock

logger = logging.getLogger(__name__)


def _write_atomic(path: str, data: Any) -> None:
    """Write *data* as JSON to *path* via temp file + ``os.replace``.

    Assumes the caller already holds the ``FileLock`` for *path*.
    """
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
    os.replace(temp_path, path)


def read_json(path: str, default: Any = None) -> Any:
    """Read JSON from *path* under a ``FileLock``.

    Returns *default* when the file is missing, unreadable, or corrupt.
    """
    try:
        with FileLock(path):
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError) as e:
        logger.error("Could not parse JSON at %s: %s", path, e)
    except TimeoutError as e:
        logger.error("Timeout reading %s: %s", path, e)
    return default


def write_json(path: str, data: Any) -> None:
    """Atomically write *data* as JSON to *path* under a ``FileLock``."""
    try:
        with FileLock(path):
            _write_atomic(path, data)
    except TimeoutError as e:
        logger.error("Timeout writing %s: %s", path, e)


def update_json(
    path: str,
    mutate: Callable[[dict], None],
    default: dict | None = None,
) -> dict:
    """Read-modify-write a JSON dict at *path* under a single ``FileLock``.

    *mutate* receives the loaded dict (or a fresh copy of *default* when the
    file is missing/corrupt) and edits it in place. The result is written
    atomically and returned. Holding the lock across the whole read-modify-write
    is what makes concurrent updates safe.
    """
    base = dict(default or {})
    try:
        with FileLock(path):
            data: dict = base
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                except (json.JSONDecodeError, FileNotFoundError):
                    data = dict(default or {})
            mutate(data)
            _write_atomic(path, data)
            return data
    except TimeoutError as e:
        logger.error("Timeout updating %s: %s", path, e)
        return base
