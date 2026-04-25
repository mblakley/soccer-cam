"""Persistent record of the most-recent successful ball-detection license.

Hooked from SecureLoader on every successful acquire so the tray UI can show
"Last license: <tier> v<version>, expires <date>" and a soft warning as
expiry approaches. The 30-day TTL on the license is the operational reality
the surface displays — at day 25 the user gets a soft warning; at day 30 a
fresh acquire will fail (and the loader surfaces that as SecureLoaderError).

State lives at `<storage_path>/ttt/license_state.json`. One row only — the
most recent license. Older history isn't needed; the file is rewritten on
each acquire.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

WARNING_DAYS = 25
HALT_DAYS = 30


@dataclass
class LicenseState:
    model_key: str
    version: str
    tier: str
    expires_at: str  # ISO 8601, what the license manifest claimed
    acquired_at: str  # ISO 8601, when we successfully decrypted

    def days_until_expiry(self, now: Optional[datetime] = None) -> float:
        check_time = now or datetime.now(UTC)
        try:
            expires = _parse_iso(self.expires_at)
        except ValueError:
            return -1.0
        return (expires - check_time).total_seconds() / 86400.0

    def status_label(self, now: Optional[datetime] = None) -> str:
        days = self.days_until_expiry(now)
        if days < 0:
            return f"EXPIRED {abs(days):.0f}d ago — re-acquire required"
        if days >= HALT_DAYS - WARNING_DAYS:
            # i.e. days > 5 since acquire; in green territory
            return f"OK — {self.tier} v{self.version} ({days:.0f}d remaining)"
        if days > 0:
            return (
                f"WARNING — {self.tier} v{self.version} "
                f"expires in {days:.0f}d. Stay online to refresh."
            )
        return f"EXPIRED — {self.tier} v{self.version}"


def _parse_iso(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _state_path(storage_path: Path) -> Path:
    return Path(storage_path) / "ttt" / "license_state.json"


def record(
    storage_path: Path | str,
    *,
    model_key: str,
    version: str,
    tier: str,
    expires_at: str,
    now: Optional[datetime] = None,
) -> LicenseState:
    """Persist the most-recent successful license. Overwrites prior state."""
    acquired = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = LicenseState(
        model_key=model_key,
        version=version,
        tier=tier,
        expires_at=expires_at,
        acquired_at=acquired,
    )
    path = _state_path(Path(storage_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")
    logger.info(
        "License state recorded: %s v%s (%s) expires %s",
        model_key,
        version,
        tier,
        expires_at,
    )
    return state


def load(storage_path: Path | str) -> Optional[LicenseState]:
    """Return the persisted state, or None if no license has been acquired."""
    path = _state_path(Path(storage_path))
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return LicenseState(**data)
    except (json.JSONDecodeError, TypeError, KeyError) as exc:
        logger.warning("Could not parse license state at %s: %s", path, exc)
        return None
