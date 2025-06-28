import os
import logging
import asyncio
from typing import Any, Dict, Optional
import aiofiles
from .queue_processor_base import QueueProcessor
from .tasks.video import BaseFfmpegTask
from .task_queue_service import get_task_queue_service

logger = logging.getLogger(__name__)

class VideoProcessor(QueueProcessor):
    """
    Task processor for video operations (convert, combine, trim).
    Processes FFmpeg tasks sequentially.
    """
    
    def __init__(self, storage_path: str, config: Any):
        super().__init__(storage_path, config)
        self.upload_processor = None
        
        # Register this processor with the task queue service
        task_queue_service = get_task_queue_service()
        task_queue_service.set_video_processor(self)
        
    def set_upload_processor(self, upload_processor):
        """Set reference to upload processor to queue work."""
        self.upload_processor = upload_processor
    
    def get_state_file_name(self) -> str:
        return "ffmpeg_queue_state.json"
    
    async def process_item(self, item: BaseFfmpegTask) -> None:
        """
        Process a video task (convert, combine, or trim).
        
        Args:
            item: BaseFfmpegTask to process
        """
        try:
            logger.info(f"VIDEO: Processing task: {item}")
            
            # Get the task queue service to pass to the task
            task_queue_service = get_task_queue_service()
            
            # Execute the task using its own execute method
            success = await item.execute(task_queue_service)
            
            if success:
                logger.info(f"VIDEO: Successfully completed task: {item}")
            else:
                logger.error(f"VIDEO: Task execution failed: {item}")
                
        except Exception as e:
            logger.error(f"VIDEO: Error processing task {item}: {e}")
    
    def get_item_key(self, item: BaseFfmpegTask) -> str:
        """Get unique key for a BaseFfmpegTask."""
        return f"{item.task_type}:{item.get_item_path()}:{hash(item)}" 