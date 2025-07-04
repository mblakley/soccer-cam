"""
Game end time NTFY task.

This task handles asking users about when a game ends in a video.
"""

import os
import logging
from typing import Dict, Any, Optional

from .base_ntfy_task import BaseNtfyTask, NtfyTaskResult
from video_grouper.utils.config import Config

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
        combined_video_path: str,
        start_time_offset: str,
        time_offset: Optional[str] = None,
        time_seconds: Optional[int] = None,
    ):
        """
        Initialize the game end task.

        Args:
            group_dir: Directory associated with the task
            config: Configuration object
            combined_video_path: Path to the combined video file
            start_time_offset: The game start time offset
            time_offset: Current time offset to ask about (if None, will be calculated)
            time_seconds: Current time in seconds (if None, will be calculated)
        """
        super().__init__(
            group_dir,
            config,
            {
                "combined_video_path": combined_video_path,
                "start_time_offset": start_time_offset,
            },
        )
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

    async def create_question(self) -> Dict[str, Any]:
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

        # Create action buttons
        actions = [
            {
                "action": "http",
                "label": "Yes",
                "url": f"https://ntfy.sh/{self.config.ntfy.topic}",
                "method": "POST",
                "headers": {"Content-Type": "text/plain"},
                "body": f"Yes, game ended at {self.time_offset}",
                "clear": True,
            },
            {
                "action": "http",
                "label": "No",
                "url": f"https://ntfy.sh/{self.config.ntfy.topic}",
                "method": "POST",
                "headers": {"Content-Type": "text/plain"},
                "body": f"No, not yet at {self.time_offset}",
                "clear": True,
            },
            {
                "action": "http",
                "label": "Not a Game",
                "url": f"https://ntfy.sh/{self.config.ntfy.topic}",
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

            # Update match info with end time
            match_info = self.get_match_info()
            match_info.end_time_offset = self.time_offset
            match_info.save()

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
