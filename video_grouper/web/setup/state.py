"""Cookie-keyed in-memory wizard state.

One concurrent wizard at a time is fine; the state lives only for the
duration of a sign-up. Aborting (closing the tab) discards it. State
is intentionally NOT persisted across service restarts — the wizard
is short-lived.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_STATE_COOKIE = "soccer_cam_wizard"


@dataclass
class WizardState:
    storage_path: str = ""
    camera_type: str = "dahua"  # 'dahua' | 'reolink'
    camera_name: str = "default"
    camera_ip: str = ""
    camera_username: str = "admin"
    camera_password: str = ""

    @property
    def is_complete(self) -> bool:
        return bool(self.storage_path and self.camera_ip)


_states: dict[str, WizardState] = {}


def get_or_create(token: Optional[str]) -> tuple[str, WizardState]:
    """Return ``(token, state)``. Mints a new token when none is supplied."""
    if token and token in _states:
        return token, _states[token]
    new_token = secrets.token_urlsafe(24)
    _states[new_token] = WizardState()
    return new_token, _states[new_token]


def get(token: Optional[str]) -> Optional[WizardState]:
    if not token:
        return None
    return _states.get(token)


def discard(token: Optional[str]) -> None:
    if token and token in _states:
        del _states[token]


def cookie_name() -> str:
    return _STATE_COOKIE
