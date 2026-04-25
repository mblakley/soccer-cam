"""Ball-tracking queue processor.

Drains the BALL_TRACKING queue, runs each task's configured provider,
updates the group's state.json to ``ball_tracking_complete`` on success,
and (when YouTube uploads are enabled) hands off to the upload processor.

Replaces the old ``AutocamProcessor``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from .base_queue_processor import QueueProcessor
from .queue_type import QueueType
from .tasks.ball_tracking import BallTrackingTask
from video_grouper.utils.config import Config

logger = logging.getLogger(__name__)


class BallTrackingProcessor(QueueProcessor):
    """Processes ball-tracking tasks sequentially (one at a time)."""

    def __init__(self, storage_path: str, config: Config, upload_processor=None):
        super().__init__(storage_path, config)
        self.upload_processor = upload_processor
        # Only one ball-tracking task runs at a time (GPU/UI contention).
        self._semaphore = asyncio.Semaphore(1)

    @property
    def queue_type(self) -> QueueType:
        return QueueType.BALL_TRACKING

    async def process_item(self, item: BallTrackingTask) -> None:
        try:
            logger.info("BALL_TRACKING: processing task: %s", item)
            success = await item.execute()
            if success:
                logger.info("BALL_TRACKING: task completed: %s", item)
                await self._handle_successful_completion(item)
            else:
                logger.error("BALL_TRACKING: task failed: %s", item)
        except Exception as e:
            logger.error("BALL_TRACKING: error processing task %s: %s", item, e)

    def get_item_key(self, item: BallTrackingTask) -> str:
        return f"{item.task_type}:{item.get_item_path()}:{hash(item)}"

    async def _handle_successful_completion(self, item: BallTrackingTask) -> None:
        group_dir = item.group_dir
        group_name = group_dir.name

        logger.info(
            "BALL_TRACKING: updating group '%s' status to ball_tracking_complete",
            group_name,
        )
        state_file = group_dir / "state.json"

        if not state_file.exists():
            logger.warning(
                "BALL_TRACKING: state.json not found for group %s on success",
                group_name,
            )
            return

        try:
            with open(state_file, "r") as f:
                state_data = json.load(f)
            state_data["status"] = "ball_tracking_complete"
            with open(state_file, "w") as f:
                json.dump(state_data, f, indent=4)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(
                "BALL_TRACKING: could not update status for group %s: %s",
                group_name,
                e,
            )
            return

        if self.config.youtube.enabled:
            logger.info(
                "BALL_TRACKING: queuing YouTube upload for group %s", group_name
            )
            await self._add_to_youtube_queue(group_dir)
        else:
            logger.info(
                "BALL_TRACKING: YouTube uploads disabled; skipping upload for %s",
                group_name,
            )

    async def _add_to_youtube_queue(self, group_dir: Path) -> None:
        try:
            from video_grouper.task_processors.tasks.upload import YoutubeUploadTask

            relative_group_dir = group_dir.relative_to(Path(self.storage_path))
            youtube_task = YoutubeUploadTask(group_dir=str(relative_group_dir))

            if self.upload_processor:
                await self.upload_processor.add_work(youtube_task)
                logger.info(
                    "BALL_TRACKING: queued YouTube upload for group %s",
                    group_dir.name,
                )
            else:
                logger.warning(
                    "BALL_TRACKING: no upload processor available for group %s",
                    group_dir.name,
                )
        except Exception as e:
            logger.error(
                "BALL_TRACKING: error creating YouTube upload task for %s: %s",
                group_dir.name,
                e,
            )
