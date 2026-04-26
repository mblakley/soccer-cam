"""Cross-process flags for "service needs interactive user action."

Long-lived OAuth providers (YouTube, TTT, NTFY) hold refresh tokens that
can lapse: revoked by the user, expired beyond renewal, scope-narrowed
during a security review, etc. The Session-0 service can't drive a
browser to recover; the user has to do it interactively from a session
that has one.

The hand-off pattern: when a processor sees a hard auth failure (vs a
transient network blip), it writes a JSON flag file under
``shared_data/<provider>_auth_needed.json``. The dashboard reads these
flags on each render and shows a banner; the tray (Phase 3) polls them
and shows a Windows toast linking back to the dashboard. When the user
re-auths successfully, the flag is cleared.

The same module backs every provider that can lapse, so adding a new
one is just calling ``write_*`` / ``clear_*`` from its error path.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _flag_path(storage_path: str | Path, provider: str) -> Path:
    return Path(storage_path) / f"{provider}_auth_needed.json"


def write_auth_needed(storage_path: str | Path, provider: str, last_error: str) -> None:
    """Mark that ``provider`` needs interactive re-auth."""
    payload = {
        "provider": provider,
        "since": datetime.now(timezone.utc).isoformat(),
        "last_error": last_error,
    }
    path = _flag_path(storage_path, provider)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        logger.info("AUTH_STATUS: %s flagged for re-auth: %s", provider, last_error)
    except OSError as exc:
        logger.warning("AUTH_STATUS: failed to write %s auth flag: %s", provider, exc)


def read_auth_needed(storage_path: str | Path, provider: str) -> Optional[dict]:
    """Return the flag payload if present, else None."""
    path = _flag_path(storage_path, provider)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("AUTH_STATUS: bad %s flag file: %s", provider, exc)
        return None


def clear_auth_needed(storage_path: str | Path, provider: str) -> None:
    """Remove the flag — call after a successful re-auth."""
    path = _flag_path(storage_path, provider)
    if path.exists():
        try:
            path.unlink()
            logger.info("AUTH_STATUS: %s flag cleared", provider)
        except OSError as exc:
            logger.warning("AUTH_STATUS: failed to clear %s flag: %s", provider, exc)


def list_auth_needed(storage_path: str | Path) -> list[dict]:
    """Return all currently-active flags. Used by the dashboard banner."""
    storage = Path(storage_path)
    if not storage.is_dir():
        return []
    flags: list[dict] = []
    for path in storage.glob("*_auth_needed.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            flags.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    return flags


# Hard-failure classification --------------------------------------------------


def is_hard_youtube_auth_failure(exc: BaseException) -> bool:
    """Return True if ``exc`` indicates the YouTube refresh token is
    permanently broken (vs a transient network / quota issue)."""
    msg = str(exc).lower()
    # google.auth.exceptions.RefreshError carries the literal token-server
    # error code in its message; same for raw RuntimeError wrapping it.
    hard_signals = (
        "invalid_grant",
        "token has been expired or revoked",
        "unauthorized_client",
        "invalid_client",
        "deleted_client",
        "no refresh token",
        "no valid credentials",
    )
    return any(sig in msg for sig in hard_signals)
