"""Team Tech Tools recording sink.

- ``recording_found`` registers the camera files and keeps TTT's recording id in
  the group's state.json (``ttt_recording_id``) -- TTT's id, TTT's field.
- ``progress`` upserts the step on that recording.
- ``published`` auto-attaches the video to the TTT game (the matched game's id
  from match_info.ini when the schedule match came from TTT, else TTT matches
  on recording time).
- ``phases`` / ``phase_verified`` write the game session found by the group
  directory name.

Every call is best-effort: no session, no recording id or any error logs and
returns, never raises.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any

from video_grouper.integrations.registry import provides

from ..base import IRecordingSink, StepReport

logger = logging.getLogger(__name__)

# phase boundary -> TTT game_sessions offset column (seconds into the trimmed video)
_OFFSET_COLUMNS = {
    "kickoff": "phase_kickoff_offset",
    "halftime": "phase_halftime_offset",
    "second_half": "phase_second_half_offset",
    "end": "phase_end_offset",
}
# phase boundary -> TTT per-phase human verification column
_VERIFIED_COLUMNS = {
    "kickoff": "phase_kickoff_verified",
    "halftime": "phase_halftime_verified",
    "second_half": "phase_second_half_verified",
    "end": "phase_end_verified",
}


def phases_to_session_fields(payload: dict[str, Any] | None) -> dict[str, Any]:
    """A phase payload (``{times:{kickoff,...}, ok, source, truncated_*}``) as
    TTT game-session columns; ``{}`` when there are no boundary times."""
    times = (payload or {}).get("times") or {}
    fields: dict[str, Any] = {
        col: float(times[key])
        for key, col in _OFFSET_COLUMNS.items()
        if times.get(key) is not None
    }
    if not fields:
        return {}
    assert payload is not None
    fields["phase_source"] = payload.get("source") or "phase_fused"
    if payload.get("ok") is not None:
        fields["phase_ok"] = bool(payload["ok"])
    for flag in ("truncated_start", "truncated_end"):
        if payload.get(flag) is not None:
            fields[f"phase_{flag}"] = bool(payload[flag])
    return fields


def _dir_name(group_dir: str) -> str:
    return os.path.basename(str(group_dir).rstrip("/\\"))


def _matched_ttt_game_id(group_dir: str) -> str | None:
    """The TTT game the schedule match chose (match_info.ini ``ttt_game_id``)."""
    try:
        from video_grouper.models.match_info import MatchInfo

        path = os.path.join(str(group_dir), "match_info.ini")
        info = MatchInfo.from_file(path) if os.path.exists(path) else None
        return (info.ttt_game_id or None) if info else None
    except Exception:
        return None


@provides(IRecordingSink)
class TTTRecordingSink(IRecordingSink):
    name = "ttt"
    priority = 10

    def __init__(self, client: Any, camera_id: str | None = None):
        self._client = client
        self._camera_id = camera_id or None
        self._team_id: str | None = None

    @classmethod
    def from_context(cls, context) -> TTTRecordingSink | None:
        if not context.ttt_enabled():
            return None
        camera_id = getattr(context.config.ttt, "camera_id", "") or None
        return cls(context.ttt_client(), camera_id)

    # -- helpers -------------------------------------------------------------

    def _signed_in(self) -> bool:
        try:
            return bool(self._client.is_authenticated())
        except Exception:
            return False

    def _team_id_sync(self) -> str | None:
        if self._team_id:
            return self._team_id
        try:
            for assignment in self._client.get_team_assignments() or []:
                if assignment.get("team_id"):
                    self._team_id = assignment["team_id"]
                    return self._team_id
        except Exception as e:
            logger.warning("TTT: could not read team assignments: %s", e)
        return None

    def _session_id_sync(self, group_dir: str) -> str | None:
        session = self._client.get_game_session_by_dir(_dir_name(group_dir))
        return session.get("id") if session else None

    # -- port ----------------------------------------------------------------

    async def recording_found(self, group_dir: str, files: list[Any]) -> None:
        if not self._camera_id or not files or not self._signed_in():
            return
        try:
            file_group = _dir_name(group_dir)
            rows = [
                {
                    "file_name": os.path.basename(str(f.file_path)),
                    "file_group": file_group,
                    "file_size_bytes": (f.metadata or {}).get("file_size"),
                    "duration_seconds": None,
                    "recording_start": f.start_time.isoformat()
                    if f.start_time
                    else None,
                    "recording_end": f.end_time.isoformat() if f.end_time else None,
                }
                for f in files
            ]
            team_id = await asyncio.to_thread(self._team_id_sync)
            if not team_id:
                return
            registered = await asyncio.to_thread(
                self._client.register_recordings, self._camera_id, team_id, rows
            )
            ttt_id = registered[0].get("id") if registered else None
            if ttt_id:
                from video_grouper.models import DirectoryState

                await DirectoryState(group_dir).set_ttt_recording_id(ttt_id)
                logger.info("TTT: registered %d file(s) for %s", len(rows), file_group)
        except Exception as e:
            logger.warning("TTT: registering %s failed: %s", group_dir, e)

    async def progress(self, group_dir: str, report: StepReport) -> None:
        try:
            from video_grouper.models import DirectoryState

            recording_id = DirectoryState(group_dir).ttt_recording_id
            if not recording_id or not self._signed_in():
                return
            await asyncio.to_thread(
                lambda: self._client.update_recording_step(
                    recording_id,
                    step_id=report.step_id,
                    step_type=report.step_type,
                    label=report.label,
                    status=report.status,
                    started_at=report.started_at,
                    completed_at=report.completed_at,
                    error=report.error,
                    config=report.config,
                    artifacts=report.artifacts,
                    pipeline_preset=report.pipeline_preset,
                )
            )
        except Exception as e:
            logger.warning(
                "TTT: step %s for %s failed: %s", report.step_id, group_dir, e
            )

    async def published(
        self, group_dir: str, video_url: str, recorded_at: datetime
    ) -> None:
        if not self._signed_in():
            return
        try:
            team_id = await asyncio.to_thread(self._team_id_sync)
            if not team_id:
                return
            game_id = await asyncio.to_thread(_matched_ttt_game_id, group_dir)
            result = await asyncio.to_thread(
                lambda: self._client.auto_match_video(
                    team_id, video_url, recorded_at.isoformat(), game_id=game_id
                )
            )
            if result:
                logger.info(
                    "TTT: attached %s: matched=%s %s",
                    _dir_name(group_dir),
                    result.get("matched", False),
                    result.get("message", ""),
                )
        except Exception as e:
            logger.warning("TTT: attaching %s failed: %s", group_dir, e)

    async def phases(self, group_dir: str, payload: dict[str, Any]) -> bool:
        fields = phases_to_session_fields(payload)
        return bool(fields) and await self._write_session(group_dir, fields)

    async def phase_verified(self, group_dir: str, phase: str, verdict: str) -> bool:
        column = _VERIFIED_COLUMNS.get(phase)
        if column is None or verdict not in ("correct", "not_correct"):
            return False
        return await self._write_session(group_dir, {column: verdict})

    async def _write_session(self, group_dir: str, fields: dict[str, Any]) -> bool:
        if not self._signed_in():
            return False
        try:
            session_id = await asyncio.to_thread(self._session_id_sync, group_dir)
            if not session_id:
                logger.info("TTT: no game session for %s", _dir_name(group_dir))
                return False
            await asyncio.to_thread(
                lambda: self._client.update_game_session_phases(session_id, **fields)
            )
            return True
        except Exception as e:
            logger.warning("TTT: game-session update for %s failed: %s", group_dir, e)
            return False
