"""
Autocam task for processing videos through Once Autocam GUI.
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Dict

import av

from video_grouper.utils.ffmpeg_utils import av_open_read

from ..base_task import BaseTask
from ...queue_type import QueueType
from video_grouper.utils.config import AutocamConfig
from video_grouper.tray.autocam_automation import run_autocam_on_file

logger = logging.getLogger(__name__)


class AutocamTask(BaseTask):
    """
    Task for processing a video through Once Autocam GUI.
    """

    def __init__(
        self,
        group_dir: Path,
        input_path: str,
        output_path: str,
        autocam_config: AutocamConfig,
    ):
        """
        Initialize the Autocam task.

        Args:
            group_dir: Directory containing the video group
            input_path: Path to the input video file (-raw.mp4)
            output_path: Path for the output video file (.mp4)
            autocam_config: Autocam configuration
        """
        self.group_dir = group_dir
        self.input_path = input_path
        self.output_path = output_path
        self.autocam_config = autocam_config

    @classmethod
    def queue_type(cls) -> QueueType:
        return QueueType.AUTOCAM

    @property
    def task_type(self) -> str:
        """Return the specific task type identifier."""
        return "autocam_process"

    def get_item_path(self) -> str:
        """Return the path of the item being processed."""
        return str(self.group_dir)

    def serialize(self) -> Dict[str, object]:
        """Serialize the task for state persistence."""
        return {
            "task_type": self.task_type,
            "group_dir": str(self.group_dir),
            "input_path": self.input_path,
            "output_path": self.output_path,
            "autocam_config": {
                "executable": self.autocam_config.executable,
                "enabled": self.autocam_config.enabled,
            },
        }

    def _validate_video_file(self, path: str) -> bool:
        """
        Use PyAV to verify that the file is a valid video with non-zero duration.

        Args:
            path: Path to the video file

        Returns:
            True if the file is a valid video, False otherwise
        """
        if not os.path.isfile(path):
            logger.error(f"AUTOCAM: Input file does not exist: {path}")
            return False

        file_size = os.path.getsize(path)
        if file_size < 10_000:  # Less than 10KB is clearly invalid
            logger.error(
                f"AUTOCAM: Input file is too small to be a valid video "
                f"({file_size} bytes): {path}"
            )
            return False

        try:
            with av_open_read(path) as container:
                duration = None
                if container.duration is not None:
                    duration = container.duration / av.time_base
                else:
                    for stream in container.streams.video:
                        if stream.duration is not None and stream.time_base is not None:
                            duration = float(stream.duration * stream.time_base)
                            break

                if duration is None:
                    logger.error(f"AUTOCAM: Could not determine duration for: {path}")
                    return False

                if duration <= 0:
                    logger.error(
                        f"AUTOCAM: Input file has zero duration ({duration}s): {path}"
                    )
                    return False

                logger.info(
                    f"AUTOCAM: Input file validated OK - duration={duration:.1f}s, "
                    f"size={file_size / (1024 * 1024):.1f}MB: {path}"
                )
                return True

        except (ValueError, av.error.FFmpegError) as e:
            logger.error(f"AUTOCAM: Error validating input file: {e}")
            return False

    async def execute(self) -> bool:
        """
        Execute the autocam task.

        Autocam 3.x writes directly to the output container we specify (we
        request .mp4 via the Save As dialog), so the post-remux step is no
        longer needed.

        Returns:
            True if task succeeded, False otherwise
        """
        try:
            logger.info(f"AUTOCAM: Processing group {self.group_dir.name}")

            # Validate the input file before running Autocam
            if not self._validate_video_file(self.input_path):
                logger.error(
                    f"AUTOCAM: Input file is invalid, skipping group {self.group_dir.name}"
                )
                return False

            # Run the autocam automation in a thread to avoid blocking
            # the asyncio/Qt event loop (autocam polls with time.sleep)
            loop = asyncio.get_event_loop()
            success = await loop.run_in_executor(
                None,
                run_autocam_on_file,
                self.autocam_config,
                self.input_path,
                self.output_path,
            )

            if not success:
                logger.error(f"AUTOCAM: Failed to process group {self.group_dir.name}")
                return False

            logger.info(f"AUTOCAM: Successfully processed group {self.group_dir.name}")
            return True

        except Exception as e:
            logger.error(f"AUTOCAM: Error processing group {self.group_dir.name}: {e}")
            return False

    def __str__(self):
        return f"AutocamTask(group_dir={self.group_dir}, input_path={self.input_path}, output_path={self.output_path})"

    def __eq__(self, other):
        if not isinstance(other, AutocamTask):
            return False
        return (
            self.group_dir == other.group_dir
            and self.input_path == other.input_path
            and self.output_path == other.output_path
            and self.autocam_config.executable == other.autocam_config.executable
            and self.autocam_config.enabled == other.autocam_config.enabled
        )

    def __hash__(self):
        return hash(
            (
                self.group_dir,
                self.input_path,
                self.output_path,
                getattr(self.autocam_config, "executable", None),
                getattr(self.autocam_config, "enabled", None),
            )
        )

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "AutocamTask":
        """
        Create an AutocamTask from serialized data.

        Args:
            data: Dictionary containing task data

        Returns:
            AutocamTask instance
        """
        from video_grouper.utils.config import AutocamConfig

        autocam_config = AutocamConfig(
            executable=data["autocam_config"]["executable"],
            enabled=data["autocam_config"]["enabled"],
        )

        return cls(
            group_dir=Path(data["group_dir"]),
            input_path=data["input_path"],
            output_path=data["output_path"],
            autocam_config=autocam_config,
        )

    @classmethod
    def deserialize(cls, data: Dict[str, object]) -> "AutocamTask":
        """
        Deserialize an AutocamTask from its serialized data.

        Args:
            data: Dictionary containing serialized task data

        Returns:
            Deserialized AutocamTask instance
        """
        return cls.from_dict(data)
