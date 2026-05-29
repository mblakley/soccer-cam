"""Append-only audit log for auto-upgrade attempts.

Every update attempt — successful, deferred, or failed — writes a single
JSON line to ``<storage>/logs/update_history.jsonl`` so we can diagnose
what happened without re-reading scattered log lines. Joined with the
service log by the ``update_id`` field (an 8-hex-char correlation id
produced by ``new_update_id()``).

The status endpoint at ``/api/update/status`` reads the most recent
entry to answer questions like "did the last check happen?", "what
version is pending install?", and "how far did the last NSIS run get?".

Rotation: when the file passes ``MAX_BYTES`` we move it aside to
``update_history.jsonl.1`` (overwriting any previous rotation) and
start fresh. Two-file scheme is enough — older history is interesting
during a single debug session, not weeks later.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from collections.abc import MutableMapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_BYTES = 1_000_000  # rotate at ~1MB


def new_update_id() -> str:
    """Short correlation id, 8 hex chars. Collision risk is negligible at
    one attempt/hour."""
    return secrets.token_hex(4)


@dataclass
class UpdateJournalEntry:
    """One attempt. Field meanings:

    - ``stages_completed``: ordered list of stage names that ran cleanly
      (``check``, ``quiescence``, ``download``, ``verify``, ``spawn``,
      ``installed``). Lets the status endpoint say "got to download but
      not verify" without re-parsing log files.
    - ``outcome``: terminal state of this attempt. One of
      ``installed``, ``spawned`` (installer launched, install state
      unknown from this side), ``pending_user_approval`` (auto_update
      off, waiting for /apply), ``deferred`` (quiescence said no),
      ``skipped`` (no update available), ``failed``.
    - ``deferred_reason``: human-readable reason set when
      ``outcome == "deferred"`` (e.g. "download_queue depth=2").
    - ``nsis_phase`` / ``nsis_tail``: populated by the post-upgrade
      service when it reads the previous attempt's marker file and log
      from ``%LOCALAPPDATA%\\VideoGrouper\\update\\``.
    """

    id: str
    started_at: float
    from_version: str
    source_url: str
    auto_update: bool
    ended_at: float | None = None
    to_version: str | None = None
    stages_completed: list[str] = field(default_factory=list)
    outcome: str = "in_progress"
    duration_ms: int | None = None
    download_bytes: int | None = None
    digest_expected: str | None = None
    digest_actual: str | None = None
    deferred_reason: str | None = None
    error: str | None = None
    user_action: str | None = None
    nsis_phase: str | None = None
    nsis_tail: str | None = None

    def finalize(self, outcome: str, **kwargs: Any) -> None:
        """Stamp the entry as complete. Sets ``ended_at`` and
        ``duration_ms`` so callers don't have to remember."""
        self.outcome = outcome
        self.ended_at = time.time()
        self.duration_ms = int((self.ended_at - self.started_at) * 1000)
        for k, v in kwargs.items():
            setattr(self, k, v)


def journal_path(storage_path: str | os.PathLike[str]) -> Path:
    return Path(storage_path) / "logs" / "update_history.jsonl"


def append_entry(
    storage_path: str | os.PathLike[str], entry: UpdateJournalEntry
) -> None:
    """Append one JSON line. Best-effort: a journal write failure must
    not break the upgrade flow itself, so we log and move on."""
    path = journal_path(storage_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(path)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(entry), default=str) + "\n")
    except OSError as exc:
        logger.warning("update journal write failed at %s: %s", path, exc)


def read_latest_entries(
    storage_path: str | os.PathLike[str], limit: int = 1
) -> list[dict]:
    """Tail the journal. Used by ``/api/update/status``. Returns the
    last ``limit`` entries (most recent last), or empty list if the
    file doesn't exist yet."""
    path = journal_path(storage_path)
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as exc:
        logger.warning("update journal read failed at %s: %s", path, exc)
        return []

    entries: list[dict] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def _rotate_if_needed(path: Path) -> None:
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size < MAX_BYTES:
        return
    rotated = path.with_suffix(path.suffix + ".1")
    try:
        if rotated.exists():
            rotated.unlink()
        path.rename(rotated)
    except OSError as exc:
        logger.warning("update journal rotation failed at %s: %s", path, exc)


class UpdateLoggerAdapter(logging.LoggerAdapter):
    """Tag every log line in an update attempt with ``[update:<id>]``.

    Use one adapter per attempt — pass it down into ``UpdateManager``
    methods so check/download/verify/spawn all share the same id. The
    journal entry uses the same id, so logs + journal join cleanly.
    """

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> tuple[Any, MutableMapping[str, Any]]:
        extra = self.extra or {}
        return f"[update:{extra.get('update_id', '?')}] {msg}", kwargs
