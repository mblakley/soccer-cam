"""Reprocess request processor — polls TTT for pending reprocess requests
and translates each into the local pipeline-runner re-entry mechanism.

Flow per request:
  1. Poll ``GET /api/reprocess-requests/queue`` for work scoped to teams
     this user is a camera-manager for.
  2. For a pending request: claim it (atomic — only one camera-manager
     wins), look up the recording's ``file_group`` to find the local
     ``recording_group_dir``, write
     ``reprocess_request.json`` + nudge ``state.json``. The local
     pipeline_processor's discovery loop picks it up.
  3. For a claimed/running request I own: check ``cancel_requested``
     and translate it into a local ``cancel_request.json`` marker.
  4. Watch local ``state.json`` for transitions and report
     status back to TTT (``running`` when pipeline starts,
     ``completed`` / ``cancelled`` / ``failed`` when it finishes).

This is the cross-network bridge: TTT owns the user-facing surface;
the local runner owns the actual compute. The processor never runs
inference itself — it just translates rows in the TTT table into
files on the local filesystem, and translates state transitions in
the other direction.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from .base_polling_processor import PollingProcessor
from ..utils.config import Config

logger = logging.getLogger(__name__)


_LOCAL_TERMINAL_STATUSES = {
    "pipeline_complete",
    "pipeline_failed",
    "pipeline_cancelled",
    "complete",
    "ball_tracking_complete",
}


class ReprocessRequestProcessor(PollingProcessor):
    """Bridges TTT ``reprocess_requests`` rows to local pipeline runs."""

    def __init__(
        self,
        storage_path: str,
        config: Config,
        ttt_client,
        poll_interval: int = 60,
    ):
        super().__init__(storage_path, config, poll_interval)
        self.ttt_client = ttt_client
        # request_id -> {"group_dir": Path, "status": str}; tracks the
        # in-flight set so we can report transitions back to TTT.
        self._tracked: dict[str, dict[str, Any]] = {}

    async def discover_work(self) -> None:
        """Poll TTT for pending + this-camera-manager's claimed requests."""
        if not self.ttt_client.is_authenticated():
            logger.debug("TTT client not authenticated, skipping reprocess poll")
            return

        try:
            queue = await asyncio.to_thread(self.ttt_client.get_reprocess_queue)
        except Exception as exc:
            # 403 = "not a camera-manager for any team" — that's normal for
            # a freshly-set-up install; quiet log.
            logger.debug("reprocess: queue poll failed (%s)", exc)
            return

        for req in queue:
            status = req.get("status")
            if status == "pending":
                await self._claim_and_start(req)
            elif status in ("claimed", "running"):
                await self._maybe_propagate_cancel(req)
                await self._maybe_report_progress(req)

    # ------------------------------------------------------------------
    # Pending → claim → write local request files
    # ------------------------------------------------------------------

    async def _claim_and_start(self, req: dict[str, Any]) -> None:
        """Atomic claim, then resolve local group_dir + write the local
        ``reprocess_request.json`` for the pipeline_processor to pick up."""
        req_id = req["id"]
        try:
            claimed = await asyncio.to_thread(
                self.ttt_client.claim_reprocess_request, req_id
            )
        except Exception as exc:
            # Either someone else won (409) or the request is gone — quiet.
            logger.debug("reprocess: claim %s failed (%s)", req_id, exc)
            return

        group_dir = await self._resolve_group_dir(claimed)
        if group_dir is None:
            await self._report_failure(
                req_id,
                "Could not resolve local recording_group_dir for this recording. "
                "Make sure the camera-manager box has the source video.",
            )
            return

        # Translate the TTT row into the local override file the runner
        # already knows how to honor.
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
        self._tracked[req_id] = {"group_dir": group_dir, "status": "claimed"}
        logger.info(
            "reprocess: claimed %s -> %s (strength=%s, skip_detect=%s)",
            req_id,
            group_dir,
            claimed["stabilization_strength"],
            claimed["skip_detect"],
        )

    async def _resolve_group_dir(self, claimed: dict[str, Any]) -> Path | None:
        """Look up the recording's ``file_group`` (e.g.
        ``2026.06.06-15.01.33``) and translate it to a local path under
        ``storage_path``.

        Currently does a follow-up GET — could be folded into the
        ``/queue`` response in a future TTT iteration, but this keeps
        the API change small and the call rate is negligible (one per
        new claim, not one per poll)."""
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
            logger.warning(
                "reprocess: recording %s has no file_group; can't locate locally",
                claimed["recording_id"],
            )
            return None
        candidate = Path(self.storage_path) / file_group
        if not candidate.is_dir():
            logger.warning(
                "reprocess: file_group %r resolves to %s which does not exist locally",
                file_group,
                candidate,
            )
            return None
        return candidate

    def _nudge_state(self, group_dir: Path) -> None:
        """Roll a terminal state.json back to ``pipeline_queued_reprocess``
        so the pipeline_discovery loop re-queues it. Mirrors the local
        web reprocess endpoint."""
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

    # ------------------------------------------------------------------
    # User-requested cancel → local marker
    # ------------------------------------------------------------------

    async def _maybe_propagate_cancel(self, req: dict[str, Any]) -> None:
        """If the user flipped ``cancel_requested`` on TTT, write the
        local cancel marker so the runner stops at its next step
        boundary."""
        if not req.get("cancel_requested"):
            return
        req_id = req["id"]
        tracked = self._tracked.get(req_id)
        if tracked is None:
            # Either we don't own this request anymore (claimed_by was
            # cleared) or we restarted and lost track — re-resolve.
            group_dir = await self._resolve_group_dir(req)
            if group_dir is None:
                return
            tracked = {"group_dir": group_dir, "status": req["status"]}
            self._tracked[req_id] = tracked

        from video_grouper.pipeline.reprocess import write_cancel_request

        write_cancel_request(tracked["group_dir"])
        logger.info(
            "reprocess: propagated cancel for %s to %s",
            req_id,
            tracked["group_dir"],
        )

    # ------------------------------------------------------------------
    # Local state.json → TTT status
    # ------------------------------------------------------------------

    async def _maybe_report_progress(self, req: dict[str, Any]) -> None:
        """Read the local state.json + pipeline_state.json and translate
        any transition into a TTT status update."""
        req_id = req["id"]
        tracked = self._tracked.get(req_id)
        if tracked is None:
            return
        group_dir: Path = tracked["group_dir"]
        last_status: str = tracked["status"]

        local = self._read_local_status(group_dir)
        new_status, current_step, error = self._map_local_to_ttt(local)
        if new_status is None or new_status == last_status:
            return

        try:
            await asyncio.to_thread(
                self.ttt_client.update_reprocess_status,
                req_id,
                new_status,
                current_step,
                error,
            )
        except Exception as exc:
            logger.warning(
                "reprocess: failed to report %s -> %s (%s)", req_id, new_status, exc
            )
            return

        tracked["status"] = new_status
        if new_status in ("completed", "cancelled", "failed"):
            self._tracked.pop(req_id, None)

    def _read_local_status(self, group_dir: Path) -> dict[str, Any]:
        out: dict[str, Any] = {}
        state_path = group_dir / "state.json"
        if state_path.exists():
            try:
                out["state_status"] = json.loads(
                    state_path.read_text(encoding="utf-8")
                ).get("status")
            except (OSError, json.JSONDecodeError):
                pass
        pipe_path = group_dir / "pipeline_state.json"
        if pipe_path.exists():
            try:
                pipe = json.loads(pipe_path.read_text(encoding="utf-8"))
                # Current step = the running one, falling back to the
                # most-recently-completed step.
                running = next(
                    (s for s in pipe.get("steps", []) if s.get("status") == "running"),
                    None,
                )
                if running:
                    out["current_step"] = running.get("step_id")
                failed = next(
                    (s for s in pipe.get("steps", []) if s.get("status") == "failed"),
                    None,
                )
                if failed:
                    out["error"] = failed.get("error")
            except (OSError, json.JSONDecodeError):
                pass
        return out

    @staticmethod
    def _map_local_to_ttt(
        local: dict[str, Any],
    ) -> tuple[str | None, str | None, str | None]:
        """Translate the local ``state.status`` enum into a TTT lifecycle
        status. Returns ``(status, current_step, error_message)``;
        ``status=None`` means "no change worth reporting"."""
        ss = local.get("state_status")
        if ss == "pipeline_complete":
            return "completed", None, None
        if ss == "pipeline_cancelled":
            return "cancelled", None, None
        if ss == "pipeline_failed":
            return "failed", None, local.get("error")
        if local.get("current_step"):
            return "running", local["current_step"], None
        return None, None, None

    async def _report_failure(self, req_id: str, message: str) -> None:
        try:
            await asyncio.to_thread(
                self.ttt_client.update_reprocess_status,
                req_id,
                "failed",
                None,
                message,
            )
        except Exception as exc:
            logger.warning(
                "reprocess: could not report failure for %s (%s)", req_id, exc
            )
