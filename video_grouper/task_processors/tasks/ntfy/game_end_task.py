"""
Game end time NTFY task.

This task handles asking users about when a game ends in a video.
"""

import logging
import os
from typing import Any

from video_grouper.task_processors.services.ntfy_service import NtfyService
from video_grouper.utils.config import Config

from .base_ntfy_task import BaseNtfyTask, NtfyTaskResult

logger = logging.getLogger(__name__)


class GameEndTask(BaseNtfyTask):
    """
    Task for determining game end time.

    This task asks users about when a game ends in a video, typically
    near the end of the video.
    """

    def __init__(
        self,
        group_dir: str,
        config: Config,
        ntfy_service: NtfyService,
        combined_video_path: str,
        start_time_offset: str,
        time_offset: str | None = None,
        time_seconds: int | None = None,
    ):
        """
        Initialize the game end task.

        Args:
            group_dir: Directory associated with the task
            config: Configuration object
            ntfy_service: NTFY service for sending notifications
            combined_video_path: Path to the combined video file
            start_time_offset: The game start time offset
            time_offset: Current time offset to ask about (if None, will be calculated)
            time_seconds: Current time in seconds (if None, will be calculated)
        """
        metadata = {
            "combined_video_path": combined_video_path,
            "start_time_offset": start_time_offset,
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
        self.start_time_offset = start_time_offset

        # If time not provided, calculate it based on video duration
        if time_offset is None or time_seconds is None:
            # This will be set when create_question is called
            self.time_offset = None
            self.time_seconds = None
        else:
            self.time_offset = time_offset
            self.time_seconds = time_seconds
            self.metadata["time_offset"] = time_offset
            self.metadata["time_seconds"] = time_seconds

    def get_task_type(self) -> str:
        """Get the task type identifier."""
        from .enums import NtfyInputType

        return NtfyInputType.GAME_END_TIME.value

    async def create_question(self) -> dict[str, Any]:
        """
        Create the question data for asking about game end time.

        Returns:
            Dictionary containing question data
        """
        # If we don't have a time offset yet, calculate it
        if self.time_offset is None or self.time_seconds is None:
            duration = await self.get_video_duration(self.combined_video_path)
            if duration:
                # Ask about 1 minute before the end
                self.time_seconds = duration - 60
                self.time_offset = (
                    f"{self.time_seconds // 60:02d}:{self.time_seconds % 60:02d}"
                )
                self.metadata["time_offset"] = self.time_offset
                self.metadata["time_seconds"] = self.time_seconds
            else:
                logger.error(
                    f"Could not get video duration for {self.combined_video_path}"
                )
                return {}

        # Generate screenshot for the current time
        image_path = None
        if os.path.exists(self.combined_video_path) and self.time_seconds is not None:
            image_path = await self.generate_screenshot(
                self.combined_video_path, self.time_seconds
            )

        # Get NTFY config from metadata
        ntfy_config = self.metadata.get("config", {}).get("ntfy", {})
        topic = ntfy_config.get("topic", "")

        # Create action buttons
        actions = [
            {
                "action": "http",
                "label": "Yes",
                "url": f"https://ntfy.sh/{topic}",
                "method": "POST",
                "headers": {"Content-Type": "text/plain"},
                "body": f"Yes, game ended at {self.time_offset}",
                "clear": True,
            },
            {
                "action": "http",
                "label": "No",
                "url": f"https://ntfy.sh/{topic}",
                "method": "POST",
                "headers": {"Content-Type": "text/plain"},
                "body": f"No, not yet at {self.time_offset}",
                "clear": True,
            },
            {
                "action": "http",
                "label": "Not a Game",
                "url": f"https://ntfy.sh/{topic}",
                "method": "POST",
                "headers": {"Content-Type": "text/plain"},
                "body": f"Not a game at {self.time_offset}",
                "clear": True,
            },
        ]

        return {
            "message": f"Does the game end near the end of the video? (Screenshot at {self.time_offset})",
            "title": f"Game End Time - {self.time_offset}",
            "tags": ["game_end", "screenshot", self.time_offset],
            "priority": 4,
            "image_path": image_path,
            "actions": actions,
            "metadata": {
                "time_offset": self.time_offset,
                "time_seconds": self.time_seconds,
                "start_time_offset": self.start_time_offset,
                "combined_video_path": self.combined_video_path,
            },
        }

    async def process_response(self, response: str) -> NtfyTaskResult:
        """
        Process a response to the game end time question.

        Args:
            response: The user's response

        Returns:
            NtfyTaskResult indicating success and whether to continue
        """
        response_lower = response.lower()

        if "yes" in response_lower or "game ended" in response_lower:
            logger.info(f"Game ended at {self.time_offset} for {self.group_dir}")

            # Compute total_duration = confirmed_end - start_time_offset and
            # persist via update_game_times so TrimTask uses the correct value.
            # (Setting match_info.end_time_offset on the dataclass and calling
            # save() is wrong — end_time_offset is not a declared field so it
            # never reaches total_duration in the ini file.)
            try:
                from video_grouper.models import MatchInfo

                start_parts = self.start_time_offset.split(":")
                if len(start_parts) == 3:
                    start_secs = (
                        int(start_parts[0]) * 3600
                        + int(start_parts[1]) * 60
                        + int(start_parts[2])
                    )
                elif len(start_parts) == 2:
                    start_secs = int(start_parts[0]) * 60 + int(start_parts[1])
                else:
                    start_secs = 0
                end_secs = int(self.time_seconds or 0)
                duration_secs = max(0, end_secs - start_secs)
                h = duration_secs // 3600
                m = (duration_secs % 3600) // 60
                s = duration_secs % 60
                total_duration = f"{h:02d}:{m:02d}:{s:02d}"
                MatchInfo.update_game_times(
                    self.group_dir,
                    total_duration=total_duration,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "game_end: could not persist total_duration for %s: %s",
                    self.group_dir,
                    exc,
                )

            return NtfyTaskResult(
                success=True,
                should_continue=False,
                message=f"Game end time set to {self.time_offset}",
                metadata={"end_time_offset": self.time_offset},
            )

        elif "not a game" in response_lower:
            logger.info(f"Video is not a game for {self.group_dir}")
            return NtfyTaskResult(
                success=True,
                should_continue=False,
                message="Video marked as not a game",
            )

        elif "no" in response_lower or "not yet" in response_lower:
            logger.info(
                f"Game has not ended yet at {self.time_offset} for {self.group_dir}"
            )

            # For end time, we could ask about an earlier time, but for now
            # we'll just mark it as not found
            return NtfyTaskResult(
                success=True, should_continue=False, message="Game end time not found"
            )

        else:
            logger.info(f"Unhandled response for game end time: {response}")
            return NtfyTaskResult(
                success=False,
                should_continue=False,
                message=f"Unhandled response: {response}",
            )
