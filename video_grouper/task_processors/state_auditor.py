import os
import logging

from .polling_processor_base import PollingProcessor
from video_grouper.models import DirectoryState
from video_grouper.models import RecordingFile, MatchInfo
from .tasks.video import ConvertTask, CombineTask, TrimTask
from .tasks.upload import YoutubeUploadTask
from .services import (
    TeamSnapService,
    PlayMetricsService,
    NtfyService,
    MatchInfoService,
    CleanupService,
)
from ..utils.config import Config

logger = logging.getLogger(__name__)


class StateAuditor(PollingProcessor):
    """
    Task processor for auditing external state changes.
    Scans the shared_data directory for state.json files and queues appropriate tasks.
    """

    def __init__(self, storage_path: str, config: Config, poll_interval: int = 60):
        super().__init__(storage_path, config, poll_interval)
        # References to other processors to queue work
        self.download_processor = None
        self.video_processor = None
        self.upload_processor = None
        self.ntfy_queue_processor = None

        # Initialize API services
        self.teamsnap_service = TeamSnapService(config.teamsnap, config.app)

        playmetrics_configs = []
        if config.playmetrics.enabled:
            playmetrics_configs.append(config.playmetrics)
        playmetrics_configs.extend(config.playmetrics_teams)

        self.playmetrics_service = PlayMetricsService(playmetrics_configs, config.app)
        # Create NTFY service for the match info service and general use
        self.ntfy_service = NtfyService(config.ntfy, storage_path)
        self.match_info_service = MatchInfoService(
            self.teamsnap_service, self.playmetrics_service, self.ntfy_service
        )

        # Initialize cleanup service
        self.cleanup_service = CleanupService(storage_path)

    def set_processors(
        self,
        download_processor,
        video_processor,
        upload_processor,
        ntfy_queue_processor=None,
    ):
        """Set references to other processors to queue work."""
        self.download_processor = download_processor
        self.video_processor = video_processor
        self.upload_processor = upload_processor
        self.ntfy_queue_processor = ntfy_queue_processor

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
        state_file_path = os.path.join(group_dir, "state.json")
        if not os.path.exists(state_file_path):
            return

        try:
            dir_state = DirectoryState(group_dir)

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

                # Auto-fix: if mp4 exists mark as converted
                elif file_obj.status in ["downloaded", "conversion_failed"]:
                    mp4_path = file_obj.file_path.replace(".dav", ".mp4")
                    if os.path.exists(mp4_path):
                        logger.info(
                            f"STATE_AUDITOR: Detected existing MP4 for {os.path.basename(file_obj.file_path)} – marking as converted"
                        )
                        await dir_state.update_file_state(
                            file_obj.file_path, status="converted"
                        )
                    elif os.path.exists(file_obj.file_path):
                        if self.video_processor:
                            await self.video_processor.add_work(
                                ConvertTask(file_obj.file_path)
                            )

            # Check if ready for combining
            if dir_state.is_ready_for_combining():
                combined_path = os.path.join(group_dir, "combined.mp4")
                if not os.path.exists(combined_path):
                    if self.video_processor:
                        await self.video_processor.add_work(CombineTask(group_dir))

            # Check for trimming (combined status with match info processing)
            if dir_state.status == "combined":
                combined_path = os.path.join(group_dir, "combined.mp4")
                if os.path.exists(combined_path):
                    logger.info(f"STATE_AUDITOR: Found combined directory: {group_dir}")

                    # Check if we're waiting for user input via NTFY queue processor
                    is_waiting = False
                    if self.ntfy_queue_processor:
                        is_waiting = (
                            self.ntfy_queue_processor.ntfy_service.is_waiting_for_input(
                                group_dir
                            )
                        )

                    if is_waiting:
                        logger.info(
                            f"STATE_AUDITOR: Waiting for user input for {group_dir}"
                        )
                    else:
                        logger.info(
                            f"STATE_AUDITOR: No pending input for {group_dir}, checking match info"
                        )

                        # Check if match info is populated
                        match_info_path = os.path.join(group_dir, "match_info.ini")
                        if os.path.exists(match_info_path):
                            match_info = MatchInfo.from_file(match_info_path)
                            if match_info and match_info.is_populated():
                                logger.info(
                                    f"STATE_AUDITOR: Match info already populated for {group_dir}, queuing trim task"
                                )
                                # Queue trim task
                                if self.video_processor:
                                    await self.video_processor.add_work(
                                        TrimTask.from_match_info(group_dir, match_info)
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
                                    if self.ntfy_queue_processor:
                                        await self.ntfy_queue_processor.request_match_info_for_directory(
                                            group_dir, combined_path, force=False
                                        )
                                else:
                                    logger.info(
                                        f"STATE_AUDITOR: Match info exists but not populated for {group_dir}, processing via service"
                                    )
                                    # Use the match info service to process this directory
                                    await self.match_info_service.process_combined_directory(
                                        group_dir, combined_path
                                    )
                        else:
                            logger.info(
                                f"STATE_AUDITOR: No match_info.ini found for {group_dir}, processing via service"
                            )
                            # Use the match info service to process this directory
                            await self.match_info_service.process_combined_directory(
                                group_dir, combined_path
                            )

            # Check for videos to upload (autocam_complete status)
            if dir_state.status == "autocam_complete":
                # Check if video upload is enabled
                if self.config.youtube.enabled:
                    if self.upload_processor:
                        await self.upload_processor.add_work(
                            YoutubeUploadTask(
                                group_dir, self.config.youtube, self.ntfy_service
                            )
                        )

            # Handle cleanup tasks
            await self._handle_cleanup(group_dir, dir_state)

        except Exception as e:
            logger.error(f"STATE_AUDITOR: Error auditing directory {group_dir}: {e}")

    async def _handle_cleanup(self, group_dir: str, dir_state: DirectoryState) -> None:
        """Handle cleanup tasks for a directory."""
        try:
            # Clean up DAV files if appropriate
            if self.cleanup_service.should_cleanup_dav_files(group_dir):
                self.cleanup_service.cleanup_dav_files(group_dir)

            # Clean up temporary files
            self.cleanup_service.cleanup_temporary_files(group_dir)

        except Exception as e:
            logger.error(f"STATE_AUDITOR: Error during cleanup for {group_dir}: {e}")

    async def stop(self) -> None:
        """Stop the state auditor and clean up services."""
        await super().stop()
        await self.match_info_service.shutdown()
