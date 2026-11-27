"""TTT game-session lookup for the parent event-tap anchors.

The phase *push* (offsets and per-phase verification) moved to the recordings
port (``video_grouper.integrations.recordings``; TTT columns live in its TTT
implementation). What remains is the session lookup the game-start anchor fetch
still uses; it moves to the phase-hint port next (plan phase 4).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _authed_client_and_session(
    ttt_config: dict | None, recording_group_dir: str, storage_path: str
):
    """Build an authenticated TTT client and resolve the game session for a recording.

    Returns ``(client, session)`` or ``(None, None)`` when TTT is disabled, the
    client can't authenticate, or no session exists for the dir. Never raises.
    """
    if not ttt_config or not ttt_config.get("enabled", True):
        return None, None
    try:
        from video_grouper.api_integrations.ttt_api import TTTApiClient

        client = TTTApiClient(
            supabase_url=ttt_config.get("supabase_url", ""),
            anon_key=ttt_config.get("anon_key", ""),
            api_base_url=ttt_config.get("api_base_url", ""),
            storage_path=str(storage_path),
        )
        if not client.is_authenticated():
            email, password = ttt_config.get("email"), ttt_config.get("password")
            if email and password:
                client.login(email, password)
        if not client.is_authenticated():
            logger.info("phase TTT: not authenticated; skipping (phases stay local)")
            return None, None
        session = client.get_game_session_by_dir(recording_group_dir)
        if not session or not session.get("id"):
            logger.info(
                "phase TTT: no game session for %s; skipping", recording_group_dir
            )
            return None, None
        return client, session
    except Exception as e:  # noqa: BLE001
        logger.warning("phase TTT: client/session resolution failed: %s", e)
        return None, None
