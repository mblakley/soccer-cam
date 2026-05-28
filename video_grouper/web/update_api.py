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
    def post_apply() -> JSONResponse:
        # Phase 1 boundary: there is no safe install path yet. The
        # tray and any other caller can wire this endpoint into their
        # UI today; the response 503 signals "feature staged but not
        # yet operational" so callers don't silently no-op.
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "unavailable",
                "reason": (
                    "Auto-install path is being rewritten in Phase 2 "
                    "(re-run NSIS installer). Pending updates are "
                    "visible at GET /api/update/status but cannot be "
                    "applied automatically yet."
                ),
                "pending_version": processor._pending_version,
            },
        )

    return router
