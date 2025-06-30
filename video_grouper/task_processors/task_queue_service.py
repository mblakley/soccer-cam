"""
Task queue service for routing tasks to appropriate processors.
"""

import logging
from typing import Optional, TYPE_CHECKING

from .tasks.queue_type import QueueType

if TYPE_CHECKING:
    from .video_processor import VideoProcessor
    from .upload_processor import UploadProcessor
    from .download_processor import DownloadProcessor
    from .tasks.base_task import BaseTask

logger = logging.getLogger(__name__)


class TaskQueueService:
    """
    Service for routing tasks to the appropriate processors.

    This service allows tasks to queue other tasks during execution
    without having direct references to processors.
    """

    def __init__(self):
        self._video_processor: Optional["VideoProcessor"] = None
        self._upload_processor: Optional["UploadProcessor"] = None
        self._download_processor: Optional["DownloadProcessor"] = None

    def set_video_processor(self, processor: "VideoProcessor") -> None:
        """Set the video processor instance."""
        self._video_processor = processor

    def set_upload_processor(self, processor: "UploadProcessor") -> None:
        """Set the upload processor instance."""
        self._upload_processor = processor

    def set_download_processor(self, processor: "DownloadProcessor") -> None:
        """Set the download processor instance."""
        self._download_processor = processor

    async def queue_task(self, task: "BaseTask") -> bool:
        """
        Queue a task to the appropriate processor based on queue type.

        Args:
            task: BaseTask to queue

        Returns:
            True if task was queued successfully, False otherwise
        """
        try:
            queue_type = task.queue_type
            task_type = task.task_type

            # Route based on queue type
            if queue_type == QueueType.VIDEO:
                if self._video_processor:
                    await self._video_processor.add_item(task)
                    logger.info(
                        f"QUEUE: Queued {task_type} task to video processor: {task}"
                    )
                    return True
                else:
                    logger.error(
                        f"QUEUE: No video processor available for {task_type} task: {task}"
                    )
                    return False

            elif queue_type == QueueType.UPLOAD:
                if self._upload_processor:
                    await self._upload_processor.add_item(task)
                    logger.info(
                        f"QUEUE: Queued {task_type} task to upload processor: {task}"
                    )
                    return True
                else:
                    logger.error(
                        f"QUEUE: No upload processor available for {task_type} task: {task}"
                    )
                    return False

            elif queue_type == QueueType.DOWNLOAD:
                if self._download_processor:
                    await self._download_processor.add_item(task)
                    logger.info(
                        f"QUEUE: Queued {task_type} task to download processor: {task}"
                    )
                    return True
                else:
                    logger.error(
                        f"QUEUE: No download processor available for {task_type} task: {task}"
                    )
                    return False

            else:
                logger.error(
                    f"QUEUE: Unknown queue type: {queue_type} for task: {task}"
                )
                return False

        except Exception as e:
            logger.error(f"QUEUE: Error queueing task {task}: {e}")
            return False


# Global instance
_task_queue_service: Optional[TaskQueueService] = None


def get_task_queue_service() -> TaskQueueService:
    """
    Get the global task queue service instance.

    Returns:
        TaskQueueService instance
    """
    global _task_queue_service
    if _task_queue_service is None:
        _task_queue_service = TaskQueueService()
    return _task_queue_service
