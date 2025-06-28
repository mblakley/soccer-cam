"""
Dahua download task for downloading files from Dahua cameras.
"""

import os
import logging
from typing import Dict, Any, Optional, Callable, Awaitable
from dataclasses import dataclass
from datetime import datetime

from .base_download_task import BaseDownloadTask

logger = logging.getLogger(__name__)


@dataclass(unsafe_hash=True)
class DahuaDownloadTask(BaseDownloadTask):
    """
    Task for downloading files from Dahua cameras.
    
    Contains all information needed to download a file from a Dahua camera
    including connection details, file paths, and timing information.
    """
    
    camera_ip: str
    username: str
    password: str
    local_file_path: str
    remote_file_path: str
    start_time: datetime
    end_time: datetime
    file_size: int = 0
    metadata: Dict[str, Any] = None
    
    def __post_init__(self):
        """Initialize metadata if not provided."""
        if self.metadata is None:
            self.metadata = {}
    
    def get_camera_type(self) -> str:
        """Return the camera type identifier."""
        return "dahua"
    
    def get_item_path(self) -> str:
        """Return the local file path."""
        return self.local_file_path
    
    def get_remote_path(self) -> str:
        """Return the remote file path."""
        return self.remote_file_path
    
    def get_camera_config(self) -> Dict[str, Any]:
        """Return the camera configuration."""
        return {
            "ip": self.camera_ip,
            "username": self.username,
            "password": self.password
        }
    
    def serialize(self) -> Dict[str, Any]:
        """
        Serialize the task for state persistence.
        
        Returns:
            Dictionary containing task data
        """
        return {
            "task_type": self.task_type,
            "camera_ip": self.camera_ip,
            "username": self.username,
            "password": self.password,
            "local_file_path": self.local_file_path,
            "remote_file_path": self.remote_file_path,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat(),
            "file_size": self.file_size,
            "metadata": self.metadata or {}
        }
    
    async def execute(self, queue_task: Optional[Callable[[Any], Awaitable[None]]] = None) -> bool:
        """
        Execute the download task.
        
        Args:
            queue_task: Function to queue additional tasks
            
        Returns:
            True if download succeeded, False otherwise
        """
        try:
            from video_grouper.cameras.dahua import DahuaCamera
            
            # Create camera instance
            camera = DahuaCamera(
                ip=self.camera_ip,
                username=self.username,
                password=self.password
            )
            
            # Ensure local directory exists
            os.makedirs(os.path.dirname(self.local_file_path), exist_ok=True)
            
            logger.info(f"DOWNLOAD: Starting download from {self.camera_ip}: {self.remote_file_path}")
            
            # Download the file
            success = await camera.download_file(
                remote_path=self.remote_file_path,
                local_path=self.local_file_path,
                start_time=self.start_time,
                end_time=self.end_time
            )
            
            if success:
                logger.info(f"DOWNLOAD: Successfully downloaded {self.remote_file_path} to {self.local_file_path}")
                
                # Queue conversion task if this is a video file
                if self.local_file_path.endswith('.dav') and queue_task:
                    from ..video import ConvertTask
                    convert_task = ConvertTask(file_path=self.local_file_path)
                    await queue_task(convert_task)
                    logger.info(f"DOWNLOAD: Queued convert task for {self.local_file_path}")
                
                return True
            else:
                logger.error(f"DOWNLOAD: Failed to download {self.remote_file_path}")
                return False
                
        except Exception as e:
            logger.error(f"DOWNLOAD: Error downloading {self.remote_file_path}: {e}")
            return False
    
    def __str__(self) -> str:
        """String representation of the task."""
        return f"DahuaDownloadTask({os.path.basename(self.local_file_path)})"
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'DahuaDownloadTask':
        """
        Create a DahuaDownloadTask from serialized data.
        
        Args:
            data: Dictionary containing task data
            
        Returns:
            DahuaDownloadTask instance
        """
        return cls(
            camera_ip=data['camera_ip'],
            username=data['username'],
            password=data['password'],
            local_file_path=data['local_file_path'],
            remote_file_path=data['remote_file_path'],
            start_time=datetime.fromisoformat(data['start_time']),
            end_time=datetime.fromisoformat(data['end_time']),
            file_size=data.get('file_size', 0),
            metadata=data.get('metadata', {})
        ) 