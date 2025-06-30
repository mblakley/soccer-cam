import logging
from typing import Any
from .queue_processor_base import QueueProcessor
from .tasks.upload import BaseUploadTask
from .task_queue_service import get_task_queue_service

logger = logging.getLogger(__name__)


class UploadProcessor(QueueProcessor):
    """
    Task processor for upload operations (YouTube, etc.).
    Processes upload tasks sequentially.
    """

    def __init__(self, storage_path: str, config: Any):
        super().__init__(storage_path, config)

        # Register this processor with the task queue service
        task_queue_service = get_task_queue_service()
        task_queue_service.set_upload_processor(self)

    def get_state_file_name(self) -> str:
        return "upload_queue_state.json"

    async def process_item(self, item: BaseUploadTask) -> None:
        """
        Process an upload task.

        Args:
            item: BaseUploadTask to process
        """
        try:
            logger.info(f"UPLOAD: Processing task: {item}")

            # Get the task queue service to pass to the task
            task_queue_service = get_task_queue_service()

            # Execute the task using its own execute method
            success = await item.execute(task_queue_service)

            if success:
                logger.info(f"UPLOAD: Successfully completed task: {item}")
            else:
                logger.error(f"UPLOAD: Task execution failed: {item}")

        except Exception as e:
            logger.error(f"UPLOAD: Error processing task {item}: {e}")

    def get_item_key(self, item: BaseUploadTask) -> str:
        """Get unique key for a BaseUploadTask."""
        return f"{item.task_type}:{item.get_item_path()}:{hash(item)}"
