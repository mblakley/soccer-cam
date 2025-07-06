"""
Base class for FFmpeg tasks.
"""

import logging
from abc import abstractmethod
from typing import Dict

from ..base_task import BaseTask
from ...queue_type import QueueType

logger = logging.getLogger(__name__)


class BaseFfmpegTask(BaseTask):
    """
    Base class for all FFmpeg-related tasks.

    Provides common functionality for task identification and routing.
    """

    @property
    def queue_type(self) -> QueueType:
        """Return the queue type for routing this task."""
        return QueueType.VIDEO

    @abstractmethod
    async def execute(self) -> bool:
        """Execute the FFmpeg task."""
        pass

    def to_dict(self) -> Dict[str, object]:
        """
        Convert task to dictionary format (alias for serialize for backward compatibility).

        Returns:
            Dictionary representation of the task
        """
        return self.serialize()

    @property
    def task_type(self) -> str:
        """Backward compatibility property for task_type."""
        return self.queue_type.value

    @property
    def item_path(self) -> str:
        """Backward compatibility property for item_path."""
        return self.get_item_path()
