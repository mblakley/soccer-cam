import logging

from video_grouper.task_processors.upload_processor import UploadProcessor
from video_grouper.utils.config import Config
from .base_queue_processor import QueueProcessor
from .tasks.video import BaseFfmpegTask
from .queue_type import QueueType

logger = logging.getLogger(__name__)


class VideoProcessor(QueueProcessor):
    """
    Task processor for video operations (combine, trim).
    Processes FFmpeg tasks sequentially.
    """

    def __init__(
        self, storage_path: str, config: Config, upload_processor: UploadProcessor
    ):
        super().__init__(storage_path, config)
        self.upload_processor = upload_processor

    @property
    def queue_type(self) -> QueueType:
        """Return the queue type for this processor."""
        return QueueType.VIDEO

    async def process_item(self, item: BaseFfmpegTask) -> None:
        """
        Process a video task (combine or trim).

        Args:
            item: BaseFfmpegTask to process
        """
        try:
            logger.info(f"VIDEO: Processing task: {item}")

            # Execute the task using its own execute method
            success = await item.execute()

            if success:
                logger.info(f"VIDEO: Successfully completed task: {item}")
            else:
                logger.error(f"VIDEO: Task execution failed: {item}")

        except Exception as e:
            logger.error(f"VIDEO: Error processing task {item}: {e}")

    def get_item_key(self, item: BaseFfmpegTask) -> str:
        """Get unique key for a BaseFfmpegTask."""
        return f"{item.task_type}:{item.get_item_path()}:{hash(item)}"
