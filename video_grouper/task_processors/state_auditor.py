import logging
import os

from ..models import DirectoryState, RecordingFile
from ..models.match_info import MatchInfo
from ..task_processors.tasks.video import CombineTask, TrimTask
from ..utils.config import Config
from ..utils.ffmpeg_utils import get_video_duration
from ..utils.paths import (
    get_combined_video_path,
    get_match_info_path,
    get_state_file_path,
)
from .base_polling_processor import PollingProcessor
from .download_processor import DownloadProcessor
from .services import (
    CleanupService,
    MatchInfoService,
    NtfyService,
)
from .services.mock_services import create_playmetrics_service, create_teamsnap_service
from .video_processor import VideoProcessor

logger = logging.getLogger(__name__)

# Group statuses that are past the download phase. Once a group reaches any of
# these, StateAuditor must never re-queue its files for download. A segment the
# camera finalized under a new name (e.g. an in-progress "..._000000_..."
# listing the camera later re-lists as "..._<endtime>_...") lingers in the
# group's state at a non-terminal file status and 404s on every fetch.
# Re-queuing it on each audit spins forever AND keeps the download queue
# non-idle, which starves the camera poller's reconcile pass — exactly what
# blocked the 2026-06-15 game from being re-ingested (a stale 2026-06-23
# "_000000_" zombie in a not_a_game group monopolized the queue). Mirrors
# DownloadProcessor.process_item's skip_statuses, plus not_a_game.
_DOWNLOAD_DONE_STATUSES = frozenset(
    {
        "combined",
        "trimmed",
        "ball_tracking_complete",
        "pipeline_complete",
        "complete",
        "not_a_game",
    }
)


class StateAuditor(PollingProcessor):
    """
    Recovery scanner for the pipeline.

    Scans the shared_data directory for state.json files and re-queues
    any work that needs to start or restart (pending downloads,
    combined dirs awaiting match info, ball_tracking_complete dirs
    awaiting upload, etc.). Originally a one-shot startup scan, but
    that left a gap: when a user manually populates a match_info.ini
    after startup (e.g. tournament-day fix), the auditor wouldn't see
    it until the next service restart.

    Now runs on a poll interval (default 60s). Safe to re-poll because
    every downstream ``add_work`` call dedupes on the task's item_key,
    so a still-in-progress task isn't re-enqueued.

    Temp-file cleanup stays BOOT-ONLY (first successful pass): the
    ``*.partial*`` / ``*.tmp`` suffixes it reaps are exactly what active
    downloads, combines, trims and state saves stage to, so re-running
    the sweep on every poll would race live writers.
    """

    def __init__(
        self,
        storage_path: str,
        config: Config,
        download_processors=None,
        video_processor: VideoProcessor = None,
        poll_interval: int = 60,
        ntfy_processor=None,
        # Legacy single-processor param
        download_processor: DownloadProcessor = None,
    ):
        super().__init__(storage_path, config, poll_interval)
        # Accept either a dict of download_processors or a single one
        if isinstance(download_processors, dict):
            self._download_processors = download_processors
        elif download_processors is not None:
            self._download_processors = {"default": download_processors}
        elif download_processor is not None:
            self._download_processors = {"default": download_processor}
        else:
            self._download_processors = {}
        self.video_processor = video_processor
        self.ntfy_processor = ntfy_processor

        # Initialize API services using mock service factory functions
        self.teamsnap_service = create_teamsnap_service(config.teamsnap)
        self.playmetrics_service = create_playmetrics_service(config.playmetrics)
        # Create NTFY service for the match info service and general use
        self.ntfy_service = NtfyService(config.ntfy, storage_path)
        self.match_info_service = MatchInfoService(
            self.teamsnap_service, self.playmetrics_service, self.ntfy_service
        )

        # Initialize cleanup service
        self.cleanup_service = CleanupService(storage_path)

        # Temp-file cleanup is BOOT-ONLY. Active operations stage to the
        # same suffixes the cleanup reaps (downloads -> *.partial*,
        # combine/trim -> *.tmp, state saves -> state.json.tmp), so a
        # recurring sweep would race live writers: on POSIX an unlink of
        # an open file makes the final os.replace fail; on Windows it
        # spams sharing-violation warnings every poll. At boot nothing
        # is in flight, so the orphan sweep is safe exactly once.
        self._startup_cleanup_done = False

    @property
    def download_processor(self):
        """Backward compat: return the first download processor."""
        if self._download_processors:
            return next(iter(self._download_processors.values()))
        return None

    async def discover_work(self) -> None:
        """
        Audit all directories in storage_path and queue appropriate tasks.
        """
        logger.info("STATE_AUDITOR: Starting audit of storage directory")

        try:
            # Get all directories in storage path
            run_cleanup = not self._startup_cleanup_done
            items = os.listdir(self.storage_path)
            for item in items:
                group_dir = os.path.join(self.storage_path, item)
                if os.path.isdir(group_dir) and not item.startswith("."):
                    if run_cleanup:
                        self._cleanup_temp_files(group_dir)
                    await self._audit_directory(group_dir)
            # Only after a full pass — a failed listdir retries cleanup
            # on the next poll instead of silently never sweeping.
            self._startup_cleanup_done = True

        except Exception as e:
            logger.error(f"STATE_AUDITOR: Error during directory audit: {e}")

    @staticmethod
    def _cleanup_temp_files(group_dir: str) -> None:
        """Remove staging files left by crashed downloads / mux operations.

        Catches both the current convention (``*.partial``,
        ``*.partial.video``, ``*.partial.audio``) and legacy suffixes
        (``*.tmp``, ``*.http.tmp``, ``*.raw.tmp``, ``*.raw.tmp.audio``)
        so an upgrade with leftover staging files from the prior naming
        scheme cleans up cleanly on first boot.
        """
        try:
            for fname in os.listdir(group_dir):
                if not (
                    ".partial" in fname or fname.endswith(".tmp") or ".tmp." in fname
                ):
                    continue
                tmp_path = os.path.join(group_dir, fname)
                try:
                    os.remove(tmp_path)
                    logger.info(
                        f"STATE_AUDITOR: Cleaned up orphaned temp file: {fname}"
                    )
                except OSError as exc:
                    logger.warning(
                        f"STATE_AUDITOR: Could not remove temp file {fname}: {exc}"
                    )
        except OSError:
            pass

    async def _audit_directory(self, group_dir: str) -> None:
        """Audit a single directory and queue appropriate tasks."""
        state_file_path = get_state_file_path(group_dir, self.storage_path)
        logger.debug(
            f"STATE_AUDITOR: Resolved state_file_path for group_dir={group_dir}, storage_path={self.storage_path}: {state_file_path}"
        )
        if not os.path.exists(state_file_path):
            return

        try:
            dir_state = DirectoryState(group_dir, self.storage_path)

            # One-time on-scan migration: when the config-driven pipeline is
            # active, rename a legacy ``ball_tracking_complete`` group to
            # ``pipeline_complete`` so the on-disk status matches the new path.
            # Upload recovery + pipeline discovery both still read BOTH values,
            # so this is safe and idempotent. Gated on the pipeline being active
            # so a legacy ball-tracking install never has its status rewritten
            # out from under the legacy discovery's ball_tracking_complete read.
            pipeline_cfg = getattr(self.config, "pipeline", None)
            if (
                pipeline_cfg is not None
                and pipeline_cfg.is_active()
                and dir_state.status == "ball_tracking_complete"
            ):
                logger.info(
                    "STATE_AUDITOR: migrating %s status "
                    "ball_tracking_complete -> pipeline_complete",
                    group_dir,
                )
                await dir_state.update_group_status("pipeline_complete")

            # Audit individual files. Only (re-)queue downloads while the group
            # is still in the download phase; a group past download
            # (combined/.../not_a_game) must never have its files re-fetched
            # (see _DOWNLOAD_DONE_STATUSES for why — the 2026-06-15 starvation).
            if dir_state.status not in _DOWNLOAD_DONE_STATUSES:
                for file_obj in dir_state.files.values():
                    if file_obj.skip:
                        continue

                    # Queue download tasks for pending/failed/interrupted
                    # downloads. "downloading" status means the app crashed
                    # mid-download, so treat it as a failure and re-queue.
                    if file_obj.status in [
                        "pending",
                        "download_failed",
                        "downloading",
                    ]:
                        if self.download_processor:
                            recording_file = RecordingFile(
                                start_time=file_obj.start_time,
                                end_time=file_obj.end_time,
                                file_path=file_obj.file_path,
                                metadata=file_obj.metadata,
                                status=file_obj.status,
                                skip=file_obj.skip,
                            )
                            await self.download_processor.add_work(recording_file)

            # Check if ready for combining
            if dir_state.is_ready_for_combining():
                combined_path = get_combined_video_path(group_dir, self.storage_path)
                logger.debug(
                    f"STATE_AUDITOR: Resolved combined_path for group_dir={group_dir}, storage_path={self.storage_path}: {combined_path}"
                )
                if not os.path.exists(combined_path):
                    if self.video_processor:
                        await self.video_processor.add_work(CombineTask(group_dir))

            # Check for trimming (combined status with match info processing)
            if dir_state.status == "combined":
                combined_path = get_combined_video_path(group_dir, self.storage_path)
                if os.path.exists(combined_path):
                    logger.info(f"STATE_AUDITOR: Found combined directory: {group_dir}")

                    # Skip NTFY flow for short clips that aren't real games
                    min_duration = self.config.recording.min_duration
                    try:
                        duration = await get_video_duration(combined_path)
                    except Exception as exc:
                        logger.warning(
                            f"STATE_AUDITOR: Could not get duration for {group_dir}: {exc}. "
                            f"Assuming long video, continuing."
                        )
                        duration = None
                    if duration is not None and duration < min_duration:
                        logger.info(
                            f"STATE_AUDITOR: Combined video too short "
                            f"({duration:.0f}s < {min_duration}s) for {group_dir}, "
                            f"marking as not_a_game"
                        )
                        await dir_state.update_group_status("not_a_game")
                        return

                    # Check if we're waiting for user input via NTFY queue processor
                    is_waiting = False
                    waiting_task_type = None
                    if self.ntfy_processor:
                        is_waiting = (
                            self.ntfy_processor.ntfy_service.is_waiting_for_input(
                                group_dir
                            )
                        )
                        if is_waiting:
                            # Get the task type that's waiting
                            pending_tasks = (
                                self.ntfy_processor.ntfy_service.get_pending_tasks()
                            )
                            if group_dir in pending_tasks:
                                waiting_task_type = pending_tasks[group_dir].get(
                                    "task_type"
                                )

                    # Check if match info is populated
                    match_info_path = get_match_info_path(group_dir, self.storage_path)
                    if os.path.exists(match_info_path):
                        match_info = MatchInfo.from_file(match_info_path)
                        if match_info and match_info.is_populated():
                            logger.info(
                                f"STATE_AUDITOR: Match info already populated for {group_dir}, queuing trim task"
                            )
                            # Queue trim task
                            if self.video_processor:
                                trim_end = getattr(
                                    self.config.processing, "trim_end_enabled", False
                                )
                                await self.video_processor.add_work(
                                    TrimTask.from_match_info(
                                        group_dir, match_info, trim_end_enabled=trim_end
                                    )
                                )
                        else:
                            # Check if we have team info but are missing timing info
                            has_team_info = (
                                match_info
                                and match_info.my_team_name.strip()
                                and match_info.opponent_team_name.strip()
                                and match_info.location.strip()
                            )
                            missing_timing = (
                                not match_info
                                or not match_info.start_time_offset.strip()
                            )

                            if has_team_info and missing_timing:
                                logger.info(
                                    f"STATE_AUDITOR: Team info populated but timing info missing for {group_dir}, queuing NTFY request"
                                )
                                # Queue NTFY request for timing information
                                if self.ntfy_processor:
                                    await self.ntfy_processor.request_match_info_for_directory(
                                        group_dir, combined_path, force=False
                                    )
                            else:
                                # Check if we're waiting for team info specifically
                                if is_waiting and waiting_task_type == "team_info":
                                    logger.info(
                                        f"STATE_AUDITOR: Waiting for team info input for {group_dir}"
                                    )
                                else:
                                    logger.info(
                                        f"STATE_AUDITOR: Match info exists but not populated for {group_dir}, processing via service"
                                    )
                                    await self._trigger_match_info_flow(
                                        group_dir, combined_path
                                    )
                    else:
                        # Check if we're waiting for team info specifically
                        if is_waiting and waiting_task_type == "team_info":
                            logger.info(
                                f"STATE_AUDITOR: Waiting for team info input for {group_dir}"
                            )
                        else:
                            logger.info(
                                f"STATE_AUDITOR: No match_info.ini found for {group_dir}, processing via service"
                            )
                            await self._trigger_match_info_flow(
                                group_dir, combined_path
                            )

            # Handle trimmed status - if no post-trim processing stage owns the
            # group (the config-driven pipeline), skip straight to upload. When
            # the pipeline is active, leave the group at ``trimmed`` for the
            # pipeline discovery to pick up.
            if (
                dir_state.status == "trimmed"
                and not self.config.post_trim_processing_active()
            ):
                logger.info(
                    f"STATE_AUDITOR: No post-trim processing active, transitioning {group_dir} to upload"
                )
                await dir_state.update_group_status("ball_tracking_complete")
                await self._queue_upload(group_dir)

            # Check for videos to upload. Accept BOTH completion statuses:
            # ``ball_tracking_complete`` (legacy in-flight) and
            # ``pipeline_complete`` (the config-driven path). Cross-app handoff:
            # when a tray-runtime pipeline step (autocam) runs in the tray, the
            # tray's processor sets this status but carries no upload_processor
            # reference; the service's StateAuditor picks it up here and queues
            # the upload via the service's UploadProcessor. For in-process
            # service runs the processor already queued the upload directly, so
            # this branch is a harmless no-op (the upload queue dedupes).
            elif dir_state.status in ("ball_tracking_complete", "pipeline_complete"):
                await self._queue_upload(group_dir)

            # Check for not_a_game status (user confirmed there was no match)
            elif dir_state.status == "not_a_game":
                logger.debug(
                    f"STATE_AUDITOR: Found not_a_game status for {group_dir}, skipping further processing"
                )

            # Handle cleanup tasks — boot-only, same reasoning as
            # _cleanup_temp_files (CleanupService reaps *.tmp, which
            # combine/trim and state saves actively stage to).
            if not self._startup_cleanup_done:
                await self._handle_cleanup(group_dir)

        except Exception as e:
            logger.error(f"STATE_AUDITOR: Error auditing directory {group_dir}: {e}")

    async def _trigger_match_info_flow(
        self, group_dir: str, combined_path: str
    ) -> None:
        """Trigger the match info + NTFY flow for a combined directory.

        Routes through ntfy_processor when available so that the NTFY
        response listener is the same instance that sent the notification.
        Falls back to match_info_service for direct processing.
        """
        # Try API-based population first
        if self.match_info_service:
            await self.match_info_service.populate_match_info_from_apis(group_dir)

        # If the API call filled in everything, skip NTFY entirely and
        # queue trim directly. Without this re-check, a successful TeamSnap
        # /PlayMetrics lookup writes match_info.ini but the auditor's
        # earlier "is_populated" check (run BEFORE this call) saw it
        # empty, so the trim only fires on the NEXT service boot. That
        # made every fresh install need a restart to actually process
        # games — and made the user think the system was stuck.
        match_info_path = get_match_info_path(group_dir, self.storage_path)
        if os.path.exists(match_info_path):
            match_info = MatchInfo.from_file(match_info_path)
            if match_info and match_info.is_populated() and self.video_processor:
                logger.info(
                    f"STATE_AUDITOR: API populated full match_info for "
                    f"{group_dir}, queuing trim directly (skipping NTFY)"
                )
                trim_end = getattr(self.config.processing, "trim_end_enabled", False)
                await self.video_processor.add_work(
                    TrimTask.from_match_info(
                        group_dir, match_info, trim_end_enabled=trim_end
                    )
                )
                if self.ntfy_processor:
                    self.ntfy_processor.ntfy_service.mark_as_processed(group_dir)
                return

        # Route NTFY through ntfy_processor so responses are handled correctly
        if self.ntfy_processor:
            await self.ntfy_processor.request_match_info_for_directory(
                group_dir, combined_path, force=False
            )
        elif self.match_info_service and self.match_info_service.ntfy_service.enabled:
            await self.match_info_service.ntfy_service.process_combined_directory(
                group_dir, combined_path
            )

    async def _queue_upload(self, group_dir: str) -> None:
        """Queue a YouTube upload for a directory if YouTube is enabled."""
        if self.config.youtube.enabled and self.video_processor.upload_processor:
            from video_grouper.task_processors.tasks.upload import YoutubeUploadTask

            relative_group_dir = os.path.relpath(group_dir, self.storage_path)
            youtube_task = YoutubeUploadTask(group_dir=relative_group_dir)
            await self.video_processor.upload_processor.add_work(youtube_task)
            logger.info(f"STATE_AUDITOR: Queued YouTube upload for {group_dir}")
        else:
            logger.warning(
                "STATE_AUDITOR: skipping YouTube upload for %s "
                "(youtube.enabled=%s, upload_processor=%s)",
                group_dir,
                self.config.youtube.enabled,
                self.video_processor.upload_processor is not None,
            )

    async def _handle_cleanup(self, group_dir: str) -> None:
        """Handle cleanup tasks for a directory."""
        try:
            # Let the cleanup service handle any cleanup tasks
            await self.cleanup_service.process_directory(group_dir)
        except Exception as e:
            logger.error(f"STATE_AUDITOR: Error during cleanup for {group_dir}: {e}")

    async def stop(self) -> None:
        """Cancel the polling loop and clean up services."""
        await super().stop()
        await self.match_info_service.shutdown()
