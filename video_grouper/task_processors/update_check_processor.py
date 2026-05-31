"""Service-side auto-upgrade poller.

Owns the GitHub Releases poll, the quiescence gate, and the
``/api/update/status`` state. Lives in the service (not the tray) so
headless installs (Linux/Docker, or a Windows box with the tray
closed) still update. See
``~/.claude/plans/investigate-the-auto-upgrade-process-jiggly-gem.md``.

Flow:

  check → quiescence → download → verify_digest → (spawn → shutdown)

The terminal step is gated by ``config.app.auto_update``:

  - ``true`` (default, Chrome-style): once the digest verifies, the
    processor spawns ``VideoGrouperSetup.exe /S`` detached and
    triggers a clean service shutdown. NSIS takes over and brings
    the new service + tray back up.
  - ``false``: stop after verify. The artifact stays on disk under
    ``pending_download_path``; the tray polls
    ``/api/update/status``, surfaces "Install v...", and the user's
    click drives ``POST /api/update/apply`` which calls
    ``apply_pending()`` here.

The legacy ``UpdateManager.install_update`` exe-swap path is gone --
``spawn_installer`` is the only install entry point.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from video_grouper.task_processors.base_polling_processor import PollingProcessor
from video_grouper.update.journal import (
    UpdateJournalEntry,
    UpdateLoggerAdapter,
    append_entry,
    new_update_id,
)
from video_grouper.update.nsis_marker import read_and_clear_marker
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
QuiescenceCheck = Callable[[], Awaitable[tuple[bool, str | None]]]


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
    last_check_at: float | None = None
    last_check_outcome: str | None = None
    last_check_deferred_reason: str | None = None
    last_check_error: str | None = None
    pending_version: str | None = None
    pending_digest: str | None = None
    pending_download_path: str | None = None
    nsis_phase_from_last_install: str | None = None
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
        shutdown_callback: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(
            storage_path=storage_path, config=config, poll_interval=poll_interval
        )
        self.current_version = current_version
        self.quiescence_check = quiescence_check
        # Called after a successful spawn so the service can exit
        # cleanly. NSIS will SCM-stop us anyway, but exiting first
        # avoids a forced termination mid-coroutine.
        self.shutdown_callback = shutdown_callback
        self._immediate_check = asyncio.Event()
        self._status_lock = asyncio.Lock()
        self._last_check_at: float | None = None
        self._last_check_outcome: str | None = None
        self._last_check_deferred_reason: str | None = None
        self._last_check_error: str | None = None
        self._pending_version: str | None = None
        self._pending_digest: str | None = None
        self._pending_download_path: str | None = None
        self._pending_installer_path: str | None = None
        # The UpdateManager that owns the temp dir between download
        # and apply -- kept alive across loop iterations only when
        # we're holding a staged artifact for the user.
        self._pending_manager: UpdateManager | None = None
        self._currently_checking = False
        # NSIS leaves a phase marker at each install boundary. On
        # startup we read the marker, journal it as the previous
        # attempt's terminal state, then clear it. ``complete`` means
        # a clean upgrade -- anything else is the furthest point an
        # interrupted install reached.
        self._nsis_phase_from_last_install: str | None = None

    async def start(self) -> None:
        await super().start()
        self._consume_nsis_marker()

    def _consume_nsis_marker(self) -> None:
        phase = read_and_clear_marker()
        if not phase:
            return
        self._nsis_phase_from_last_install = phase
        entry = UpdateJournalEntry(
            id=new_update_id(),
            started_at=time.time(),
            from_version="(unknown)",
            source_url="(nsis)",
            auto_update=self.config.app.auto_update,
            nsis_phase=phase,
            user_action="post_install_marker",
        )
        if phase == "complete":
            entry.finalize("installed")
            logger.info("Previous upgrade completed cleanly (NSIS phase=%s)", phase)
        else:
            entry.finalize(
                "failed",
                error=f"NSIS install reached phase '{phase}' but did not complete",
            )
            logger.warning(
                "Previous upgrade did not complete (NSIS phase=%s) -- "
                "the install may need to be re-run manually",
                phase,
            )
        append_entry(self.storage_path, entry)

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

        # ---------- check ----------
        try:
            has_update, version_info = await manager.check_for_updates()
        except (NetworkError, UpdateCheckError) as exc:
            adapter.warning("check failed: %s", exc)
            entry.finalize("failed", error=str(exc))
            manager.cleanup()
            return
        entry.stages_completed.append("check")

        if not has_update or version_info is None:
            adapter.info("no update available")
            entry.finalize("skipped")
            manager.cleanup()
            return

        remote_version = version_info.get("version")
        entry.to_version = remote_version
        adapter.info("update available: %s -> %s", self.current_version, remote_version)

        # ---------- quiescence ----------
        is_idle, busy_reason = await self.quiescence_check()
        entry.stages_completed.append("quiescence")
        if not is_idle:
            adapter.info("deferred -- pipeline busy: %s", busy_reason)
            entry.finalize("deferred", deferred_reason=busy_reason)
            # Surface the pending version so the dashboard shows
            # "0.3.7 detected, deferred until idle" between ticks.
            self._pending_version = remote_version
            manager.cleanup()
            return

        # ---------- download ----------
        adapter.info("downloading installer")
        downloaded = await manager.download_update(version_info)
        if not downloaded:
            adapter.error("download failed")
            entry.finalize("failed", error="download_failed")
            manager.cleanup()
            return
        entry.stages_completed.append("download")
        entry.download_bytes = self._sum_downloaded_bytes(version_info)

        # ---------- digest verify ----------
        installer_path = manager.installer_path()
        digest = self._extract_digest(version_info)
        entry.digest_expected = digest
        try:
            entry.digest_actual = manager.compute_sha256(installer_path)
        except OSError as exc:
            adapter.error("digest read failed: %s", exc)
            entry.finalize("failed", error=f"digest_read: {exc}")
            manager.cleanup()
            return
        if not manager.verify_digest(installer_path, digest):
            adapter.error(
                "digest verify failed: expected=%s actual=%s",
                digest,
                entry.digest_actual,
            )
            entry.finalize("failed", error="digest_mismatch")
            manager.cleanup()
            return
        entry.stages_completed.append("verify")

        # Past this point the artifact is safe to launch. Stash it
        # for either the auto-install branch or a later /apply.
        self._pending_version = remote_version
        self._pending_digest = digest
        self._pending_download_path = manager.temp_dir
        self._pending_installer_path = installer_path
        self._pending_manager = manager  # keeps the temp dir alive

        if not self.config.app.auto_update:
            adapter.info(
                "staged %s -- auto_update=false, waiting for /api/update/apply",
                remote_version,
            )
            entry.finalize("pending_user_approval")
            return

        # ---------- spawn ----------
        self._spawn_and_shutdown(adapter, entry)

    def _spawn_and_shutdown(
        self, adapter: UpdateLoggerAdapter, entry: UpdateJournalEntry
    ) -> None:
        """Drive the staged artifact: spawn NSIS detached, then
        signal a clean shutdown. Shared by the auto_update=true
        branch above and the user-driven ``apply_pending`` path."""
        assert self._pending_manager is not None
        try:
            pid = self._pending_manager.spawn_installer(self._pending_installer_path)
        except Exception as exc:
            adapter.error("installer spawn failed: %s", exc)
            entry.finalize("failed", error=f"spawn_failed: {exc}")
            return
        entry.stages_completed.append("spawn")
        adapter.info(
            "installer spawned pid=%d -- service shutting down for NSIS handoff",
            pid,
        )
        entry.finalize("spawned")
        # Best-effort: trigger clean exit. NSIS would SCM-stop us
        # anyway, but exiting first means no half-cancelled tasks.
        if self.shutdown_callback is not None:
            try:
                self.shutdown_callback()
            except Exception as exc:
                logger.error("shutdown_callback raised: %s", exc, exc_info=True)

    async def apply_pending(self) -> tuple[bool, str]:
        """User-driven ``POST /api/update/apply`` entry point.

        Returns ``(ok, message)``. The HTTP layer maps this to a
        202/4xx response. Validates that we actually have a staged
        artifact and a fresh quiescence check before driving the
        spawn -- otherwise a tray click that races with new pipeline
        work would interrupt it.
        """
        if not self._pending_version or not self._pending_installer_path:
            return False, "No update pending."
        if self._pending_manager is None:
            return False, "Pending artifact missing manager state."

        is_idle, busy_reason = await self.quiescence_check()
        if not is_idle:
            return False, f"Pipeline busy: {busy_reason}"

        update_id = new_update_id()
        adapter = UpdateLoggerAdapter(logger, {"update_id": update_id})
        source_url, _ = resolve_api_url(
            self.config.app.github_repo, self.config.app.update_api_url
        )
        entry = UpdateJournalEntry(
            id=update_id,
            started_at=time.time(),
            from_version=self.current_version,
            source_url=source_url,
            auto_update=False,
            to_version=self._pending_version,
            stages_completed=["check", "quiescence", "download", "verify"],
            digest_expected=self._pending_digest,
            user_action="clicked_install",
        )
        try:
            self._spawn_and_shutdown(adapter, entry)
            return entry.outcome == "spawned", entry.outcome
        finally:
            append_entry(self.storage_path, entry)

    @staticmethod
    def _extract_digest(version_info: VersionInfo) -> str | None:
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
    def _sum_downloaded_bytes(version_info: VersionInfo) -> int | None:
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
            except TimeoutError:
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
            nsis_phase_from_last_install=self._nsis_phase_from_last_install,
        )

    def get_queue_size(self) -> int:
        """Polling processors have no queue, but the orchestrator's
        ``get_queue_sizes()`` method calls this. Return 0 so the
        update-check pseudo-processor never registers as 'busy' in
        its own quiescence query."""
        return 0
