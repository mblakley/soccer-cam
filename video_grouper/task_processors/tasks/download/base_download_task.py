"""
Base class for download tasks.
"""

import os
import logging
from abc import abstractmethod
from typing import Dict, Any, Optional, Callable, Awaitable
from dataclasses import dataclass

from ..base_task import BaseTask
from ..queue_type import QueueType

logger = logging.getLogger(__name__)


@dataclass(unsafe_hash=True)
class BaseDownloadTask(BaseTask):
    """
    Base class for all download tasks.

    Provides common interface for downloading videos from various camera types.
    """

    @property
    def queue_type(self) -> QueueType:
        """Return the queue type for routing this task."""
        return QueueType.DOWNLOAD

    @property
    def task_type(self) -> str:
        """Return the specific task type identifier."""
        return f"{self.get_camera_type()}_download"

    @abstractmethod
    def get_camera_type(self) -> str:
        """Return the camera type identifier (e.g., 'dahua', 'hikvision')."""
        pass

    @abstractmethod
    def get_item_path(self) -> str:
        """
        Return the local path where the file will be downloaded.

        Returns:
            String path for the local file
        """
        pass

    @abstractmethod
    def get_remote_path(self) -> str:
        """
        Return the remote path on the camera.

        Returns:
            String path for the remote file
        """
        pass

    @abstractmethod
    def get_camera_config(self) -> Dict[str, Any]:
        """
        Return the camera configuration needed for download.

        Returns:
            Dictionary containing camera connection details
        """
        pass

    @abstractmethod
    def serialize(self) -> Dict[str, Any]:
        """
        Serialize the task to a dictionary for state persistence.

        Returns:
            Dictionary containing all data needed to recreate the task
        """
        pass

    @abstractmethod
    async def execute(
        self, queue_task: Optional[Callable[[Any], Awaitable[None]]] = None
    ) -> bool:
        """
        Execute the download task.

        Args:
            queue_task: Function to queue additional tasks (optional, not typically used for downloads)

        Returns:
            True if download succeeded, False otherwise
        """
        pass

    def to_dict(self) -> Dict[str, Any]:
        """
        Convert task to dictionary format (alias for serialize for backward compatibility).

        Returns:
            Dictionary representation of the task
        """
        return self.serialize()

    @property
    def item_path(self) -> str:
        """Backward compatibility property for item_path."""
        return self.get_item_path()

    def __str__(self) -> str:
        """String representation of the task."""
        return f"{self.__class__.__name__}({os.path.basename(self.get_item_path())})"
