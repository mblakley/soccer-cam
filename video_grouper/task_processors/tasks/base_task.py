"""
Base class for all tasks.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional

from .queue_type import QueueType


class BaseTask(ABC):
    """
    Base class for all tasks in the system.
    
    Provides common interface that all tasks must implement.
    """
    
    @property
    @abstractmethod
    def queue_type(self) -> QueueType:
        """Return the queue type for routing this task."""
        pass
    
    @property
    @abstractmethod
    def task_type(self) -> str:
        """Return the specific task type identifier (e.g., 'convert', 'youtube_upload')."""
        pass
    
    @abstractmethod
    def get_item_path(self) -> str:
        """Return the path of the item being processed."""
        pass
    
    @abstractmethod
    def serialize(self) -> Dict[str, Any]:
        """Serialize the task for state persistence."""
        pass
    
    @abstractmethod
    async def execute(self, task_queue_service: Optional['TaskQueueService'] = None) -> bool:
        """
        Execute the task.
        
        Args:
            task_queue_service: Service for queueing additional tasks
            
        Returns:
            True if task succeeded, False otherwise
        """
        pass
    
    def get_task_type(self) -> str:
        """Return the specific task type identifier (for backward compatibility)."""
        return self.task_type 