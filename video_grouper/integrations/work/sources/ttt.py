"""Team Tech Tools work source.

Maps each verb x kind onto the TTT client (the one shared session):

================  ======================  =====================  =================  ==============================
verb              clip                    highlight_reel         process_recording  reprocess
================  ======================  =====================  =================  ==============================
pending           get_pending_clip_       get_pending_           get_pending_jobs   get_reprocess_queue (pending +
                  requests                highlights(camera_id)                     this user's claimed / running)
claim             start_clip_request      claim_highlight        claim_job          claim_reprocess_request
                  ("Cannot start" = ours  (None on 409)
                  already, carry on)
details           --                      get_highlight_game_    --                 get_camera_recording
                                          clips / _moment_clips                     (payload recording_id)
                                          (by payload source)
latest            --                      get_highlight          --                 --
progress          --                      update_highlight_      update_job_        update_reprocess_status
                                          progress               progress           (running, step)
blocked           --                      report_blocker         --                 --
complete          fulfill_clip_request    complete_highlight     complete_job       update_reprocess_status
                                                                                    (completed)
fail              --                      fail_highlight         fail_job           update_reprocess_status
                                          (error[:500])                             (failed, error)
cancelled         --                      --                     --                 update_reprocess_status
                                                                                    (cancelled)
================  ======================  =====================  =================  ==============================

"--" raises ``NotImplementedError``: TTT has no such call, and no processor
makes it. Client errors propagate unchanged (each processor keeps its own
policy); ``pending`` alone isolates kinds and returns nothing while signed out.
``camera_id`` is ``[TTT] camera_id``, read on each call.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Collection
from typing import Any

from video_grouper.integrations.registry import provides

from ..base import (
    CLIP,
    HIGHLIGHT_REEL,
    KINDS,
    PROCESS_RECORDING,
    REPROCESS,
    IWorkSource,
    WorkItem,
)

logger = logging.getLogger(__name__)


@provides(IWorkSource)
class TTTWorkSource(IWorkSource):
    name = "ttt"
    priority = 10

    def __init__(self, client: Any, ttt_config: Any = None):
        self._client = client
        self._ttt_config = ttt_config

    @classmethod
    def from_context(cls, context) -> TTTWorkSource | None:
        if not context.ttt_enabled():
            return None
        return cls(context.ttt_client(), context.config.ttt)

    # -- helpers -------------------------------------------------------------

    @property
    def _camera_id(self) -> str | None:
        return getattr(self._ttt_config, "camera_id", None)

    def _signed_in(self) -> bool:
        try:
            return bool(self._client.is_authenticated())
        except Exception:
            return False

    @staticmethod
    def _unsupported(verb: str, item: WorkItem) -> NotImplementedError:
        return NotImplementedError(f"TTT has no '{verb}' for {item.kind} work")

    def _list(self, kind: str) -> Any:
        if kind == CLIP:
            return self._client.get_pending_clip_requests()
        if kind == HIGHLIGHT_REEL:
            return self._client.get_pending_highlights(self._camera_id or None)
        if kind == PROCESS_RECORDING:
            return self._client.get_pending_jobs()
        return self._client.get_reprocess_queue()

    def _reprocess_status(
        self,
        item: WorkItem,
        status: str,
        step: str | None = None,
        error: str | None = None,
    ) -> Awaitable[Any]:
        return asyncio.to_thread(
            self._client.update_reprocess_status, item.id, status, step, error
        )

    # -- port ----------------------------------------------------------------

    async def pending(self, kinds: Collection[str] | None = None) -> list[WorkItem]:
        wanted = [k for k in KINDS if kinds is None or k in kinds]
        if not wanted or not self._signed_in():
            return []
        items: list[WorkItem] = []
        for kind in wanted:
            try:
                rows = await asyncio.to_thread(self._list, kind)
            except Exception as exc:
                if kind == REPROCESS:
                    # 403 = "not a camera-manager for any team" -- quiet log.
                    logger.debug("reprocess: queue poll failed (%s)", exc)
                else:
                    logger.exception(
                        "TTT work: %s poll raised; other kinds unaffected", kind
                    )
                continue
            for row in rows or []:
                row_id = row.get("id")
                if not row_id:
                    continue
                # Only the reprocess queue lists in-flight rows; the others
                # list pending work only.
                status = (row.get("status") or "") if kind == REPROCESS else "pending"
                items.append(WorkItem(kind, str(row_id), row, status))
        return items

    async def claim(self, item: WorkItem) -> dict[str, Any] | None:
        if item.kind == CLIP:
            try:
                result = await asyncio.to_thread(
                    self._client.start_clip_request, item.id
                )
            except Exception as exc:
                if "Cannot start" not in str(exc):
                    raise
                result = None  # already in_progress: ours, being retried
            return result if isinstance(result, dict) else {}
        if item.kind == HIGHLIGHT_REEL:
            # None on 409: another camera-manager has it.
            return await asyncio.to_thread(
                self._client.claim_highlight, item.id, self._camera_id or ""
            )
        if item.kind == PROCESS_RECORDING:
            result = await asyncio.to_thread(self._client.claim_job, item.id)
            return result if isinstance(result, dict) else {}
        if item.kind == REPROCESS:
            result = await asyncio.to_thread(
                self._client.claim_reprocess_request, item.id
            )
            return result if isinstance(result, dict) else {}
        raise self._unsupported("claim", item)

    async def details(self, item: WorkItem) -> dict[str, Any]:
        if item.kind == HIGHLIGHT_REEL:
            if item.payload.get("source", "manual") == "moment_tagger":
                fetch = self._client.get_highlight_moment_clips
            else:
                fetch = self._client.get_highlight_game_clips
            clips = await asyncio.to_thread(fetch, item.id)
            return {"clips": clips or []}
        if item.kind == REPROCESS:
            recording = await asyncio.to_thread(
                self._client.get_camera_recording, item.payload["recording_id"]
            )
            return {"recording": recording or {}}
        raise self._unsupported("details", item)

    async def latest(self, item: WorkItem) -> dict[str, Any]:
        if item.kind == HIGHLIGHT_REEL:
            reel = await asyncio.to_thread(self._client.get_highlight, item.id)
            return reel or {}
        raise self._unsupported("latest", item)

    async def progress(
        self,
        item: WorkItem,
        step: str,
        pct: float | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        if item.kind == HIGHLIGHT_REEL:
            await asyncio.to_thread(
                self._client.update_highlight_progress,
                item.id,
                stage=step,
                percent=int(pct) if pct is not None else 0,
            )
            return
        if item.kind == PROCESS_RECORDING:
            progress: dict[str, Any] = {} if pct is None else {"percent": pct}
            progress.update(detail or {})
            await asyncio.to_thread(
                self._client.update_job_progress, item.id, step, progress
            )
            return
        if item.kind == REPROCESS:
            await self._reprocess_status(item, "running", step)
            return
        raise self._unsupported("progress", item)

    async def blocked(
        self, item: WorkItem, reason: str, detail: dict[str, Any] | None = None
    ) -> None:
        if item.kind == HIGHLIGHT_REEL:
            await asyncio.to_thread(
                self._client.report_blocker, item.id, self._camera_id, reason
            )
            return
        raise self._unsupported("blocked", item)

    async def complete(self, item: WorkItem, result: dict[str, Any]) -> None:
        if item.kind == CLIP:
            await asyncio.to_thread(
                self._client.fulfill_clip_request,
                item.id,
                result["url"],
                result.get("notes"),
            )
            return
        if item.kind == HIGHLIGHT_REEL:
            await asyncio.to_thread(
                self._client.complete_highlight,
                item.id,
                file_path=result["file_path"],
                youtube_video_id=result.get("youtube_video_id"),
            )
            return
        if item.kind == PROCESS_RECORDING:
            await asyncio.to_thread(self._client.complete_job, item.id, result)
            return
        if item.kind == REPROCESS:
            await self._reprocess_status(item, "completed")
            return
        raise self._unsupported("complete", item)

    async def fail(self, item: WorkItem, error: str | None) -> None:
        if item.kind == HIGHLIGHT_REEL:
            # Capped at 500 characters, as the reel processor always sent it.
            await asyncio.to_thread(
                self._client.fail_highlight, item.id, (error or "")[:500]
            )
            return
        if item.kind == PROCESS_RECORDING:
            await asyncio.to_thread(self._client.fail_job, item.id, error or "")
            return
        if item.kind == REPROCESS:
            await self._reprocess_status(item, "failed", None, error)
            return
        raise self._unsupported("fail", item)

    async def cancelled(self, item: WorkItem) -> None:
        if item.kind == REPROCESS:
            await self._reprocess_status(item, "cancelled")
            return
        raise self._unsupported("cancelled", item)
