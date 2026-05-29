"""Read NSIS install-phase markers left behind by ``installer.nsi``.

NSIS doesn't natively log to a file. Instead it writes the current
section's name to ``%ProgramData%\\VideoGrouper\\update\\nsis-phase.txt``
at each boundary, overwriting the previous value. If NSIS crashes
or aborts mid-install, the file is frozen at the last phase it
reached -- the post-upgrade service can read it and tell us how
far the upgrade got.

Phases (defined in ``installer.nsi``):

  started -> files-copied -> service-installed ->
  scheduled-task-registered -> service-started ->
  tray-launched -> complete

If the file reads ``complete``, the install ran to the end. Anything
else is the failure-furthest point reached. Missing file means no
install has run since the last journal sync (we delete the file
after journaling).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def nsis_marker_path() -> Path:
    """Where NSIS leaves the breadcrumb.

    Uses ``%ProgramData%`` so the path is visible to both the
    installer (running as the elevated user) and the service
    (running as LocalSystem). Honours an env-var override for
    testability.
    """
    override = os.environ.get("SOCCER_CAM_NSIS_MARKER_PATH")
    if override:
        return Path(override)
    base = os.environ.get("ProgramData", r"C:\ProgramData")
    return Path(base) / "VideoGrouper" / "update" / "nsis-phase.txt"


def read_and_clear_marker() -> str | None:
    """Return the last phase NSIS reached and delete the file.

    Returns None when no marker exists (no recent install) or when
    reading fails -- the function never raises, so callers can wire
    it into startup without try/except.
    """
    path = nsis_marker_path()
    if not path.exists():
        return None
    try:
        phase = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning("Could not read NSIS phase marker at %s: %s", path, exc)
        return None
    try:
        path.unlink()
    except OSError as exc:
        # Non-fatal: next read will see the same value but the
        # journal entry's a one-shot so we won't double-log.
        logger.warning("Could not clear NSIS phase marker at %s: %s", path, exc)
    return phase or None
