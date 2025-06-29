import os
import logging
from typing import Any

from video_grouper.task_processors.tasks.download import BaseDownloadTask
from .queue_processor_base import QueueProcessor
from video_grouper.utils.directory_state import DirectoryState
from video_grouper.models import RecordingFile
from .tasks.video import ConvertTask

logger = logging.getLogger(__name__)

class DownloadProcessor(QueueProcessor):
    """
    Task processor for downloading files from the camera.
    Processes download queue sequentially, one file at a time.
    """
    
    def __init__(self, storage_path: str, config: Any, camera: Any):
        super().__init__(storage_path, config)
        self.camera = camera
        self.video_processor = None
        
    def set_video_processor(self, video_processor):
        """Set reference to video processor to queue work."""
        self.video_processor = video_processor
    
    def get_state_file_name(self) -> str:
        return "download_queue_state.json"
    
    async def process_item(self, item: RecordingFile) -> None:
        """
        Download a single file from the camera.
        
        Args:
            item: RecordingFile object to download
        """
        file_path = item.file_path
        group_dir = os.path.dirname(file_path)
        dir_state = DirectoryState(group_dir)

        try:
            logger.info(f"DOWNLOAD: Starting download of {os.path.basename(file_path)}")
            await dir_state.update_file_state(file_path, status="downloading")
            
            # Download the file from camera
            download_successful = await self.camera.download_file(
                file_path=item.metadata['path'],
                local_path=file_path
            )

            if download_successful:
                await dir_state.update_file_state(file_path, status="downloaded")
                logger.info(f"DOWNLOAD: Successfully downloaded {os.path.basename(file_path)}")
                
                # After successful download, add to video processor queue for conversion
                if self.video_processor:
                    await self.video_processor.add_work(ConvertTask(file_path=file_path))
            else:
                await dir_state.update_file_state(file_path, status="download_failed")
                logger.error(f"DOWNLOAD: Download failed for {os.path.basename(file_path)}")

        except Exception as e:
            logger.error(f"DOWNLOAD: An error occurred during download of {os.path.basename(file_path)}: {e}", exc_info=True)
            await dir_state.update_file_state(file_path, status="download_failed")
    
    def get_item_key(self, item: RecordingFile) -> str:
        return f"recording:{item.file_path}:{hash(item.file_path)}"