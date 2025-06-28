"""
YouTube upload task for uploading videos to YouTube.
"""

import os
from typing import Dict, Any, Optional
from dataclasses import dataclass

from .base_upload_task import BaseUploadTask


@dataclass(unsafe_hash=True)
class YoutubeUploadTask(BaseUploadTask):
    """
    Task for uploading videos to YouTube.
    
    Handles the upload process including authentication and metadata.
    """
    
    group_dir: str
    
    def get_platform(self) -> str:
        """Return the platform identifier."""
        return "youtube"
    
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
            "group_dir": self.group_dir
        }
    
    async def execute(self, task_queue_service: Optional['TaskQueueService'] = None) -> bool:
        """
        Execute the YouTube upload task.
        
        Args:
            task_queue_service: Service for queueing additional tasks
            
        Returns:
            True if upload succeeded, False otherwise
        """
        import logging
        logger = logging.getLogger(__name__)
        
        try:
            # Import YouTube upload functionality
            from video_grouper.youtube_upload import upload_group_videos, get_youtube_paths
            
            # Get storage path from group directory
            storage_path = os.path.dirname(self.group_dir)
            while storage_path and not os.path.exists(os.path.join(storage_path, 'config.ini')):
                parent = os.path.dirname(storage_path)
                if parent == storage_path:  # Reached root
                    storage_path = os.path.dirname(self.group_dir)
                    break
                storage_path = parent
            
            # Get credentials and token file paths
            credentials_file, token_file = get_youtube_paths(storage_path)
            
            # Check if credentials file exists
            if not os.path.exists(credentials_file):
                logger.error(f"YouTube credentials file not found: {credentials_file}")
                return False
            
            logger.info(f"Starting YouTube upload for {self.group_dir}")
            
            # Upload the videos (no playlist config for now - can be added later)
            success = upload_group_videos(self.group_dir, credentials_file, token_file, None)
            
            if success:
                logger.info(f"Successfully uploaded videos for {self.group_dir} to YouTube")
                return True
            else:
                logger.error(f"Failed to upload videos for {self.group_dir} to YouTube")
                return False
                
        except ImportError as e:
            logger.error(f"YouTube upload functionality not available: {e}")
            return False
        except Exception as e:
            logger.error(f"Error during YouTube upload for {self.group_dir}: {e}")
            return False
    
    def __str__(self) -> str:
        """String representation of the task."""
        return f"YoutubeUploadTask({os.path.basename(self.group_dir)})"
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'YoutubeUploadTask':
        """
        Create a YoutubeUploadTask from serialized data.
        
        Args:
            data: Dictionary containing task data
            
        Returns:
            YoutubeUploadTask instance
        """
        # Handle both 'group_dir' and 'item_path' for backward compatibility
        group_dir = data.get('group_dir') or data.get('item_path')
        return cls(group_dir=group_dir) 