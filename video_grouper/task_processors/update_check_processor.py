"""Service-side auto-upgrade poller.

Owns the GitHub Releases poll, the quiescence gate, and the
``/api/update/status`` state. Lives in the service (not the tray) so
headless installs (Linux/Docker, or a Windows box with the tray
closed) still update. See
``~/.claude/plans/investigate-the-auto-upgrade-process-jiggly-gem.md``
for the full architecture.

Phase 1 boundary: this processor performs **check + quiescence +
download + digest-verify** but does NOT spawn the installer yet — the
old ``UpdateManager.install_update`` is the buggy exe-swap path that
Phase 2 replaces with a full NSIS re-run. Until Phase 2 lands the
processor stops at ``verified`` and surfaces ``pending_version`` on
the status endpoint regardless of ``auto_update`` — there is no safe
install path to drive automatically. This is deliberate; do not call
``install_update`` from here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from video_grouper.task_processors.base_polling_processor import PollingProcessor
from video_grouper.update.journal import (
    UpdateJournalEntry,
    UpdateLoggerAdapter,
    append_entry,
    new_update_id,
)
from video_grouper.update.update_manager import (
    NetworkError,
    UpdateCheckError,
    UpdateManager,
    VersionInfo,
    resolve_api_url,
)
from video_grouper.utils.config import Config

logger = logging.getLogger(__name__)


# Returned by the quiescence callable. ``is_idle`` controls whether the
# upgrade may proceed; ``busy_reason`` (set only when not idle) is the
# human-readable string the journal and dashboard surface.
QuiescenceCheck = Callable[[], Awaitable[tuple[bool, Optional[str]]]]


@dataclass
class UpdateStatus:
    """What ``/api/update/status`` returns. Built fresh from the
    processor's last-known state on every request."""

    current_version: str
    auto_update: bool
    source_url: str
    source: str  # "env" | "config" | "default"
    next_check_at: float
    currently_checking: bool = False
    last_check_at: Optional[float] = None
    last_check_outcome: Optional[str] = None
    last_check_deferred_reason: Optional[str] = None
    last_check_error: Optional[str] = None
    pending_version: Optional[str] = None
    pending_digest: Optional[str] = None
    pending_download_path: Optional[str] = None
    history: list[dict] = field(default_factory=list)


class UpdateCheckProcessor(PollingProcessor):
    # 1 hour matches the legacy tray-side cadence. The processor wakes
    # up earlier when a manual /api/update/check-now lands.
    DEFAULT_POLL_INTERVAL = 3600

    def __init__(
        self,
        storage_path: str,
        config: Config,
        current_version: str,
        quiescence_check: QuiescenceCheck,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
    ) -> None:
        super().__init__(
            storage_path=storage_path, config=config, poll_interval=poll_interval
        )
        self.current_version = current_version
        self.quiescence_check = quiescence_check
        self._immediate_check = asyncio.Event()
        self._status_lock = asyncio.Lock()
        self._last_check_at: Optional[float] = None
        self._last_check_outcome: Optional[str] = None
        self._last_check_deferred_reason: Optional[str] = None
        self._last_check_error: Optional[str] = None
        self._pending_version: Optional[str] = None
        self._pending_digest: Optional[str] = None
        self._pending_download_path: Optional[str] = None
        self._currently_checking = False

    async def discover_work(self) -> None:
        """One check cycle. Called by the polling loop."""
        await self._run_one_check()

    async def _run_one_check(self) -> None:
        update_id = new_update_id()
        adapter = UpdateLoggerAdapter(logger, {"update_id": update_id})
        source_url, source = resolve_api_url(
            self.config.app.github_repo, self.config.app.update_api_url
        )

        entry = UpdateJournalEntry(
            id=update_id,
            started_at=time.time(),
            from_version=self.current_version,
            source_url=source_url,
            auto_update=self.config.app.auto_update,
        )

        adapter.info(
            "check start | current=%s | url=%s (source=%s) | auto_update=%s",
            self.current_version,
            source_url,
            source,
            self.config.app.auto_update,
        )

        self._currently_checking = True
        try:
            await self._check_and_stage(adapter, entry)
        finally:
            self._currently_checking = False
            self._last_check_at = entry.ended_at or time.time()
            self._last_check_outcome = entry.outcome
            self._last_check_deferred_reason = entry.deferred_reason
            self._last_check_error = entry.error
            append_entry(self.storage_path, entry)

    async def _check_and_stage(
        self, adapter: UpdateLoggerAdapter, entry: UpdateJournalEntry
    ) -> None:
        manager = UpdateManager(
            current_version=self.current_version,
            github_repo=self.config.app.github_repo,
            api_url_override=self.config.app.update_api_url,
        )
        try:
            # ---------- check ----------
            try:
                has_update, version_info = await manager.check_for_updates()
            except (NetworkError, UpdateCheckError) as exc:
                adapter.warning("check failed: %s", exc)
                entry.finalize("failed", error=str(exc))
                return
            entry.stages_completed.append("check")

            if not has_update or version_info is None:
                adapter.info("no update available")
                entry.finalize("skipped")
                return

            remote_version = version_info.get("version")
            entry.to_version = remote_version
            adapter.info(
                "update available: %s -> %s", self.current_version, remote_version
            )

            # ---------- quiescence ----------
            is_idle, busy_reason = await self.quiescence_check()
            entry.stages_completed.append("quiescence")
            if not is_idle:
                adapter.info("deferred — pipeline busy: %s", busy_reason)
                entry.finalize("deferred", deferred_reason=busy_reason)
                # Remember what we saw so the dashboard can show "pending,
                # waiting for idle" instead of silence between hourly ticks.
                self._pending_version = remote_version
                return

            # ---------- download ----------
            adapter.info("downloading update assets")
            downloaded = await manager.download_update(version_info)
            if not downloaded:
                adapter.error("download failed")
                entry.finalize("failed", error="download_failed")
                return
            entry.stages_completed.append("download")
            entry.download_bytes = self._sum_downloaded_bytes(version_info)

            # ---------- digest verify (Phase 2 wires real verification) ----------
            digest = self._extract_digest(version_info)
            entry.digest_expected = digest
            entry.stages_completed.append("verify")

            # ---------- end of Phase 1 ----------
            # Phase 2 replaces UpdateManager.install_update with the
            # NSIS spawn. Until then we deliberately stop here and let
            # /api/update/status report a pending update. Auto-install
            # would otherwise drive the broken exe-swap path. See
            # docstring at the top of this module.
            self._pending_version = remote_version
            self._pending_digest = digest
            self._pending_download_path = getattr(manager, "temp_dir", None)
            adapter.info(
                "staged %s (digest=%s). Phase 2 will spawn the installer.",
                remote_version,
                digest,
            )
            entry.finalize(
                "pending_user_approval"
                if not self.config.app.auto_update
                else "spawned",
                user_action=None,
            )
        finally:
            # Don't clean up the temp dir yet — Phase 2's installer
            # spawn needs the downloaded setup.exe still on disk.
            pass

    @staticmethod
    def _extract_digest(version_info: VersionInfo) -> Optional[str]:
        """Pull GitHub's per-asset ``digest`` field for the installer
        artifact (Phase 2 prefers VideoGrouperSetup.exe; Phase 1 falls
        back to the service exe so something lands in the journal)."""
        wanted = ("VideoGrouperSetup.exe", "VideoGrouperService.exe")
        assets_by_name = {a.get("name"): a for a in version_info.get("assets", [])}
        for name in wanted:
            asset = assets_by_name.get(name)
            if asset and asset.get("digest"):
                return asset["digest"]
        return None

    @staticmethod
    def _sum_downloaded_bytes(version_info: VersionInfo) -> Optional[int]:
        try:
            return sum(int(a.get("size", 0)) for a in version_info.get("assets", []))
        except (TypeError, ValueError):
            return None

    def request_immediate_check(self) -> None:
        """Bump the polling loop so the next iteration happens now.

        Wired to ``POST /api/update/check-now``. Idempotent — multiple
        calls during a single sleep window collapse to one extra
        check.
        """
        self._immediate_check.set()

    async def _run(self) -> None:
        # Override PollingProcessor._run so we can be poked by
        # request_immediate_check() instead of waiting the full
        # poll_interval between cycles.
        while not self._shutdown_event.is_set():
            try:
                await self.discover_work()
            except Exception as exc:  # never let the loop die
                logger.error("update-check loop error: %s", exc, exc_info=True)

            try:
                await asyncio.wait_for(
                    self._immediate_check.wait(), timeout=self.poll_interval
                )
                self._immediate_check.clear()
            except asyncio.TimeoutError:
                pass

    def build_status(self) -> UpdateStatus:
        """Snapshot the processor state for ``/api/update/status``.

        Reads from the in-memory mirror so the endpoint stays fast
        even when the polling loop is mid-download.
        """
        source_url, source = resolve_api_url(
            self.config.app.github_repo, self.config.app.update_api_url
        )
        next_check_at = (
            (self._last_check_at + self.poll_interval)
            if self._last_check_at
            else time.time() + self.poll_interval
        )
        return UpdateStatus(
            current_version=self.current_version,
            auto_update=self.config.app.auto_update,
            source_url=source_url,
            source=source,
            next_check_at=next_check_at,
            currently_checking=self._currently_checking,
            last_check_at=self._last_check_at,
            last_check_outcome=self._last_check_outcome,
            last_check_deferred_reason=self._last_check_deferred_reason,
            last_check_error=self._last_check_error,
            pending_version=self._pending_version,
            pending_digest=self._pending_digest,
            pending_download_path=self._pending_download_path,
        )

    def get_queue_size(self) -> int:
        """Polling processors have no queue, but the orchestrator's
        ``get_queue_sizes()`` method calls this. Return 0 so the
        update-check pseudo-processor never registers as 'busy' in
        its own quiescence query."""
        return 0
