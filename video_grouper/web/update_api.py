"""HTTP surface for the auto-upgrade subsystem.

Three endpoints, all under ``/api/update/``:

- ``GET /status`` — what the tray polls. Snapshots the
  ``UpdateCheckProcessor`` state plus the last few journal entries so
  the dashboard / tray can show "v0.3.7 detected, deferred until
  idle" without having to read log files.
- ``POST /check-now`` — pokes the processor's polling loop to run a
  check immediately instead of waiting up to an hour. Wired to a
  hidden dashboard button + used by E2E tests.
- ``POST /apply`` — Phase 1 placeholder. Returns 202 ``not yet
  available — Phase 2 wires this to spawn the NSIS installer``.
  Existing today so the tray UI flow can be built and tested against
  the real route shape.

The router is mounted by ``video_grouper.web.auth_server.create_app``
when an ``update_processor`` is supplied.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from video_grouper.task_processors.update_check_processor import UpdateCheckProcessor
from video_grouper.update.journal import read_latest_entries

logger = logging.getLogger(__name__)


def build_router(processor: UpdateCheckProcessor) -> APIRouter:
    router = APIRouter(prefix="/api/update", tags=["update"])

    @router.get("/status")
    def get_status() -> dict[str, Any]:
        snapshot = processor.build_status()
        body = asdict(snapshot)
        # Always include the last 5 history entries so the dashboard
        # can render a mini-timeline without a second request. Cheap:
        # the journal is one tiny file we tail in memory.
        body["history"] = read_latest_entries(processor.storage_path, limit=5)
        return body

    @router.post("/check-now", status_code=status.HTTP_202_ACCEPTED)
    def post_check_now() -> dict[str, str]:
        processor.request_immediate_check()
        return {"status": "scheduled"}

    @router.post("/apply", status_code=status.HTTP_202_ACCEPTED)
    async def post_apply() -> JSONResponse:
        """Drive the staged installer. Returns 202 on a successful
        spawn (the service will shut down moments later as NSIS takes
        over), 409 when nothing is staged, 503 when the pipeline is
        currently busy. Idempotent only in the sense that a second
        click while the first install is in flight is harmless --
        the service may already be exiting."""
        ok, message = await processor.apply_pending()
        if ok:
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content={
                    "status": "spawned",
                    "pending_version": processor._pending_version,
                },
            )
        # Pick a status code from the message shape. Pipeline-busy
        # is retryable (503); missing pending is not (409).
        if message.lower().startswith("pipeline busy"):
            code = status.HTTP_503_SERVICE_UNAVAILABLE
        else:
            code = status.HTTP_409_CONFLICT
        return JSONResponse(
            status_code=code,
            content={
                "status": "rejected",
                "reason": message,
                "pending_version": processor._pending_version,
            },
        )

    return router
