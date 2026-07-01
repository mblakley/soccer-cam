"""S2 — push detected game-phase boundaries to the TTT game session.

After the ``phase_detect`` pipeline step fuses the four boundaries on the trimmed
game video, this pushes them to the matching TTT game session (looked up by the
recording group dir) via ``PATCH /api/game-sessions/{id}``. The offsets are
seconds into the *trimmed* game video — exactly what the T1 schema
(``phase_*_offset``) stores and the TTT UI displays.

Everything here is BEST-EFFORT and non-fatal: the canonical phase artifact is the
local ``phases.json`` + ``state.json`` the step already wrote. A community install
(no TTT) or a recording with no TTT session simply skips the push. Import stays
light (no ML deps) so the pipeline step can call it directly.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# detector boundary key -> TTT game_sessions column (T1 schema). Offsets are
# seconds into the trimmed game video.
_OFFSET_COLUMNS = {
    "kickoff": "phase_kickoff_offset",
    "halftime": "phase_halftime_offset",
    "second_half": "phase_second_half_offset",
    "end": "phase_end_offset",
}


def phases_to_session_fields(payload: dict) -> dict:
    """Map a phase payload (``{times:{kickoff,...}, ok, source}``) to TTT columns.

    Returns ``{}`` when there are no boundary times to push (e.g. a no-play
    result), so callers can cheaply skip.
    """
    times = (payload or {}).get("times") or {}
    fields: dict = {
        col: float(times[key])
        for key, col in _OFFSET_COLUMNS.items()
        if times.get(key) is not None
    }
    if not fields:
        return {}
    fields["phase_source"] = payload.get("source") or "phase_fused"
    if payload.get("ok") is not None:
        fields["phase_ok"] = bool(payload["ok"])
    return fields


def push_phases_to_ttt(
    ttt_config: dict | None,
    recording_group_dir: str,
    payload: dict,
    storage_path: str,
) -> bool:
    """Push the fused phases to this recording's TTT game session. Best-effort.

    Returns True iff the phases were pushed. Returns False (and never raises) when
    TTT is disabled, no session exists for ``recording_group_dir``, there are no
    phases to push, or the client can't authenticate — the local artifacts still
    stand and the pipeline is never failed by a push problem.
    """
    if not ttt_config or not ttt_config.get("enabled", True):
        return False
    fields = phases_to_session_fields(payload)
    if not fields:
        return False
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
            logger.info(
                "phase push: TTT not authenticated; skipping (phases stay local)"
            )
            return False
        session = client.get_game_session_by_dir(recording_group_dir)
        if not session or not session.get("id"):
            logger.info(
                "phase push: no TTT game session for %s; skipping", recording_group_dir
            )
            return False
        client.update_game_session(session["id"], **fields)
        logger.info(
            "phase push: updated TTT session %s with %s",
            session["id"],
            {k: fields[k] for k in fields if k.endswith("_offset")},
        )
        return True
    except Exception as e:  # noqa: BLE001 — push is best-effort, never fatal
        logger.warning("phase push to TTT failed (non-fatal): %s", e)
        return False
