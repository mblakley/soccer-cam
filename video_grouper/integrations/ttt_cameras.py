"""TTT's ids for this install's cameras, shared by the TTT implementations.

The TTT device link fills it from ``POST /api/internal/device-link/register``
(camera name -> TTT camera id); the TTT recording sink reads it to report a
recording under the camera that recorded it. Persisted to
``<storage>/ttt/cameras.json`` so ids survive a restart before the first
registration of the run. TTT-implementation state: nothing outside the TTT
sources reads it.

Until any camera is registered, the hand-typed ``[TTT] camera_id`` still serves
as the id (installs from before self-registration keep working); once one is,
a named camera TTT didn't register has no id.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


class TTTCameraIds:
    def __init__(
        self, storage_path: str | None, configured_camera_id: str | None = None
    ):
        # None: in memory only (nothing read or written).
        self._path = (
            Path(storage_path) / "ttt" / "cameras.json" if storage_path else None
        )
        self._fallback = configured_camera_id or None
        self._lock = threading.Lock()
        self._ids: dict[str, str] = self._load()

    def _load(self) -> dict[str, str]:
        if self._path is None:
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return {str(k): str(v) for k, v in data.items() if v}
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.warning("TTT: could not read %s: %s", self._path, e)
            return {}

    def update(self, ids: dict[str, str]) -> None:
        """Record TTT's ids for these cameras (by name) and persist."""
        with self._lock:
            changed = {k: v for k, v in ids.items() if v and self._ids.get(k) != v}
            if not changed:
                return
            self._ids.update(changed)
            if self._path is None:
                return
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._path.write_text(json.dumps(self._ids, indent=2), encoding="utf-8")
            except Exception as e:
                logger.warning("TTT: could not save %s: %s", self._path, e)

    def id_for(self, camera_name: str | None) -> str | None:
        """TTT's id for ``camera_name``.

        Before any camera has registered, every camera gets the configured id.
        Once one has, a named camera that isn't registered (TTT refused it, or
        it hasn't been described yet) gets None -- never the configured id,
        which would report camera B's recordings under camera A. With no name:
        the only registered id, else the configured one.
        """
        with self._lock:
            if camera_name:
                if camera_name in self._ids:
                    return self._ids[camera_name]
                if self._ids:
                    return None
            elif len(self._ids) == 1:
                return next(iter(self._ids.values()))
        return self._fallback

    def all_ids(self) -> dict[str, str]:
        """Every known camera id by name (the configured one under ``""``)."""
        with self._lock:
            ids = dict(self._ids)
        if not ids and self._fallback:
            ids[""] = self._fallback
        return ids
