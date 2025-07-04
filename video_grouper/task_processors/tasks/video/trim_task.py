"""
Trim task for trimming combined videos based on match information.
"""

import os
import logging
from typing import List, Dict, Any, Optional, Callable, Awaitable
from dataclasses import dataclass
from datetime import datetime

from .base_ffmpeg_task import BaseFfmpegTask
from video_grouper.models import DirectoryState

logger = logging.getLogger(__name__)


@dataclass(unsafe_hash=True)
class TrimTask(BaseFfmpegTask):
    """
    Task for trimming a combined video based on match information.

    Uses FFmpeg to trim the video to the specified start and end times.
    """

    group_dir: str
    start_time: str  # Format: "HH:MM:SS"
    end_time: str  # Format: "HH:MM:SS"

    @property
    def task_type(self) -> str:
        """Return the specific task type identifier."""
        return "trim"

    def get_command(self) -> List[str]:
        """
        Return the FFmpeg command to trim the video.

        Returns:
            FFmpeg command as list of strings
        """
        combined_path = os.path.join(self.group_dir, "combined.mp4")

        # Create the subdirectory and get the trimmed file path
        trimmed_path = self._get_trimmed_file_path()

        cmd = [
            "ffmpeg",
            "-y",  # Overwrite output file
            "-i",
            combined_path,  # Input file
            "-ss",
            self.start_time,  # Start time
            "-to",
            self.end_time,  # End time
            "-c",
            "copy",  # Copy streams without re-encoding
            trimmed_path,
        ]

        return cmd

    def _get_trimmed_file_path(self) -> str:
        """
        Create the subdirectory structure and return the path for the trimmed file.

        Returns:
            Path where the trimmed file should be created
        """
        from video_grouper.models import MatchInfo

        # Get match info to extract team names and location
        match_info, _ = MatchInfo.get_or_create(self.group_dir)

        # Extract date from directory name (format: YYYY.MM.DD-HH.MM.SS)
        dir_name = os.path.basename(self.group_dir)
        try:
            date_part = dir_name.split("-")[0]  # YYYY.MM.DD
            date_obj = datetime.strptime(date_part, "%Y.%m.%d")
            formatted_date = date_obj.strftime("%m-%d-%Y")
        except Exception:
            # Fallback to current date if parsing fails
            formatted_date = datetime.now().strftime("%m-%d-%Y")

        # Get sanitized team names and location
        my_team, opponent_team, location = match_info.get_sanitized_names()

        # Create subdirectory name: "YYYY.MM.DD - My Team vs Opponent Team (location)"
        subdir_name = f"{date_part} - {my_team} vs {opponent_team} ({location})"
        subdir_path = os.path.join(self.group_dir, subdir_name)

        # Create the subdirectory if it doesn't exist
        os.makedirs(subdir_path, exist_ok=True)

        # Create filename: "myteam-opponent-location-MM-DD-YYYY-raw.mp4"
        # Convert team names to lowercase and replace spaces with hyphens
        my_team_slug = my_team.lower().replace(" ", "-")
        opponent_team_slug = opponent_team.lower().replace(" ", "-")
        location_slug = location.lower().replace(" ", "-")

        filename = f"{my_team_slug}-{opponent_team_slug}-{location_slug}-{formatted_date}-raw.mp4"

        return os.path.join(subdir_path, filename)

    async def execute(
        self, queue_task: Optional[Callable[[Any], Awaitable[None]]] = None
    ) -> bool:
        """
        Execute the trim task and handle post-actions.

        Args:
            queue_task: Function to queue additional tasks

        Returns:
            True if command succeeded, False otherwise
        """
        # Execute the FFmpeg command
        success = await super().execute(queue_task)

        if success:
            await self._handle_post_trim_actions()
        else:
            await self._handle_task_failure()

        return success

    async def _handle_post_trim_actions(self) -> None:
        """Handle post-trim actions like updating status."""
        try:
            dir_state = DirectoryState(self.group_dir)
            logger.info(f"TRIM: Successfully trimmed video in {self.group_dir}")
            await dir_state.update_group_status("trimmed")

        except Exception as e:
            logger.error(f"TRIM: Error in post-trim actions for {self}: {e}")

    async def _handle_task_failure(self) -> None:
        """Handle task failure by updating directory state."""
        try:
            dir_state = DirectoryState(self.group_dir)
            await dir_state.update_group_status(
                "trim_failed", error_message="Task execution failed"
            )
        except Exception as e:
            logger.error(f"TRIM: Error handling task failure for {self}: {e}")

    def get_item_path(self) -> str:
        """Return the group directory path."""
        return self.group_dir

    def serialize(self) -> Dict[str, Any]:
        """
        Serialize the task for state persistence.

        Returns:
            Dictionary containing task data
        """
        return {
            "task_type": self.task_type,
            "group_dir": self.group_dir,
            "start_time": self.start_time,
            "end_time": self.end_time,
        }

    def get_output_path(self) -> str:
        """
        Get the expected output path for the trimmed file.

        Returns:
            Path where the trimmed file will be created
        """
        return self._get_trimmed_file_path()

    def __str__(self) -> str:
        """String representation of the task."""
        return f"TrimTask({os.path.basename(self.group_dir)}, {self.start_time}-{self.end_time})"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TrimTask":
        """
        Create a TrimTask from serialized data.

        Args:
            data: Dictionary containing task data

        Returns:
            TrimTask instance
        """
        # Handle both 'group_dir' and 'item_path' for backward compatibility
        group_dir = data.get("group_dir") or data.get("item_path")
        return cls(
            group_dir=group_dir,
            start_time=data["start_time"],
            end_time=data["end_time"],
        )

    @classmethod
    def from_match_info(cls, group_dir: str, match_info) -> "TrimTask":
        """
        Create a TrimTask from match information.

        Args:
            group_dir: Directory containing the combined video
            match_info: MatchInfo object with timing information

        Returns:
            TrimTask instance
        """
        # Get start time from match_info (start_time_offset)
        start_time = match_info.get_start_offset()  # This returns HH:MM:SS format

        # Calculate end time from start_time_offset + total_duration
        total_duration_seconds = match_info.get_total_duration_seconds()
        start_offset_seconds = cls._time_to_seconds(start_time)
        end_time_seconds = start_offset_seconds + total_duration_seconds
        end_time = cls._seconds_to_time(end_time_seconds)

        return cls(group_dir=group_dir, start_time=start_time, end_time=end_time)

    @staticmethod
    def _time_to_seconds(time_str: str) -> int:
        """Convert HH:MM:SS time string to seconds."""
        try:
            parts = time_str.split(":")
            if len(parts) == 3:
                hours, minutes, seconds = map(int, parts)
                return hours * 3600 + minutes * 60 + seconds
            elif len(parts) == 2:
                minutes, seconds = map(int, parts)
                return minutes * 60 + seconds
            else:
                return 0
        except (ValueError, IndexError):
            return 0

    @staticmethod
    def _seconds_to_time(seconds: int) -> str:
        """Convert seconds to HH:MM:SS time string."""
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
