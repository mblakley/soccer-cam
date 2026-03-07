"""Ball Tracking Queue Processor -- drop-in replacement for AutocamProcessor.

Scans for groups with 'trimmed' status and processes them through the
ball tracking + virtual camera pipeline.
"""

import json
import logging
from pathlib import Path

from .base_queue_processor import QueueProcessor
from .tasks.ball_tracking import BallTrackingTask
from .queue_type import QueueType
from .autocam_utils import get_autocam_input_output_paths
from video_grouper.utils.config import Config

logger = logging.getLogger(__name__)


class BallTrackingProcessor(QueueProcessor):
    """Task processor for ball tracking operations.

    Drop-in replacement for AutocamProcessor. Processes trimmed videos
    through the ML ball tracking + virtual camera pipeline.
    """

    def __init__(self, storage_path: str, config: Config, upload_processor=None):
        super().__init__(storage_path, config)
        self.upload_processor = upload_processor

    @property
    def queue_type(self) -> QueueType:
        return QueueType.TRACKING

    async def process_item(self, item: BallTrackingTask) -> None:
        try:
            logger.info(f"BALL_TRACKING: Processing task: {item}")
            success = await item.execute()

            if success:
                logger.info(f"BALL_TRACKING: Successfully completed task: {item}")
                await self._handle_successful_completion(item)
            else:
                logger.error(f"BALL_TRACKING: Task execution failed: {item}")

        except Exception as e:
            logger.error(f"BALL_TRACKING: Error processing task {item}: {e}")

    def get_item_key(self, item: BallTrackingTask) -> str:
        return f"{item.task_type}:{item.get_item_path()}:{hash(item)}"

    async def _handle_successful_completion(self, item: BallTrackingTask) -> None:
        group_dir = item.group_dir
        group_name = group_dir.name

        logger.info(
            f"BALL_TRACKING: Updating group '{group_name}' status to autocam_complete."
        )
        state_file = group_dir / "state.json"

        if state_file.exists():
            try:
                with open(state_file, "r") as f:
                    state_data = json.load(f)
                state_data["status"] = "autocam_complete"
                with open(state_file, "w") as f:
                    json.dump(state_data, f, indent=4)

                if self.config.youtube.enabled:
                    logger.info(
                        f"BALL_TRACKING: Adding group '{group_name}' to YouTube upload queue."
                    )
                    await self._add_to_youtube_queue(group_dir)

            except (json.JSONDecodeError, IOError) as e:
                logger.error(
                    f"BALL_TRACKING: Could not update status for group {group_name}: {e}"
                )

    async def discover_work(self) -> None:
        """Scan for groups with 'trimmed' status and create ball tracking tasks."""
        storage = Path(self.storage_path)
        if not storage.exists():
            return

        for entry in storage.iterdir():
            if not entry.is_dir():
                continue

            state_file = entry / "state.json"
            if not state_file.exists():
                continue

            try:
                with open(state_file, "r") as f:
                    state_data = json.load(f)
            except (json.JSONDecodeError, IOError):
                continue

            if state_data.get("status") != "trimmed":
                continue

            try:
                input_path, output_path = get_autocam_input_output_paths(
                    entry, output_ext="mp4"
                )
            except FileNotFoundError:
                continue

            task = BallTrackingTask(
                group_dir=entry,
                input_path=input_path,
                output_path=output_path,
                ball_tracking_config=self.config.ball_tracking,
            )
            await self.add_work(task)

    async def _add_to_youtube_queue(self, group_dir: Path) -> None:
        try:
            from video_grouper.task_processors.tasks.upload import YoutubeUploadTask

            relative_group_dir = group_dir.relative_to(Path(self.storage_path))
            youtube_task = YoutubeUploadTask(group_dir=str(relative_group_dir))

            if self.upload_processor:
                await self.upload_processor.add_work(youtube_task)
                logger.info(
                    f"BALL_TRACKING: Added YouTube upload task for group {group_dir.name}"
                )
            else:
                logger.warning(
                    f"BALL_TRACKING: No upload processor available for group {group_dir.name}"
                )

        except Exception as e:
            logger.error(
                f"BALL_TRACKING: Error creating upload task for group {group_dir.name}: {e}"
            )
