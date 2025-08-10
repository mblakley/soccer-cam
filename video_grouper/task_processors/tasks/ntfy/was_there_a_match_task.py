"""
Was there a match NTFY task.

This task asks users if there was actually a match during the recording period.
"""

import logging
import os
import json
from typing import Dict, Any, Optional
from datetime import datetime

from .base_ntfy_task import BaseNtfyTask, NtfyTaskResult
from video_grouper.utils.config import Config
from video_grouper.task_processors.services.ntfy_service import NtfyService
from video_grouper.models import DirectoryState

logger = logging.getLogger(__name__)


class WasThereAMatchTask(BaseNtfyTask):
    """
    Task for asking if there was a match during the recording period.

    This task asks users to confirm if there was actually a match
    during the time period covered by the video files.
    """

    def __init__(
        self,
        group_dir: str,
        config: Config,
        ntfy_service: NtfyService,
        combined_video_path: str,
    ):
        """
        Initialize the was there a match task.

        Args:
            group_dir: Directory associated with the task
            config: Configuration object
            ntfy_service: NTFY service for sending notifications
            combined_video_path: Path to the combined video file
        """
        metadata = {
            "combined_video_path": combined_video_path,
            "config": {
                "ntfy": {
                    "topic": config.ntfy.topic,
                    "server_url": config.ntfy.server_url,
                    "enabled": config.ntfy.enabled,
                }
            },
        }
        super().__init__(group_dir, config, ntfy_service, metadata)
        self.combined_video_path = combined_video_path

    def get_task_type(self) -> str:
        """Get the task type identifier."""
        from .enums import NtfyInputType

        return NtfyInputType.WAS_THERE_A_MATCH.value

    def _get_recording_timespan(self) -> Optional[tuple[datetime, datetime]]:
        """
        Get the recording timespan from directory state.

        Returns:
            Tuple of (start_time, end_time) or None if not available
        """
        try:
            dir_state = DirectoryState(self.group_dir)

            # Get files from the files attribute
            files = list(dir_state.files.values())
            logger.info(f"Found {len(files)} files in directory state")

            if not files:
                logger.warning("No files found in directory state")
                return None

            # Sort files by start time to get first and last
            files.sort(key=lambda f: f.start_time)
            first_file = files[0]
            last_file = files[-1]

            logger.info(
                f"First file: {first_file.file_path} at {first_file.start_time}"
            )
            logger.info(
                f"Last file: {last_file.file_path} at {last_file.end_time or last_file.start_time}"
            )

            recording_start = first_file.start_time
            recording_end = last_file.end_time or last_file.start_time

            logger.info(f"Recording timespan: {recording_start} to {recording_end}")
            return recording_start, recording_end

        except Exception as e:
            logger.error(f"Error getting recording timespan for {self.group_dir}: {e}")
            return None

    async def create_question(self) -> Dict[str, Any]:
        """
        Create the question data for asking if there was a match.

        Returns:
            Dictionary containing question data
        """
        # Get the recording timespan
        timespan = self._get_recording_timespan()

        if not timespan:
            # If we can't get the timespan, just ask a generic question
            message = "Was there a match during the recording period? Please respond with 'Yes' or 'No'."
        else:
            recording_start, recording_end = timespan
            # Format the times for display
            start_str = recording_start.strftime("%Y-%m-%d %H:%M")
            end_str = recording_end.strftime("%Y-%m-%d %H:%M")

            message = f"Was there a match during the recording period ({start_str} to {end_str})? Please respond with 'Yes' or 'No'."

        # Get the directory information
        directory_info = ""
        if self.group_dir:
            directory_name = os.path.basename(self.group_dir)
            directory_info = f" in directory: {directory_name}"

        # Get NTFY config from metadata
        ntfy_config = self.metadata.get("config", {}).get("ntfy", {})
        topic = ntfy_config.get("topic", "")

        # Create action buttons that send responses back to the NTFY topic
        actions = [
            {
                "action": "http",
                "label": "Yes",
                "url": f"https://ntfy.sh/{topic}",
                "method": "POST",
                "headers": {"Content-Type": "text/plain"},
                "body": "Yes, there was a match",
                "clear": True,
            },
            {
                "action": "http",
                "label": "No",
                "url": f"https://ntfy.sh/{topic}",
                "method": "POST",
                "headers": {"Content-Type": "text/plain"},
                "body": "No, there was no match",
                "clear": True,
            },
        ]

        return {
            "message": f"Match confirmation{directory_info}: {message}",
            "title": "Was There a Match?",
            "tags": ["question", "info"],
            "priority": 4,
            "image_path": None,  # No screenshot for this question
            "actions": actions,
            "metadata": {
                "combined_video_path": self.combined_video_path,
                "recording_timespan": timespan,
            },
        }

    async def process_response(self, response: str) -> NtfyTaskResult:
        """
        Process a response to the was there a match question.

        Args:
            response: The user's response

        Returns:
            NtfyTaskResult indicating success and whether to continue
        """
        response_lower = response.lower().strip()

        # Handle responses from action buttons
        if "yes, there was a match" in response_lower or response_lower in [
            "yes",
            "y",
            "true",
            "1",
        ]:
            logger.info(f"User confirmed there was a match for {self.group_dir}")
            return NtfyTaskResult(
                success=True,
                should_continue=True,  # Continue to ask for team info
                message="Match confirmed, proceeding to team info collection",
            )
        elif "no, there was no match" in response_lower or response_lower in [
            "no",
            "n",
            "false",
            "0",
        ]:
            logger.info(
                f"User confirmed there was NO match for {self.group_dir}, marking as not_a_game"
            )

            # Update the state to mark this as not a game
            await self._mark_as_not_a_game()

            return NtfyTaskResult(
                success=True,
                should_continue=False,  # Don't continue to team info
                message="No match confirmed, marked as not_a_game",
            )
        else:
            logger.warning(
                f"Unclear response '{response}' for {self.group_dir}, treating as 'No'"
            )

            # For unclear responses, default to "No" and mark as not a game
            await self._mark_as_not_a_game()

            return NtfyTaskResult(
                success=True,
                should_continue=False,
                message="Unclear response, defaulting to not_a_game",
            )

    async def _mark_as_not_a_game(self) -> None:
        """Mark the directory as not containing a game."""
        try:
            state_file = os.path.join(self.group_dir, "state.json")

            if os.path.exists(state_file):
                with open(state_file, "r") as f:
                    state_data = json.load(f)

                state_data["status"] = "not_a_game"
                state_data["marked_not_a_game_at"] = datetime.now().isoformat()

                with open(state_file, "w") as f:
                    json.dump(state_data, f, indent=4)

                logger.info(f"Successfully marked {self.group_dir} as not_a_game")
            else:
                logger.warning(
                    f"state.json not found for {self.group_dir}, cannot mark as not_a_game"
                )

        except Exception as e:
            logger.error(f"Error marking {self.group_dir} as not_a_game: {e}")

    @classmethod
    def deserialize(cls, data: Dict[str, object]) -> "WasThereAMatchTask":
        """
        Deserialize a WasThereAMatchTask from its serialized data.

        Args:
            data: Serialized task data

        Returns:
            WasThereAMatchTask instance
        """

        # Extract the data
        group_dir = data["group_dir"]
        metadata = data.get("metadata", {})

        # Create a minimal config from metadata if available
        config = None
        if "config" in metadata:
            from video_grouper.utils.config import NtfyConfig

            ntfy_config_data = metadata["config"].get("ntfy", {})
            ntfy_config = NtfyConfig(**ntfy_config_data)

            class MinimalConfig:
                def __init__(self, ntfy_config):
                    self.ntfy = ntfy_config

            config = MinimalConfig(ntfy_config)

        # Create a dummy NTFY service (will be replaced when task is used)
        class DummyNtfyService:
            def __init__(self):
                pass

        ntfy_service = DummyNtfyService()

        # Create the task
        task = cls(
            group_dir=group_dir,
            config=config,
            ntfy_service=ntfy_service,
            combined_video_path=metadata.get("combined_video_path", ""),
        )

        # Restore additional state if needed
        if "created_at" in data:
            task.created_at = datetime.fromisoformat(data["created_at"])

        return task
