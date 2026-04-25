import asyncio
import logging
import os
from typing import Any, Optional

from video_grouper.task_processors.upload_processor import UploadProcessor
from video_grouper.utils.config import Config
from video_grouper.utils.paths import get_combined_video_path, get_match_info_path
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
        self.ttt_reporter = None

    @property
    def queue_type(self) -> QueueType:
        """Return the queue type for this processor."""
        return QueueType.VIDEO

    def _get_priority(self, item) -> int:
        """Trim tasks (final step) prioritize over combine tasks (intermediate)."""
        if hasattr(item, "task_type") and item.task_type == "trim":
            return 1
        return 2

    async def process_item(self, item: BaseFfmpegTask) -> None:
        """
        Process a video task (combine or trim).

        After successful completion, triggers event-driven transitions:
        - CombineTask → async match info gathering (APIs + NTFY)
        - TrimTask → AutocamDiscoveryProcessor picks up "trimmed" status,
          or if autocam is disabled, skips directly to upload

        Args:
            item: BaseFfmpegTask to process
        """
        try:
            logger.info(f"VIDEO: Processing task: {item}")

            # Report stage start to TTT (best-effort)
            if self.ttt_reporter and item.task_type in ("combine", "trim"):
                group_dir = item.get_item_path()
                try:
                    from video_grouper.models import DirectoryState

                    dir_state = DirectoryState(group_dir)
                    await self.ttt_reporter.update_recording_status(
                        dir_state.ttt_recording_id, item.task_type, "in_progress"
                    )
                except Exception:
                    pass  # Never block video processing on TTT

            # Execute the task using its own execute method
            success = await item.execute()

            if success:
                logger.info(f"VIDEO: Successfully completed task: {item}")

                # Report stage completion to TTT (best-effort)
                if self.ttt_reporter and item.task_type in ("combine", "trim"):
                    group_dir = item.get_item_path()
                    try:
                        from video_grouper.models import DirectoryState

                        dir_state = DirectoryState(group_dir)
                        await self.ttt_reporter.update_recording_status(
                            dir_state.ttt_recording_id, item.task_type, "complete"
                        )
                    except Exception:
                        pass  # Never block video processing on TTT

                # Trigger event-driven transitions based on task type
                if item.task_type == "combine":
                    asyncio.create_task(self._on_combine_complete(item.get_item_path()))
                elif item.task_type == "trim" and not self.config.ball_tracking.enabled:
                    asyncio.create_task(self._on_trim_complete(item.get_item_path()))
            else:
                logger.error(f"VIDEO: Task execution failed: {item}")

                # Report stage failure to TTT (best-effort)
                if self.ttt_reporter and item.task_type in ("combine", "trim"):
                    group_dir = item.get_item_path()
                    try:
                        from video_grouper.models import DirectoryState

                        dir_state = DirectoryState(group_dir)
                        await self.ttt_reporter.update_recording_status(
                            dir_state.ttt_recording_id, item.task_type, "failed"
                        )
                    except Exception:
                        pass  # Never block video processing on TTT

        except Exception as e:
            logger.error(f"VIDEO: Error processing task {item}: {e}")

    async def _on_combine_complete(self, group_dir: str) -> None:
        """Trigger match info gathering and trim after a successful combine.

        If match_info.ini is already fully populated (team info + start time),
        queues the trim immediately. Otherwise falls back to API lookups and
        NTFY prompts.
        """
        try:
            combined_path = get_combined_video_path(group_dir, self.storage_path)

            # Check if match_info is already fully populated (e.g. pre-set by user)
            from video_grouper.models import MatchInfo

            match_info_path = get_match_info_path(group_dir, self.storage_path)
            if os.path.exists(match_info_path):
                match_info = MatchInfo.from_file(match_info_path)
                if match_info and match_info.is_populated():
                    logger.info(
                        f"VIDEO: Match info already fully populated for {group_dir}, "
                        f"queuing trim directly"
                    )
                    from video_grouper.task_processors.tasks.video import TrimTask

                    await self.add_work(TrimTask(group_dir))
                    return

            # Try API-based population first (TeamSnap, PlayMetrics)
            if self.match_info_service:
                logger.info(f"VIDEO: Triggering API-based match info for {group_dir}")
                await self.match_info_service.populate_match_info_from_apis(group_dir)

            # Check again after API population - APIs may have filled team info
            # but not start_time_offset, so we may still need NTFY for that
            if os.path.exists(match_info_path):
                match_info = MatchInfo.from_file(match_info_path)
                if match_info and match_info.is_populated():
                    logger.info(
                        f"VIDEO: Match info populated after API lookup for {group_dir}, "
                        f"queuing trim directly"
                    )
                    from video_grouper.task_processors.tasks.video import TrimTask

                    await self.add_work(TrimTask(group_dir))
                    return

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

    async def _on_trim_complete(self, group_dir: str) -> None:
        """Skip ball-tracking and transition directly to upload when disabled."""
        try:
            from video_grouper.models import DirectoryState

            dir_state = DirectoryState(group_dir)
            await dir_state.update_group_status("ball_tracking_complete")
            logger.info(
                f"VIDEO: Ball tracking disabled, set {group_dir} to ball_tracking_complete"
            )

            if self.config.youtube.enabled and self.upload_processor:
                from video_grouper.task_processors.tasks.upload import YoutubeUploadTask

                relative_group_dir = os.path.relpath(group_dir, self.storage_path)
                youtube_task = YoutubeUploadTask(group_dir=relative_group_dir)
                await self.upload_processor.add_work(youtube_task)
                logger.info(f"VIDEO: Queued YouTube upload for {group_dir}")
        except Exception as e:
            logger.error(f"VIDEO: Error in post-trim transition for {group_dir}: {e}")

    def get_item_key(self, item: BaseFfmpegTask) -> str:
        """Get unique key for a BaseFfmpegTask."""
        return f"{item.task_type}:{item.get_item_path()}"
