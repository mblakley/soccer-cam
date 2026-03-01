"""
Queue processor that executes clip extraction and highlight compilation tasks.

Handles ClipExtractionTask (FFmpeg trim) and HighlightCompilationTask (FFmpeg concat),
then uploads results to YouTube and updates the API.
"""

import logging
import os
from typing import Optional

from .base_queue_processor import QueueProcessor
from .queue_type import QueueType
from .tasks.base_task import BaseTask
from .tasks.clips.clip_extraction_task import ClipExtractionTask
from .tasks.clips.highlight_compilation_task import HighlightCompilationTask
from video_grouper.api_integrations.moment_api_client import MomentApiClient
from video_grouper.utils.config import Config

logger = logging.getLogger(__name__)


class ClipProcessor(QueueProcessor):
    """Processes clip extraction and highlight compilation tasks."""

    def __init__(
        self,
        storage_path: str,
        config: Config,
        api_client: MomentApiClient,
        youtube_uploader=None,
    ):
        super().__init__(storage_path, config)
        self.api_client = api_client
        self.youtube_uploader = youtube_uploader

    @property
    def queue_type(self) -> QueueType:
        return QueueType.CLIPS

    async def process_item(self, item: BaseTask) -> None:
        """Route to the appropriate handler based on task type."""
        if isinstance(item, ClipExtractionTask):
            await self._process_clip(item)
        elif isinstance(item, HighlightCompilationTask):
            await self._process_highlight(item)
        else:
            logger.warning("CLIP_PROC: Unknown task type: %s", type(item).__name__)

    async def _process_clip(self, task: ClipExtractionTask) -> None:
        """Extract a clip, optionally upload to YouTube, and update the API."""
        # Mark as generating
        await self.api_client.update_clip(task.clip_id, status="generating")

        success = await task.execute()

        if not success:
            await self.api_client.update_clip(task.clip_id, status="failed")
            return

        # Upload to YouTube if configured
        youtube_video_id = await self._upload_to_youtube(
            task.clip_output_path,
            title=f"Clip {task.tag_id[:8]}",
            description=f"Moment clip from game session {task.game_session_id[:8]}",
        )

        # Update clip record
        await self.api_client.update_clip(
            task.clip_id,
            status="ready",
            file_path=task.clip_output_path,
            youtube_video_id=youtube_video_id,
        )
        logger.info("CLIP_PROC: Clip %s ready", task.clip_id[:8])

    async def _process_highlight(self, task: HighlightCompilationTask) -> None:
        """Compile a highlight reel, upload to YouTube, and update the API."""
        success = await task.execute()

        if not success:
            await self.api_client.update_highlight(task.highlight_id, status="failed")
            return

        # Upload to YouTube if configured
        youtube_video_id = await self._upload_to_youtube(
            task.output_path,
            title=task.title,
            description=f"Highlight reel for {task.player_name}"
            if task.player_name
            else "",
        )

        # Update highlight record
        await self.api_client.update_highlight(
            task.highlight_id,
            status="ready",
            file_path=task.output_path,
            youtube_video_id=youtube_video_id,
        )
        logger.info("CLIP_PROC: Highlight %s ready", task.highlight_id[:8])

    async def _upload_to_youtube(
        self, video_path: str, title: str, description: str
    ) -> Optional[str]:
        """Upload a video to YouTube if the uploader is configured.

        Returns the YouTube video ID, or None.
        """
        if not self.youtube_uploader:
            return None

        if not os.path.isfile(video_path):
            logger.warning("CLIP_PROC: Video file not found for upload: %s", video_path)
            return None

        try:
            video_id = self.youtube_uploader.upload_video(
                video_path,
                title=title,
                description=description,
                privacy_status=self.config.youtube.privacy_status,
            )
            if video_id:
                logger.info("CLIP_PROC: Uploaded to YouTube: %s", video_id)
            return video_id
        except Exception as exc:
            logger.error("CLIP_PROC: YouTube upload failed for %s: %s", video_path, exc)
            return None
