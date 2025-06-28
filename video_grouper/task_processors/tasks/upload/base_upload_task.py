"""
Base class for upload tasks.
"""

import os
import asyncio
import logging
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
from dataclasses import dataclass

from ..base_task import BaseTask
from ..queue_type import QueueType

logger = logging.getLogger(__name__)


@dataclass(unsafe_hash=True)
class BaseUploadTask(BaseTask):
    """
    Base class for all upload tasks.
    
    Provides common interface for uploading videos to various platforms.
    """
    
    @property
    def queue_type(self) -> QueueType:
        """Return the queue type for routing this task."""
        return QueueType.UPLOAD
    
    @property
    def task_type(self) -> str:
        """Return the specific task type identifier."""
        return f"{self.get_platform()}_upload"
    
    @abstractmethod
    def get_platform(self) -> str:
        """Return the platform identifier (e.g., 'youtube', 'vimeo')."""
        pass
    
    @abstractmethod
    def get_item_path(self) -> str:
        """
        Return the path or identifier for this task.
        
        Returns:
            String path or identifier for the task
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
    async def execute(self, task_queue_service: Optional['TaskQueueService'] = None) -> bool:
        """
        Execute the upload task.
        
        Args:
            task_queue_service: Service for queueing additional tasks
            
        Returns:
            True if upload succeeded, False otherwise
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