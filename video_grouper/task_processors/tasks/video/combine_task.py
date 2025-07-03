"""
Combine task for combining multiple MP4 files into a single video.
"""

import os
import logging
from typing import List, Dict, Any, Optional, Callable, Awaitable
from dataclasses import dataclass
import aiofiles

from .base_ffmpeg_task import BaseFfmpegTask
from video_grouper.models import DirectoryState

logger = logging.getLogger(__name__)


@dataclass(unsafe_hash=True)
class CombineTask(BaseFfmpegTask):
    """
    Task for combining multiple MP4 files in a directory into a single combined video.

    Uses FFmpeg concat demuxer to combine files without re-encoding.
    """

    group_dir: str

    @property
    def task_type(self) -> str:
        """Return the specific task type identifier."""
        return "combine"

    def get_command(self) -> List[str]:
        """
        Return the FFmpeg command to combine the MP4 files.

        Note: This method returns the basic command structure, but the actual
        execution requires creating a filelist.txt file first. The execute()
        method should be overridden to handle this.

        Returns:
            FFmpeg command as list of strings
        """
        combined_path = os.path.join(self.group_dir, "combined.mp4")
        file_list_path = os.path.join(self.group_dir, "filelist.txt")

        return [
            "ffmpeg",
            "-y",  # Overwrite output file
            "-f",
            "concat",  # Use concat demuxer
            "-safe",
            "0",  # Allow unsafe file names
            "-i",
            file_list_path,  # Input file list
            "-c",
            "copy",  # Copy streams without re-encoding
            combined_path,
        ]

    def get_item_path(self) -> str:
        """Return the group directory path."""
        return self.group_dir

    def serialize(self) -> Dict[str, Any]:
        """
        Serialize the task for state persistence.

        Returns:
            Dictionary containing task data
        """
        return {"task_type": self.task_type, "group_dir": self.group_dir}

    def get_output_path(self) -> str:
        """
        Get the expected output path for the combined file.

        Returns:
            Path where the combined.mp4 file will be created
        """
        return os.path.join(self.group_dir, "combined.mp4")

    def get_file_list_path(self) -> str:
        """
        Get the path for the temporary filelist.txt file.

        Returns:
            Path for the filelist.txt file
        """
        return os.path.join(self.group_dir, "filelist.txt")

    def get_mp4_files(self) -> List[str]:
        """
        Get the list of MP4 files to combine from the group directory.

        Returns:
            Sorted list of MP4 file paths
        """
        mp4_files = []
        try:
            for filename in sorted(os.listdir(self.group_dir)):
                if filename.endswith(".mp4") and filename != "combined.mp4":
                    mp4_files.append(os.path.join(self.group_dir, filename))
        except FileNotFoundError:
            pass
        return mp4_files

    async def execute(
        self, queue_task: Optional[Callable[[Any], Awaitable[None]]] = None
    ) -> bool:
        """
        Execute the combine task with proper file list creation and handle post-actions.

        Args:
            queue_task: Function to queue additional tasks

        Returns:
            True if command succeeded, False otherwise
        """
        mp4_files = self.get_mp4_files()
        if not mp4_files:
            await self._handle_task_failure()
            return False

        file_list_path = self.get_file_list_path()

        try:
            # Create the file list for ffmpeg concat
            async with aiofiles.open(file_list_path, "w") as f:
                for mp4_file in mp4_files:
                    # Use relative paths for the concat file
                    await f.write(f"file '{os.path.basename(mp4_file)}'\n")

            # Execute the ffmpeg command
            success = await super().execute(queue_task)

            if success:
                await self._handle_post_combine_actions(queue_task)
            else:
                await self._handle_task_failure()

            return success

        except Exception as e:
            logger.error(f"COMBINE: Error during combine task execution: {e}")
            await self._handle_task_failure()
            return False
        finally:
            # Clean up the temporary file list
            try:
                if os.path.exists(file_list_path):
                    os.remove(file_list_path)
            except Exception:
                pass

    async def _handle_post_combine_actions(
        self, queue_task: Optional[Callable[[Any], Awaitable[None]]] = None
    ) -> None:
        """Handle post-combine actions like updating status and checking for trim readiness."""
        try:
            dir_state = DirectoryState(self.group_dir)

            logger.info(f"COMBINE: Successfully combined videos in {self.group_dir}")
            await dir_state.update_group_status("combined")

            # The match info processing will be handled by the StateAuditor
            # which will detect the "combined" status and process match info appropriately

        except Exception as e:
            logger.error(f"COMBINE: Error in post-combine actions for {self}: {e}")

    async def _handle_task_failure(self) -> None:
        """Handle task failure by updating directory state."""
        try:
            dir_state = DirectoryState(self.group_dir)
            await dir_state.update_group_status(
                "combine_failed", error_message="Task execution failed"
            )
        except Exception as e:
            logger.error(f"COMBINE: Error handling task failure for {self}: {e}")

    def __str__(self) -> str:
        """String representation of the task."""
        return f"CombineTask({os.path.basename(self.group_dir)})"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CombineTask":
        """
        Create a CombineTask from serialized data.

        Args:
            data: Dictionary containing task data

        Returns:
            CombineTask instance
        """
        return cls(group_dir=data["group_dir"])
