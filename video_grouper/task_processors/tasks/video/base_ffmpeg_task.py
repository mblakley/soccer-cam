"""
Base class for FFmpeg tasks.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional, Callable, Awaitable

from ..base_task import BaseTask
from ..queue_type import QueueType

logger = logging.getLogger(__name__)


class BaseFfmpegTask(BaseTask):
    """
    Base class for all FFmpeg-related tasks.
    
    Provides common functionality for executing FFmpeg commands
    and handling the results.
    """
    
    @property
    def queue_type(self) -> QueueType:
        """Return the queue type for routing this task."""
        return QueueType.VIDEO
    
    @abstractmethod
    def get_command(self) -> List[str]:
        """Return the FFmpeg command to execute."""
        pass
    
    async def execute(self, queue_task: Optional[Callable[[Any], Awaitable[None]]] = None) -> bool:
        """
        Execute the FFmpeg command.
        
        Args:
            queue_task: Function to queue additional tasks
            
        Returns:
            True if command succeeded, False otherwise
        """
        command = self.get_command()
        
        try:
            logger.info(f"FFMPEG: Executing command: {' '.join(command)}")
            
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate()
            
            if process.returncode == 0:
                logger.info(f"FFMPEG: Command completed successfully for {self.task_type}")
                return True
            else:
                logger.error(f"FFMPEG: Command failed with return code {process.returncode}")
                logger.error(f"FFMPEG: stderr: {stderr.decode()}")
                return False
                
        except Exception as e:
            logger.error(f"FFMPEG: Error executing command: {e}")
            return False
    
    def to_dict(self) -> Dict[str, Any]:
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