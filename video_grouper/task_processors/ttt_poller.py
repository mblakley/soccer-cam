"""Single TTT polling entrypoint.

Per the project's "one PollingProcessor for TTT" invariant: every
TTT-cloud feature (clip requests, highlight reels, reprocess requests,
plugin jobs) is queued onto a dedicated :class:`QueueProcessor`, and
this class is the ONE thing that polls TTT for new work and enqueues
it. It also owns:

* the ``ttt.enabled`` + ``is_authenticated`` gate
* the shared poll cadence (no per-feature timers)
* per-feature exception isolation — one bad TTT endpoint can't starve
  the others
* service registration + heartbeat for the TTTJob feature (cheap calls
  that naturally belong on the poll cadence)
* the reprocess-request lifecycle dance — cancel propagation and
  status reporting for in-flight rows, which aren't discrete enqueueable
  jobs

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


class TTTPoller(PollingProcessor):
    """Polls TTT and enqueues work onto each feature's QueueProcessor."""

    def __init__(
        self,
        storage_path: str,
        config: Config,
        ttt_client: TTTApiClient,
        *,
        clip_request_processor: ClipRequestProcessor | None = None,
        highlight_reel_processor: HighlightReelProcessor | None = None,
        ttt_job_processor: TTTJobProcessor | None = None,
        reprocess_request_processor: ReprocessRequestProcessor | None = None,
        poll_interval: int = 60,
    ):
        super().__init__(storage_path, config, poll_interval)
        self.ttt_client = ttt_client
        self.clip_request_processor = clip_request_processor
        self.highlight_reel_processor = highlight_reel_processor
        self.ttt_job_processor = ttt_job_processor
        self.reprocess_request_processor = reprocess_request_processor
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
            )
            if p is not None
        ]
        logger.info("TTTPoller: %d feature(s) wired (%s)", len(wired), ", ".join(wired))

    async def discover_work(self) -> None:
        """One tick: gate on enabled + auth, then poll each feature.
        Per-feature exceptions are caught + logged so one bad endpoint
        can't starve the others."""
        ttt_cfg = getattr(self.config, "ttt", None)
        if ttt_cfg is None or not getattr(ttt_cfg, "enabled", False):
            return
        if not self.ttt_client.is_authenticated():
            logger.debug("TTTPoller: TTT client not authenticated, skipping tick")
            return

        await self._safe_poll("clip_requests", self._poll_clip_requests)
        await self._safe_poll("highlight_reels", self._poll_highlight_reels)
        await self._safe_poll("ttt_jobs", self._poll_ttt_jobs)
        await self._safe_poll("reprocess_requests", self._poll_reprocess_requests)

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
    # Per-feature polling
    # ------------------------------------------------------------------

    async def _poll_clip_requests(self) -> None:
        if self.clip_request_processor is None:
            return
        requests = await asyncio.to_thread(self.ttt_client.get_pending_clip_requests)
        for req in requests or []:
            req_id = req.get("id")
            if not req_id:
                continue
            await self.clip_request_processor.add_work(
                ClipRequestTask(ttt_id=req_id, payload=req)
            )

    async def _poll_highlight_reels(self) -> None:
        if self.highlight_reel_processor is None:
            return
        camera_id = getattr(self.config.ttt, "camera_id", None) or None
        reels = await asyncio.to_thread(
            self.ttt_client.get_pending_highlights, camera_id
        )
        for reel in reels or []:
            reel_id = reel.get("id")
            if not reel_id:
                continue
            await self.highlight_reel_processor.add_work(
                HighlightReelTask(ttt_id=reel_id, payload=reel)
            )

    async def _poll_ttt_jobs(self) -> None:
        if self.ttt_job_processor is None:
            return
        # Service registration + heartbeat — owned by the poller because
        # they're cheap calls that naturally belong on the poll cadence
        # (and the processor itself doesn't need them to run a job).
        await self._maybe_register_service()
        await self._maybe_heartbeat()

        jobs = await asyncio.to_thread(self.ttt_client.get_pending_jobs)
        for job in jobs or []:
            job_id = job.get("id")
            if not job_id:
                continue
            await self.ttt_job_processor.add_work(
                TTTJobTask(ttt_id=job_id, payload=job)
            )

    async def _poll_reprocess_requests(self) -> None:
        if self.reprocess_request_processor is None:
            return
        try:
            queue = await asyncio.to_thread(self.ttt_client.get_reprocess_queue)
        except Exception as exc:
            # 403 = "not a camera-manager for any team" — quiet log.
            logger.debug("reprocess: queue poll failed (%s)", exc)
            return

        for req in queue or []:
            status = req.get("status")
            req_id = req.get("id")
            if not req_id:
                continue
            if status == "pending":
                await self.reprocess_request_processor.add_work(
                    ReprocessRequestTask(ttt_id=req_id, payload=req)
                )
            elif status in ("claimed", "running"):
                # In-flight rows: handle cancel propagation + progress
                # reporting inline. Light I/O, no queue needed.
                await self._propagate_cancel_if_set(req)
                await self._report_progress_if_changed(req)

    # ------------------------------------------------------------------
    # TTTJob: service registration + heartbeat
    # ------------------------------------------------------------------

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

    async def _propagate_cancel_if_set(self, req: dict[str, Any]) -> None:
        if not req.get("cancel_requested"):
            return
        req_id = req["id"]
        group_dir = self._reprocess_tracked.get(req_id)
        if group_dir is None:
            group_dir = await self._resolve_reprocess_group_dir(req)
            if group_dir is None:
                return
            self._reprocess_tracked[req_id] = group_dir
        from video_grouper.pipeline.reprocess import write_cancel_request

        write_cancel_request(group_dir)
        logger.info("reprocess: propagated cancel for %s to %s", req_id, group_dir)

    async def _report_progress_if_changed(self, req: dict[str, Any]) -> None:
        req_id = req["id"]
        group_dir = self._reprocess_tracked.get(req_id)
        if group_dir is None:
            group_dir = await self._resolve_reprocess_group_dir(req)
            if group_dir is None:
                return
            self._reprocess_tracked[req_id] = group_dir

        local = self._read_local_status(group_dir)
        new_status, current_step, error = self._map_local_to_ttt(local)
        if new_status is None or new_status == req.get("status"):
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
        if new_status in ("completed", "cancelled", "failed"):
            self._reprocess_tracked.pop(req_id, None)

    async def _resolve_reprocess_group_dir(self, req: dict[str, Any]) -> Path | None:
        try:
            recording = await asyncio.to_thread(
                self.ttt_client.get_camera_recording, req["recording_id"]
            )
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
