"""Single remote-work polling entrypoint.

Per the project's "one PollingProcessor for TTT" invariant: every kind of
remote work (clip requests, highlight reels, processing jobs, reprocess
requests) is queued onto a dedicated :class:`QueueProcessor`, and this class
is the ONE thing that asks the work source (:class:`IWorkSource`) for new
work and enqueues it. One ``pending()`` call per tick, for just the kinds a
processor is wired for; each item goes to its kind's processor. It also owns:

* the shared poll cadence (no per-feature timers)
* per-kind exception isolation — one bad item or kind can't starve the
  others (the work source isolates kinds while listing, too)
* the reprocess-request lifecycle dance — cancel propagation and
  status reporting for in-flight rows, which aren't discrete enqueueable
  jobs
* still TTT-specific until plan phase 5 (IDeviceLink): service
  registration + heartbeat for the processing-job feature and the schedule
  refresh, on the TTT client and behind the ``ttt.enabled`` +
  ``is_authenticated`` gate, exactly as before

The processors handle the heavyweight work (FFmpeg, uploads, etc.) and
do that inside :meth:`QueueProcessor.process_item`, gated by the shared
:class:`~video_grouper.pipeline.resources.ResourceManager`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import platform
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..integrations.work import (
    CLIP,
    HIGHLIGHT_REEL,
    PROCESS_RECORDING,
    REPROCESS,
    IWorkSource,
    WorkItem,
)
from ..integrations.work.sources.null import NullWorkSource
from ..utils.config import Config
from .base_polling_processor import PollingProcessor
from .tasks.clip.clip_request_task import ClipRequestTask
from .tasks.ttt.highlight_reel_task import HighlightReelTask
from .tasks.ttt.reprocess_request_task import ReprocessRequestTask
from .tasks.ttt.ttt_job_task import TTTJobTask

if TYPE_CHECKING:
    from video_grouper.api_integrations.ttt_api import TTTApiClient

    from .clip_request_processor import ClipRequestProcessor
    from .highlight_reel_processor import HighlightReelProcessor
    from .reprocess_request_processor import ReprocessRequestProcessor
    from .ttt_job_processor import TTTJobProcessor

logger = logging.getLogger(__name__)

# The task each kind of work is queued as (the queue files store these).
_TASKS = {
    CLIP: ClipRequestTask,
    HIGHLIGHT_REEL: HighlightReelTask,
    PROCESS_RECORDING: TTTJobTask,
    REPROCESS: ReprocessRequestTask,
}


class TTTPoller(PollingProcessor):
    """Polls the work source and enqueues work onto each kind's QueueProcessor."""

    def __init__(
        self,
        storage_path: str,
        config: Config,
        ttt_client: TTTApiClient | None = None,
        *,
        work: IWorkSource | None = None,
        clip_request_processor: ClipRequestProcessor | None = None,
        highlight_reel_processor: HighlightReelProcessor | None = None,
        ttt_job_processor: TTTJobProcessor | None = None,
        reprocess_request_processor: ReprocessRequestProcessor | None = None,
        schedule_service=None,
        poll_interval: int = 60,
    ):
        super().__init__(storage_path, config, poll_interval)
        # Service register / heartbeat + schedule only (IDeviceLink, phase 5).
        self.ttt_client = ttt_client
        self.work: IWorkSource = work or NullWorkSource()
        self.clip_request_processor = clip_request_processor
        self.highlight_reel_processor = highlight_reel_processor
        self.ttt_job_processor = ttt_job_processor
        self.reprocess_request_processor = reprocess_request_processor
        self.schedule_service = schedule_service
        # kind -> the processor that runs it, for the kinds that are wired.
        self._processors = {
            kind: proc
            for kind, proc in (
                (CLIP, clip_request_processor),
                (HIGHLIGHT_REEL, highlight_reel_processor),
                (PROCESS_RECORDING, ttt_job_processor),
                (REPROCESS, reprocess_request_processor),
            )
            if proc is not None
        }
        # TTT-job service registration + heartbeat live here.
        self._service_id: str | None = None
        # Reprocess-request lifecycle tracking — request_id -> group_dir.
        self._reprocess_tracked: dict[str, Path] = {}

        wired = [
            n
            for n, p in (
                ("clip_requests", clip_request_processor),
                ("highlight_reels", highlight_reel_processor),
                ("ttt_jobs", ttt_job_processor),
                ("reprocess_requests", reprocess_request_processor),
                ("schedule", schedule_service),
            )
            if p is not None
        ]
        logger.info("TTTPoller: %d feature(s) wired (%s)", len(wired), ", ".join(wired))

    async def discover_work(self) -> None:
        """One tick: device link (TTT, gated), then one ``pending()`` for the
        wired kinds, then the schedule refresh (TTT, gated). Exceptions are
        caught + logged per step and per item so one bad endpoint or item
        can't starve the others."""
        ttt_ready = self._ttt_session_ready()
        if ttt_ready and self.ttt_job_processor is not None:
            # Service registration + heartbeat — owned by the poller because
            # they're cheap calls that naturally belong on the poll cadence
            # (and the processor itself doesn't need them to run a job).
            # TTT client calls until IDeviceLink takes them (plan phase 5).
            await self._safe_poll("ttt_jobs", self._register_and_heartbeat)
        await self._safe_poll("work", self._poll_work)
        if ttt_ready:
            await self._safe_poll("schedule", self._poll_schedule)

    def _ttt_session_ready(self) -> bool:
        """The TTT gate for the calls still made on the TTT client."""
        ttt_cfg = getattr(self.config, "ttt", None)
        if ttt_cfg is None or not getattr(ttt_cfg, "enabled", False):
            return False
        if self.ttt_client is None or not self.ttt_client.is_authenticated():
            logger.debug("TTTPoller: TTT client not authenticated, skipping TTT calls")
            return False
        return True

    async def _safe_poll(self, name: str, fn) -> None:
        try:
            await fn()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "TTTPoller: %s poll raised; other features unaffected", name
            )

    # ------------------------------------------------------------------
    # Work: one pending() per tick, dispatched by kind
    # ------------------------------------------------------------------

    async def _poll_work(self) -> None:
        if not self._processors:
            return
        for item in await self.work.pending(list(self._processors)):
            try:
                await self._dispatch(item)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "TTTPoller: %s %s raised; other work unaffected",
                    item.kind,
                    item.id,
                )

    async def _dispatch(self, item: WorkItem) -> None:
        processor = self._processors.get(item.kind)
        if processor is None:
            return
        if item.kind == REPROCESS and item.status != "pending":
            if item.status in ("claimed", "running"):
                # In-flight rows: handle cancel propagation + progress
                # reporting inline. Light I/O, no queue needed.
                await self._propagate_cancel_if_set(item)
                await self._report_progress_if_changed(item)
            return
        await processor.add_work(
            _TASKS[item.kind](ttt_id=item.id, payload=item.payload)
        )

    async def _poll_schedule(self) -> None:
        if self.schedule_service is None:
            return
        await asyncio.to_thread(self.schedule_service.refresh)

    # ------------------------------------------------------------------
    # TTTJob: service registration + heartbeat (TTT client; IDeviceLink in
    # plan phase 5)
    # ------------------------------------------------------------------

    async def _register_and_heartbeat(self) -> None:
        await self._maybe_register_service()
        await self._maybe_heartbeat()

    async def _maybe_register_service(self) -> None:
        if self._service_id is not None:
            return
        try:
            machine_name = self.config.ttt.machine_name or platform.node()
            capabilities = {
                "ffmpeg": True,
                "pipeline": self.config.pipeline.is_active(),
                "camera_type": self.config.camera.type,
                "camera_ip": self.config.camera.device_ip,
            }
            result = await asyncio.to_thread(
                self.ttt_client.register_service, machine_name, capabilities
            )
            self._service_id = result.get("id") if result else None
            logger.info(
                "TTT_JOBS: registered service as '%s' (id=%s)",
                machine_name,
                self._service_id,
            )
        except Exception as exc:
            logger.error("TTT_JOBS: failed to register service: %s", exc)

    async def _maybe_heartbeat(self) -> None:
        if not self._service_id:
            return
        try:
            await asyncio.to_thread(
                self.ttt_client.send_heartbeat, self._service_id, "online"
            )
        except Exception as exc:
            logger.debug("TTT_JOBS: heartbeat failed: %s", exc)

    # ------------------------------------------------------------------
    # Reprocess: inline cancel + progress reporting for in-flight rows
    # ------------------------------------------------------------------

    async def _propagate_cancel_if_set(self, item: WorkItem) -> None:
        if not item.payload.get("cancel_requested"):
            return
        req_id = item.id
        group_dir = self._reprocess_tracked.get(req_id)
        if group_dir is None:
            group_dir = await self._resolve_reprocess_group_dir(item)
            if group_dir is None:
                return
            self._reprocess_tracked[req_id] = group_dir
        from video_grouper.pipeline.reprocess import write_cancel_request

        write_cancel_request(group_dir)
        logger.info("reprocess: propagated cancel for %s to %s", req_id, group_dir)

    async def _report_progress_if_changed(self, item: WorkItem) -> None:
        req_id = item.id
        group_dir = self._reprocess_tracked.get(req_id)
        if group_dir is None:
            group_dir = await self._resolve_reprocess_group_dir(item)
            if group_dir is None:
                return
            self._reprocess_tracked[req_id] = group_dir

        local = self._read_local_status(group_dir)
        new_status, current_step, error = self._map_local_to_ttt(local)
        if new_status is None or new_status == item.status:
            return
        try:
            if new_status == "running":
                await self.work.progress(item, current_step or "")
            elif new_status == "completed":
                await self.work.complete(item, {})
            elif new_status == "cancelled":
                await self.work.cancelled(item)
            else:
                await self.work.fail(item, error)
        except Exception as exc:
            logger.warning(
                "reprocess: failed to report %s -> %s (%s)", req_id, new_status, exc
            )
            return
        if new_status in ("completed", "cancelled", "failed"):
            self._reprocess_tracked.pop(req_id, None)

    async def _resolve_reprocess_group_dir(self, item: WorkItem) -> Path | None:
        try:
            recording = (await self.work.details(item)).get("recording") or {}
        except Exception:
            return None
        file_group = recording.get("file_group")
        if not file_group:
            return None
        candidate = Path(self.storage_path) / file_group
        return candidate if candidate.is_dir() else None

    @staticmethod
    def _read_local_status(group_dir: Path) -> dict[str, Any]:
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
