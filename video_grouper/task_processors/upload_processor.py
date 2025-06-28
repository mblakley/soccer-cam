import os
import logging
from typing import Any, Dict, Optional
from .queue_processor_base import QueueProcessor
from video_grouper.models import VideoUploadTask
from video_grouper.youtube_upload import upload_group_videos, get_youtube_paths

logger = logging.getLogger(__name__)

class UploadProcessor(QueueProcessor):
    """
    Task processor for video uploads.
    Processes video upload queue sequentially, one video at a time.
    """
    
    def __init__(self, storage_path: str, config: Any):
        super().__init__(storage_path, config)
        
    def get_state_file_name(self) -> str:
        return "upload_queue_state.json"
    
    async def process_item(self, item: VideoUploadTask) -> None:
        """
        Upload videos for a group directory.
        
        Args:
            item: VideoUploadTask to process
        """
        group_dir = item.item_path
        logger.info(f"UPLOAD: Processing upload task for {group_dir}")
        
        try:
            # Get the credentials and token file paths using the helper function
            credentials_file, token_file = get_youtube_paths(self.storage_path)
            
            # Check if credentials file exists
            if not os.path.exists(credentials_file):
                logger.error(f"UPLOAD: Credentials file not found: {credentials_file}")
                return
            
            # Get playlist configuration
            playlist_config = None
            if self.config.has_section('youtube.playlist.processed') and self.config.has_section('youtube.playlist.raw'):
                playlist_config = {
                    "processed": {
                        "name_format": self.config.get('youtube.playlist.processed', 'name_format', fallback="{my_team_name} 2013s"),
                        "description": self.config.get('youtube.playlist.processed', 'description', fallback="Processed videos"),
                        "privacy_status": self.config.get('youtube.playlist.processed', 'privacy_status', fallback="unlisted")
                    },
                    "raw": {
                        "name_format": self.config.get('youtube.playlist.raw', 'name_format', fallback="{my_team_name} 2013s - Full Field"),
                        "description": self.config.get('youtube.playlist.raw', 'description', fallback="Raw videos"),
                        "privacy_status": self.config.get('youtube.playlist.raw', 'privacy_status', fallback="unlisted")
                    }
                }
                logger.info(f"UPLOAD: Using playlist configuration: {playlist_config}")
            else:
                logger.info("UPLOAD: No playlist configuration found in config file, using defaults")
            
            # Upload the videos with playlist configuration
            success = upload_group_videos(group_dir, credentials_file, token_file, playlist_config)
            
            if success:
                logger.info(f"UPLOAD: Successfully uploaded videos for {group_dir}")
            else:
                logger.error(f"UPLOAD: Failed to upload videos for {group_dir}")
                
        except Exception as e:
            logger.error(f"UPLOAD: Error during upload for {group_dir}: {e}")
    
    def serialize_item(self, item: VideoUploadTask) -> Dict[str, Any]:
        """Serialize a VideoUploadTask for state persistence."""
        return item.to_dict()
    
    def deserialize_item(self, item_data: Dict[str, Any]) -> Optional[VideoUploadTask]:
        """Deserialize a VideoUploadTask from state data."""
        try:
            if 'item_path' in item_data:
                return VideoUploadTask(item_data['item_path'])
            else:
                logger.error(f"UPLOAD: Missing item_path in task data: {item_data}")
                return None
        except Exception as e:
            logger.error(f"UPLOAD: Failed to deserialize VideoUploadTask: {e}")
            return None
    
    def get_item_key(self, item: VideoUploadTask) -> str:
        """Get unique key for a VideoUploadTask."""
        return item.item_path 