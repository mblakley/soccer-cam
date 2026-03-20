import os
import logging

from .base_polling_processor import PollingProcessor
from .download_processor import DownloadProcessor
from .video_processor import VideoProcessor
from ..models import DirectoryState, RecordingFile
from ..models.match_info import MatchInfo
from ..task_processors.tasks.video import CombineTask, TrimTask
from ..utils.paths import (
    get_state_file_path,
    get_combined_video_path,
    get_match_info_path,
)
from .services import (
    NtfyService,
    MatchInfoService,
    CleanupService,
)
from .services.mock_services import create_teamsnap_service, create_playmetrics_service
from ..utils.config import Config

logger = logging.getLogger(__name__)


class StateAuditor(PollingProcessor):
    """
    Startup recovery scanner for the pipeline.

    On app start, scans the shared_data directory for state.json files and
    re-queues any interrupted work (pending downloads, combined dirs awaiting
    match info, etc.). Does NOT run as a continuous poller during normal
    operation — event-driven transitions handle ongoing work.
    """

    def __init__(
        self,
        storage_path: str,
        config: Config,
        download_processor: DownloadProcessor,
        video_processor: VideoProcessor,
        poll_interval: int = 60,
        ntfy_processor=None,
    ):
        super().__init__(storage_path, config, poll_interval)
        self.download_processor = download_processor
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

    async def start(self) -> None:
        """Run a one-time startup scan to recover interrupted work.

        Unlike the base PollingProcessor.start(), this does NOT create a
        persistent polling loop. It runs discover_work() once and returns.
        """
        logger.info("STATE_AUDITOR: Running startup recovery scan")
        await self.discover_work()
        logger.info("STATE_AUDITOR: Startup recovery scan complete")

    async def discover_work(self) -> None:
        """
        Audit all directories in storage_path and queue appropriate tasks.
        This is the main work of the state auditor.
        """
        logger.info("STATE_AUDITOR: Starting audit of storage directory")

        try:
            # Get all directories in storage path
            items = os.listdir(self.storage_path)
            for item in items:
                group_dir = os.path.join(self.storage_path, item)
                if os.path.isdir(group_dir) and not item.startswith("."):
                    await self._audit_directory(group_dir)

        except Exception as e:
            logger.error(f"STATE_AUDITOR: Error during directory audit: {e}")

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

            # Audit individual files
            for file_obj in dir_state.files.values():
                if file_obj.skip:
                    continue

                # Queue download tasks for pending/failed downloads
                if file_obj.status in ["pending", "download_failed"]:
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

            # Handle trimmed status - if autocam is disabled, skip to upload
            if dir_state.status == "trimmed" and not self.config.autocam.enabled:
                logger.info(
                    f"STATE_AUDITOR: Autocam disabled, transitioning {group_dir} to upload"
                )
                await dir_state.update_group_status("autocam_complete")
                await self._queue_upload(group_dir)

            # Check for videos to upload (autocam_complete status)
            elif dir_state.status == "autocam_complete":
                if not self.config.autocam.enabled:
                    # Headless mode: queue upload directly
                    await self._queue_upload(group_dir)
                else:
                    logger.debug(
                        f"STATE_AUDITOR: Found autocam_complete status for {group_dir}, uploads handled by tray agent"
                    )

            # Check for not_a_game status (user confirmed there was no match)
            elif dir_state.status == "not_a_game":
                logger.debug(
                    f"STATE_AUDITOR: Found not_a_game status for {group_dir}, skipping further processing"
                )

            # Handle cleanup tasks
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

    async def _handle_cleanup(self, group_dir: str) -> None:
        """Handle cleanup tasks for a directory."""
        try:
            # Let the cleanup service handle any cleanup tasks
            await self.cleanup_service.process_directory(group_dir)
        except Exception as e:
            logger.error(f"STATE_AUDITOR: Error during cleanup for {group_dir}: {e}")

    async def stop(self) -> None:
        """Clean up services. No polling loop to stop."""
        await self.match_info_service.shutdown()
