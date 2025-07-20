"""
Autocam task for processing videos through Once Autocam GUI.
"""

import logging
from pathlib import Path
from typing import Dict

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

    @property
    def queue_type(self) -> QueueType:
        """Return the queue type for routing this task."""
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

    async def execute(self) -> bool:
        """
        Execute the autocam task.

        Returns:
            True if task succeeded, False otherwise
        """
        try:
            logger.info(f"AUTOCAM: Processing group {self.group_dir.name}")

            # Run the autocam automation
            success = run_autocam_on_file(
                self.autocam_config, self.input_path, self.output_path
            )

            if success:
                logger.info(
                    f"AUTOCAM: Successfully processed group {self.group_dir.name}"
                )
            else:
                logger.error(f"AUTOCAM: Failed to process group {self.group_dir.name}")

            return success

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
