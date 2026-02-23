import asyncio
import logging
from typing import Any, Optional

from video_grouper.task_processors.upload_processor import UploadProcessor
from video_grouper.utils.config import Config
from video_grouper.utils.paths import get_combined_video_path
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
        self,
        storage_path: str,
        config: Config,
        upload_processor: UploadProcessor,
        match_info_service: Optional[Any] = None,
        ntfy_processor: Optional[Any] = None,
    ):
        super().__init__(storage_path, config)
        self.upload_processor = upload_processor
        self.match_info_service = match_info_service
        self.ntfy_processor = ntfy_processor

    @property
    def queue_type(self) -> QueueType:
        """Return the queue type for this processor."""
        return QueueType.VIDEO

    async def process_item(self, item: BaseFfmpegTask) -> None:
        """
        Process a video task (combine or trim).

        After successful completion, triggers event-driven transitions:
        - CombineTask → async match info gathering (APIs + NTFY)
        - TrimTask → AutocamDiscoveryProcessor picks up "trimmed" status

        Args:
            item: BaseFfmpegTask to process
        """
        try:
            logger.info(f"VIDEO: Processing task: {item}")

            # Execute the task using its own execute method
            success = await item.execute()

            if success:
                logger.info(f"VIDEO: Successfully completed task: {item}")

                # Trigger event-driven transitions based on task type
                if item.task_type == "combine":
                    asyncio.create_task(self._on_combine_complete(item.get_item_path()))
            else:
                logger.error(f"VIDEO: Task execution failed: {item}")

        except Exception as e:
            logger.error(f"VIDEO: Error processing task {item}: {e}")

    async def _on_combine_complete(self, group_dir: str) -> None:
        """Trigger match info gathering after a successful combine.

        Runs asynchronously so the video queue can continue processing
        other groups' tasks without waiting for API calls or user input.
        """
        try:
            combined_path = get_combined_video_path(group_dir, self.storage_path)

            # Try API-based population first (TeamSnap, PlayMetrics)
            if self.match_info_service:
                logger.info(f"VIDEO: Triggering API-based match info for {group_dir}")
                await self.match_info_service.populate_match_info_from_apis(group_dir)

            # Queue NTFY tasks for remaining info (game start time, team info
            # if APIs didn't find it). request_match_info_for_directory() skips
            # fields that are already populated.
            if self.ntfy_processor:
                logger.info(
                    f"VIDEO: Triggering NTFY match info request for {group_dir}"
                )
                await self.ntfy_processor.request_match_info_for_directory(
                    group_dir, combined_path
                )

        except Exception as e:
            logger.error(
                f"VIDEO: Error in post-combine transition for {group_dir}: {e}"
            )

    def get_item_key(self, item: BaseFfmpegTask) -> str:
        """Get unique key for a BaseFfmpegTask."""
        return f"{item.task_type}:{item.get_item_path()}"
