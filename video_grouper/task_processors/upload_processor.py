import logging

from .base_queue_processor import QueueProcessor
from .tasks.upload import BaseUploadTask
from .queue_type import QueueType
from video_grouper.utils.config import Config

logger = logging.getLogger(__name__)


class UploadProcessor(QueueProcessor):
    """
    Task processor for upload operations (YouTube, etc.).
    Processes upload tasks sequentially.
    """

    def __init__(self, storage_path: str, config: Config):
        """Initialize the upload processor."""
        super().__init__(storage_path, config)
        self.config = config

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

            # Create dependencies for the task
            from video_grouper.task_processors.services.ntfy_service import NtfyService
            ntfy_service = NtfyService(self.config.ntfy, self.storage_path)

            # Execute the task using its own execute method with dependencies
            if hasattr(item, 'execute') and callable(getattr(item, 'execute')):
                # Check if the task accepts dependencies
                import inspect
                sig = inspect.signature(item.execute)
                if 'youtube_config' in sig.parameters or 'ntfy_service' in sig.parameters:
                    success = await item.execute(
                        youtube_config=self.config.youtube,
                        ntfy_service=ntfy_service
                    )
                else:
                    success = await item.execute()
            else:
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
