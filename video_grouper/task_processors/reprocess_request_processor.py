"""ReprocessRequestProcessor — claims a TTT reprocess request, resolves
the recording's local group dir, writes the marker the local
pipeline_processor honors.

QueueProcessor pattern. The "claim + write marker" work runs as
``process_item`` (light enough that the ``ram_heavy`` acquire is
mostly insurance for uniformity). Cancel propagation + progress
reporting for in-flight requests stay in :class:`TTTPoller` because
they're per-poll lifecycle checks, not discrete jobs.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from ..utils.config import Config
from .base_queue_processor import QueueProcessor
from .queue_type import QueueType
from .tasks.ttt.reprocess_request_task import ReprocessRequestTask

logger = logging.getLogger(__name__)


_LOCAL_TERMINAL_STATUSES = {
    "pipeline_complete",
    "pipeline_failed",
    "pipeline_cancelled",
    "complete",
    "ball_tracking_complete",
}


class ReprocessRequestProcessor(QueueProcessor):
    """Claims a pending reprocess request and writes the local marker
    that the pipeline_processor re-entry mechanism picks up."""

    def __init__(
        self,
        storage_path: str,
        config: Config,
        ttt_client=None,
        resource_manager=None,
        on_verify_phases=None,
    ):
        super().__init__(storage_path, config)
        self.ttt_client = ttt_client
        self.resource_manager = resource_manager
        # S3: async callback (group_dir -> None) that starts the NTFY phase
        # verify-loop. Wired to NtfyProcessor.request_phase_verify by the app;
        # None when NTFY isn't configured (a verify_phases request then fails
        # cleanly rather than silently).
        self.on_verify_phases = on_verify_phases

    @property
    def queue_type(self) -> QueueType:
        return QueueType.TTT_REPROCESS_REQUEST

    def get_item_key(self, item: ReprocessRequestTask) -> str:
        # Dedup on the TTT request id so a re-poll of the same row
        # doesn't double-claim.
        return item.ttt_id

    async def process_item(self, item: ReprocessRequestTask) -> None:
        """Claim the TTT row, find the local group dir, write the
        ``reprocess_request.json`` marker.

        Wrapped in the shared ``ram_heavy`` gate even though the work
        itself is light — the uniformity is the point. The pipeline
        re-run that this triggers later DOES contend on ``ram_heavy``,
        and serialising the claim against the gate avoids the very
        narrow window where two reprocess marker writes for the same
        recording could land back-to-back.
        """
        if self.ttt_client is None:
            raise RuntimeError(
                "ReprocessRequestProcessor needs a ttt_client; check TTT config"
            )

        async def _do_work() -> None:
            ttt_id = item.ttt_id

            try:
                claimed = await asyncio.to_thread(
                    self.ttt_client.claim_reprocess_request, ttt_id
                )
            except Exception as exc:
                # Lost the race or the row is gone — quiet success.
                logger.debug("reprocess: claim %s failed (%s)", ttt_id, exc)
                return

            group_dir = await self._resolve_group_dir(claimed)
            if group_dir is None:
                await self._report_failure(
                    ttt_id,
                    "Could not resolve local recording_group_dir for this "
                    "recording. Make sure the camera-manager box has the "
                    "source video.",
                )
                return

            # Dispatch by request kind. Existing (stabilization) rows carry no
            # "kind" -> default, so their behavior is unchanged. S4 adds
            # "phase_correction" (apply a human phase edit); S3 adds
            # "verify_phases" (kick off the NTFY verify-loop) via a separate
            # producer the app wires in.
            kind = claimed.get("kind") or "stabilization"
            if kind == "phase_correction":
                await self._handle_phase_correction(ttt_id, claimed, group_dir)
                return
            if kind == "verify_phases":
                await self._handle_verify_phases(ttt_id, group_dir)
                return
            if kind in ("truncated_start", "truncated_end"):
                await self._handle_truncated(ttt_id, group_dir, kind)
                return

            local_request = {
                "stabilization_strength": claimed["stabilization_strength"],
                "skip_detect": claimed["skip_detect"],
                "requested_at": claimed.get("created_at"),
                "requested_by": f"ttt:{claimed.get('requested_by', '')}",
            }
            (group_dir / "reprocess_request.json").write_text(
                json.dumps(local_request), encoding="utf-8"
            )
            self._nudge_state(group_dir)
            logger.info(
                "reprocess: claimed %s -> %s (strength=%s, skip_detect=%s)",
                ttt_id,
                group_dir,
                claimed["stabilization_strength"],
                claimed["skip_detect"],
            )

        if self.resource_manager is not None:
            async with self.resource_manager.acquire(("ram_heavy",)):
                await _do_work()
        else:
            await _do_work()

    # ------------------------------------------------------------------
    # S4: phase-correction handler
    # ------------------------------------------------------------------

    _PHASE_KEYS = ("kickoff", "halftime", "second_half", "end")

    async def _handle_phase_correction(
        self, ttt_id: str, claimed: dict, group_dir: Path
    ) -> None:
        """Apply a camera-manager phase edit: overwrite the local phases with the
        corrected (human) values and re-confirm them to TTT.

        The corrected boundaries ride in ``claimed["phases"]`` as
        ``{kickoff/halftime/second_half/end: seconds}`` (trimmed-video time, any
        subset). Phases are display-only (decision 4), so nothing downstream
        consumes them yet — no re-trim/re-render is triggered here; that arrives
        with the first phase consumer.
        """
        raw = claimed.get("phases") or {}
        times = {k: float(raw[k]) for k in self._PHASE_KEYS if raw.get(k) is not None}
        if not times:
            await self._report_failure(
                ttt_id, "phase_correction request carried no phase times"
            )
            return
        payload = {"source": "human", "ok": True, "times": times}

        try:
            from video_grouper.models import DirectoryState

            await asyncio.to_thread(
                DirectoryState(str(group_dir)).set_game_phases, payload
            )
        except Exception as exc:  # noqa: BLE001 — local persistence is best-effort
            logger.warning(
                "phase_correction: could not persist phases to state.json (%s)", exc
            )

        await self._repush_phases(group_dir, payload)

        try:
            await asyncio.to_thread(
                self.ttt_client.update_reprocess_status, ttt_id, "completed", None, None
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("phase_correction: could not report completion (%s)", exc)
        logger.info(
            "phase_correction: applied human phases %s to %s", times, group_dir.name
        )

    async def _handle_truncated(self, ttt_id: str, group_dir: Path, kind: str) -> None:
        """Re-run the detector with the human-supplied truncation flag (from the T2
        "already started" / "ended early" control) and re-push corrected phases.

        ``truncated_start`` pins KO=0 and sets the trim to 0 (the game is already in
        progress at the file head); ``truncated_end`` pins END to the file end. HT/2H
        (and the un-truncated boundary) are re-detected. This is the app-side path for
        confident-but-truncated games that never hit the NTFY game-start walk.
        """
        ts = kind == "truncated_start"
        combined = group_dir / "combined.mp4"

        if ts:
            # Trim at 0 -- nothing to cut off the front of a game already underway.
            try:
                from video_grouper.models import MatchInfo

                await asyncio.to_thread(
                    MatchInfo.update_game_times,
                    str(group_dir),
                    start_time_offset="00:00",
                    storage_path=self.storage_path,
                )
            except Exception as exc:  # noqa: BLE001 — trim write is best-effort
                logger.warning("truncated_start: could not set start offset (%s)", exc)

        result = None
        if combined.exists():
            try:
                from video_grouper.task_processors.phase_game_start import _run_detector

                result = await _run_detector(
                    str(group_dir),
                    str(combined),
                    truncated_start=ts,
                    truncated_end=not ts,
                )
            except Exception as exc:  # noqa: BLE001 — never break the flow
                logger.warning(
                    "truncated re-run failed for %s (%s)", group_dir.name, exc
                )

        if result:
            payload = {
                "source": "phase_fused",
                "ok": bool(result.get("ok")),
                "times": {
                    k: float(v)
                    for k, v in (result.get("times") or {}).items()
                    if v is not None
                },
                "truncated_start": bool(result.get("truncated_start")),
                "truncated_end": bool(result.get("truncated_end")),
            }
            try:
                from video_grouper.models import DirectoryState

                await asyncio.to_thread(
                    DirectoryState(str(group_dir)).set_game_phases, payload
                )
            except Exception as exc:  # noqa: BLE001 — local persistence is best-effort
                logger.warning("truncated: could not persist phases (%s)", exc)
            await self._repush_phases(group_dir, payload)

        try:
            await asyncio.to_thread(
                self.ttt_client.update_reprocess_status, ttt_id, "completed", None, None
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("truncated: could not report completion (%s)", exc)
        logger.info("%s applied to %s", kind, group_dir.name)

    async def _repush_phases(self, group_dir: Path, payload: dict) -> None:
        """Re-confirm the (now source=human) phases to the TTT game session."""
        from video_grouper.task_processors.phase_ttt_push import (
            phases_to_session_fields,
        )

        fields = phases_to_session_fields(payload)
        if not fields:
            return
        try:
            session = await asyncio.to_thread(
                self.ttt_client.get_game_session_by_dir, group_dir.name
            )
            if session and session.get("id"):
                await asyncio.to_thread(
                    self.ttt_client.update_game_session, session["id"], **fields
                )
        except Exception as exc:  # noqa: BLE001 — re-push is best-effort
            logger.warning("phase_correction: TTT re-push failed (%s)", exc)

    # ------------------------------------------------------------------
    # S3: verify-phases trigger
    # ------------------------------------------------------------------

    async def _handle_verify_phases(self, ttt_id: str, group_dir: Path) -> None:
        """Start the NTFY verify-loop for this recording (S3).

        The loop itself runs asynchronously in NtfyProcessor (one Correct/
        Not-Correct question per boundary); the reprocess request just means
        "begin verifying", so we mark it completed once the loop is queued.
        """
        if self.on_verify_phases is None:
            await self._report_failure(
                ttt_id,
                "Phase verification is unavailable on this install (NTFY not configured).",
            )
            return
        try:
            await self.on_verify_phases(group_dir)
        except Exception as exc:  # noqa: BLE001
            await self._report_failure(
                ttt_id, f"Could not start phase verification: {exc}"
            )
            return
        try:
            await asyncio.to_thread(
                self.ttt_client.update_reprocess_status, ttt_id, "completed", None, None
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("verify_phases: could not report completion (%s)", exc)
        logger.info("verify_phases: started verify-loop for %s", group_dir.name)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _resolve_group_dir(self, claimed: dict) -> Path | None:
        try:
            recording = await asyncio.to_thread(
                self.ttt_client.get_camera_recording, claimed["recording_id"]
            )
        except Exception as exc:
            logger.warning(
                "reprocess: could not fetch recording %s (%s)",
                claimed["recording_id"],
                exc,
            )
            return None
        file_group = recording.get("file_group")
        if not file_group:
            return None
        candidate = Path(self.storage_path) / file_group
        return candidate if candidate.is_dir() else None

    def _nudge_state(self, group_dir: Path) -> None:
        state_path = group_dir / "state.json"
        if not state_path.exists():
            return
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if state.get("status") in _LOCAL_TERMINAL_STATUSES:
                state["status"] = "pipeline_queued_reprocess"
                state.pop("error_message", None)
                state_path.write_text(json.dumps(state), encoding="utf-8")
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "reprocess: could not nudge state.json at %s (%s)", state_path, exc
            )

    async def _report_failure(self, ttt_id: str, message: str) -> None:
        try:
            await asyncio.to_thread(
                self.ttt_client.update_reprocess_status,
                ttt_id,
                "failed",
                None,
                message,
            )
        except Exception as exc:
            logger.warning("reprocess: could not report failure (%s)", exc)
