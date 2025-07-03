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

        # Initialize API services
        teamsnap_configs = []
        if config.teamsnap.enabled:
            teamsnap_configs.append(config.teamsnap)
        teamsnap_configs.extend(config.teamsnap_teams)
        self.teamsnap_service = TeamSnapService(teamsnap_configs)

        playmetrics_configs = []
        if config.playmetrics.enabled:
            playmetrics_configs.append(config.playmetrics)
        playmetrics_configs.extend(config.playmetrics_teams)

        self.playmetrics_service = PlayMetricsService(playmetrics_configs)
        self.ntfy_service = NtfyService(config.ntfy, storage_path)
        self.match_info_service = MatchInfoService(
            self.teamsnap_service, self.playmetrics_service, self.ntfy_service
        )

        # Initialize cleanup service
        self.cleanup_service = CleanupService(storage_path)

    def set_processors(self, download_processor, video_processor, upload_processor):
        """Set references to other processors to queue work."""
        self.download_processor = download_processor
        self.video_processor = video_processor
        self.upload_processor = upload_processor

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

            # Handle NTFY responses for pending inputs
            await self._handle_ntfy_responses()

        except Exception as e:
            logger.error(f"STATE_AUDITOR: Error during directory audit: {e}")

    async def _handle_ntfy_responses(self) -> None:
        """Handle responses for pending NTFY requests."""
        pending_inputs = self.ntfy_service.get_pending_inputs()
        if not pending_inputs:
            return

        for group_dir, info in pending_inputs.items():
            if info.get("input_type") == "playlist_name":
                # This is a simplified check; in a real scenario, you'd
                # likely have a more robust way to check for new messages.
                # For now, we assume the user has responded if the state is pending.

                # In a real implementation, you would listen to ntfy.sh here
                # and get the response. For this example, we'll simulate a response.

                # Once a response is received, update the state and config
                playlist_name = "User Provided Playlist"  # Simulated response

                # In a real implementation, you would get the name from the ntfy response
                self._update_playlist_info(
                    group_dir, info["metadata"]["team_name"], playlist_name
                )

    def _update_playlist_info(
        self, group_dir: str, team_name: str, playlist_name: str
    ) -> None:
        """Update state and config with a new playlist name."""
        dir_state = DirectoryState(group_dir)
        dir_state.set_youtube_playlist_name(playlist_name)

        # Update the main config as well
        self.config.set("YOUTUBE_PLAYLIST_MAPPING", team_name, playlist_name)
        config_path = os.path.join(self.storage_path, "config.ini")
        try:
            with open(config_path, "w") as configfile:
                self.config.write(configfile)
            logger.info(
                f"Updated config with new playlist mapping for team {team_name}"
            )
        except Exception as e:
            logger.error(f"Failed to update config with new playlist mapping: {e}")

        self.ntfy_service.clear_pending_input(group_dir)

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

                # Queue convert tasks for downloaded files
                elif file_obj.status == "downloaded":
                    if self.video_processor:
                        await self.video_processor.add_work(
                            ConvertTask(file_obj.file_path)
                        )

                # Queue convert tasks for failed conversions
                elif file_obj.status == "conversion_failed":
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
                    # Check if we're waiting for user input
                    if self.match_info_service.is_waiting_for_user_input(group_dir):
                        logger.debug(
                            f"STATE_AUDITOR: Waiting for user input for {group_dir}"
                        )
                    else:
                        # Check if match info is populated
                        match_info_path = os.path.join(group_dir, "match_info.ini")
                        if os.path.exists(match_info_path):
                            match_info = MatchInfo.from_file(match_info_path)
                            if match_info and match_info.is_populated():
                                # Queue trim task
                                if self.video_processor:
                                    await self.video_processor.add_work(
                                        TrimTask.from_match_info(group_dir, match_info)
                                    )
                            else:
                                # Try to process match info
                                await (
                                    self.match_info_service.process_combined_directory(
                                        group_dir, combined_path
                                    )
                                )
                        else:
                            # Try to process match info
                            await self.match_info_service.process_combined_directory(
                                group_dir, combined_path
                            )

            # Check for videos to upload (autocam_complete status)
            if dir_state.status == "autocam_complete":
                # Check if video upload is enabled
                if self.config.youtube.enabled:
                    if self.upload_processor:
                        await self.upload_processor.add_work(
                            YoutubeUploadTask(group_dir)
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
