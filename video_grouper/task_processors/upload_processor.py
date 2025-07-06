import logging

from .base_queue_processor import QueueProcessor
from .tasks.upload import BaseUploadTask
from .queue_type import QueueType

logger = logging.getLogger(__name__)


class UploadProcessor(QueueProcessor):
    """
    Task processor for upload operations (YouTube, etc.).
    Processes upload tasks sequentially.
    """

    @property
    def queue_type(self) -> QueueType:
        """Return the queue type for this processor."""
        return QueueType.UPLOAD

    async def process_item(self, item: BaseUploadTask) -> None:
        """
        Process an upload task.

        Args:
            item: BaseUploadTask to process
        """
        try:
            logger.info(f"UPLOAD: Processing task: {item}")

            # Execute the task using its own execute method
            success = await item.execute()

            if success:
                logger.info(f"UPLOAD: Successfully completed task: {item}")
            else:
                logger.error(f"UPLOAD: Task execution failed: {item}")

        except Exception as e:
            logger.error(f"UPLOAD: Error processing task {item}: {e}")

    def get_item_key(self, item: BaseUploadTask) -> str:
        """Get unique key for a BaseUploadTask."""
        return f"{item.task_type}:{item.get_item_path()}:{hash(item)}"
