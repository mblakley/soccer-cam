"""
Game start time NTFY task.

This task handles asking users about when a game starts in a video.
"""

import os
import logging
from typing import Dict, Any

from .base_ntfy_task import BaseNtfyTask, NtfyTaskResult
from video_grouper.utils.config import Config

logger = logging.getLogger(__name__)


class GameStartTask(BaseNtfyTask):
    """
    Task for determining game start time.

    This task asks users about when a game starts in a video, starting from
    the beginning and moving forward in 5-minute intervals until the user
    identifies the start time.
    """

    def __init__(
        self,
        group_dir: str,
        config: Config,
        combined_video_path: str,
        time_offset: str = "00:00",
        time_seconds: int = 0,
    ):
        """
        Initialize the game start task.

        Args:
            group_dir: Directory associated with the task
            config: Configuration object
            combined_video_path: Path to the combined video file
            time_offset: Current time offset to ask about (e.g., "05:00")
            time_seconds: Current time in seconds
        """
        super().__init__(
            group_dir,
            config,
            {
                "combined_video_path": combined_video_path,
                "time_offset": time_offset,
                "time_seconds": time_seconds,
            },
        )
        self.combined_video_path = combined_video_path
        self.time_offset = time_offset
        self.time_seconds = time_seconds

    def get_task_type(self) -> str:
        """Get the task type identifier."""
        from .enums import NtfyInputType

        return NtfyInputType.GAME_START_TIME.value

    async def create_question(self) -> Dict[str, Any]:
        """
        Create the question data for asking about game start time.

        Returns:
            Dictionary containing question data
        """
        # Generate screenshot for the current time
        image_path = None
        if os.path.exists(self.combined_video_path):
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
                "body": f"Yes, game started at {self.time_offset}",
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
            "message": f"Does the game start at {self.time_offset}? (Screenshot at {self.time_offset})",
            "title": f"Game Start Time - {self.time_offset}",
            "tags": ["game_start", "screenshot", self.time_offset],
            "priority": 4,
            "image_path": image_path,
            "actions": actions,
            "metadata": {
                "time_offset": self.time_offset,
                "time_seconds": self.time_seconds,
                "combined_video_path": self.combined_video_path,
            },
        }

    async def process_response(self, response: str) -> NtfyTaskResult:
        """
        Process a response to the game start time question.

        Args:
            response: The user's response

        Returns:
            NtfyTaskResult indicating success and whether to continue
        """
        response_lower = response.lower()

        if "yes" in response_lower or "game started" in response_lower:
            logger.info(f"Game started at {self.time_offset} for {self.group_dir}")

            # Calculate the actual start time by subtracting 4 minutes from the current time offset
            # This is because if you say "Yes" at 05:00, the game likely started after 00:00 (when we said "No" at 00:00)
            actual_start_seconds = (
                self.time_seconds - 240
            )  # Subtract 4 minutes (240 seconds)
            if actual_start_seconds < 0:
                actual_start_seconds = 0  # Don't go below 0

            actual_start_offset = (
                f"{actual_start_seconds // 60:02d}:{actual_start_seconds % 60:02d}"
            )

            logger.info(
                f"Calculated actual start time: {actual_start_offset} (from {self.time_offset} minus 4 minutes)"
            )

            # Update match info with the calculated start time
            match_info = self.get_match_info()
            match_info.start_time_offset = actual_start_offset
            match_info.save()

            return NtfyTaskResult(
                success=True,
                should_continue=False,
                message=f"Game start time set to {actual_start_offset}",
                metadata={"start_time_offset": actual_start_offset},
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
                f"Game has not started yet at {self.time_offset} for {self.group_dir}"
            )

            # Calculate next time offset (+5 minutes)
            next_time_seconds = self.time_seconds + 300  # Add 5 minutes (300 seconds)
            next_time_offset = (
                f"{next_time_seconds // 60:02d}:{next_time_seconds % 60:02d}"
            )

            logger.info(
                f"Will ask about next time: {next_time_offset} (+5 minutes from {self.time_offset})"
            )

            # Update our metadata for the next iteration
            self.time_offset = next_time_offset
            self.time_seconds = next_time_seconds
            self.metadata["time_offset"] = next_time_offset
            self.metadata["time_seconds"] = next_time_seconds
            self.metadata["previous_time"] = self.time_offset

            return NtfyTaskResult(
                success=True,
                should_continue=True,
                message=f"Will ask about {next_time_offset}",
                metadata={
                    "next_time_offset": next_time_offset,
                    "next_time_seconds": next_time_seconds,
                },
            )

        else:
            logger.info(f"Unhandled response for game start time: {response}")
            return NtfyTaskResult(
                success=False,
                should_continue=True,
                message=f"Unhandled response: {response}",
            )

    @classmethod
    def create_next_task(
        cls,
        current_task: "GameStartTask",
        next_time_offset: str,
        next_time_seconds: int,
    ) -> "GameStartTask":
        """
        Create the next task in the sequence.

        Args:
            current_task: The current task
            next_time_offset: The next time offset to ask about
            next_time_seconds: The next time in seconds

        Returns:
            New GameStartTask for the next time
        """
        return cls(
            group_dir=current_task.group_dir,
            config=current_task.config,
            combined_video_path=current_task.combined_video_path,
            time_offset=next_time_offset,
            time_seconds=next_time_seconds,
        )
