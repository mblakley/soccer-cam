"""TTT processing job processor -- polls TTT for pending jobs and executes them."""

import asyncio
import logging
import os

from ..utils.config import Config
from .base_queue_processor import QueueProcessor
from .queue_type import QueueType
from .tasks.ttt.ttt_job_task import TTTJobTask

logger = logging.getLogger(__name__)


class TTTJobProcessor(QueueProcessor):
    """Drains the TTT processing-job queue.

    Service registration + heartbeats are owned by :class:`TTTPoller`
    (they're cheap calls on the poll cadence). This processor's job is
    purely the per-task pipeline: claim → download → combine → trim →
    upload. The entire flow runs under ``ram_heavy``.
    """

    def __init__(
        self,
        storage_path: str,
        config: Config,
        ttt_client=None,
        camera=None,
        download_processor=None,
        video_processor=None,
        upload_processor=None,
        resource_manager=None,
    ):
        super().__init__(storage_path, config)
        self.ttt_client = ttt_client
        self.camera = camera
        self.download_processor = download_processor
        self.video_processor = video_processor
        self.upload_processor = upload_processor
        self.resource_manager = resource_manager
        self._service_id: str | None = None  # set by TTTPoller via setter

    @property
    def queue_type(self) -> QueueType:
        return QueueType.TTT_JOB

    def get_item_key(self, item: TTTJobTask) -> str:
        return item.ttt_id

    async def process_item(self, item: TTTJobTask) -> None:
        if self.ttt_client is None:
            raise RuntimeError("TTTJobProcessor needs a ttt_client; check TTT config")
        if self.resource_manager is not None:
            async with self.resource_manager.acquire(("ram_heavy",)):
                await self._process_job(self.ttt_client, item.payload)
        else:
            await self._process_job(self.ttt_client, item.payload)

    async def _process_job(self, ttt_client, job: dict) -> None:
        """Process a single TTT job through the pipeline."""
        job_id = job["id"]
        try:
            # Claim the job
            try:
                await asyncio.to_thread(ttt_client.claim_job, job_id)
                logger.info("TTT_JOBS: Claimed job %s", job_id)
            except Exception as e:
                logger.error("TTT_JOBS: Failed to claim job %s: %s", job_id, e)
                return

            config = job.get("config", {})

            # Step 1: Report downloading status
            await self._update_progress(
                ttt_client,
                job_id,
                "downloading",
                {"percent": 0, "message": "Starting download from camera"},
            )

            group_dir = await self._resolve_or_create_group(job, config)
            if not group_dir:
                await self._fail_job(
                    ttt_client, job_id, "Could not resolve recording group directory"
                )
                return

            # Step 2: Wait for combine (or trigger it)
            await self._update_progress(
                ttt_client,
                job_id,
                "combining",
                {"percent": 30, "message": "Combining video files"},
            )
            combined_path = await self._wait_for_combined(group_dir)
            if not combined_path:
                await self._fail_job(ttt_client, job_id, "Combined video not found")
                return

            # Step 3: Trim if config specifies trim times
            trim_start = config.get("trim_start")
            trim_end = config.get("trim_end")
            if trim_start is not None:
                await self._update_progress(
                    ttt_client,
                    job_id,
                    "trimming",
                    {"percent": 50, "message": "Trimming video"},
                )
                trimmed_path = await self._trim_video(
                    group_dir, combined_path, trim_start, trim_end
                )
                upload_path = trimmed_path or combined_path
            else:
                upload_path = combined_path

            # Step 4: Upload
            await self._update_progress(
                ttt_client,
                job_id,
                "uploading",
                {"percent": 70, "message": "Uploading to YouTube"},
            )
            youtube_url = await self._upload_video(group_dir, upload_path, config)

            # Step 5: Complete
            result = {"youtube_url": youtube_url} if youtube_url else {}
            await asyncio.to_thread(ttt_client.complete_job, job_id, result)
            logger.info("TTT_JOBS: Completed job %s", job_id)

        except Exception as e:
            logger.error(
                "TTT_JOBS: Error processing job %s: %s", job_id, e, exc_info=True
            )
            await self._fail_job(ttt_client, job_id, str(e))

    async def _update_progress(
        self, ttt_client, job_id: str, status: str, progress: dict
    ) -> None:
        """Update job progress in TTT."""
        try:
            await asyncio.to_thread(
                ttt_client.update_job_progress, job_id, status, progress
            )
        except Exception as e:
            logger.warning("TTT_JOBS: Failed to update progress for %s: %s", job_id, e)

    async def _fail_job(self, ttt_client, job_id: str, error: str) -> None:
        """Mark a job as failed in TTT."""
        try:
            await asyncio.to_thread(ttt_client.fail_job, job_id, error)
            logger.warning("TTT_JOBS: Job %s failed: %s", job_id, error)
        except Exception as e:
            logger.error("TTT_JOBS: Failed to report failure for %s: %s", job_id, e)

    async def _resolve_or_create_group(self, job: dict, config: dict) -> str | None:
        """Resolve or create the recording group directory for this job."""
        recording_dir = config.get("recording_group_dir")
        if recording_dir:
            abs_path = os.path.join(self.storage_path, recording_dir)
            if os.path.isdir(abs_path):
                return abs_path
            if os.path.isabs(recording_dir) and os.path.isdir(recording_dir):
                return recording_dir

        logger.warning("TTT_JOBS: No recording group dir found for job %s", job["id"])
        return None

    async def _wait_for_combined(
        self, group_dir: str, timeout: int = 3600
    ) -> str | None:
        """Wait for combined.mp4 to appear in the group directory."""
        from ..utils.paths import get_combined_video_path

        combined_path = get_combined_video_path(group_dir, self.storage_path)

        elapsed = 0
        while elapsed < timeout:
            if os.path.isfile(combined_path):
                return combined_path
            await asyncio.sleep(10)
            elapsed += 10

        return None

    async def _trim_video(
        self,
        group_dir: str,
        combined_path: str,
        trim_start: str,
        trim_end: str | None,
    ) -> str | None:
        """Trim the combined video using FFmpeg."""
        from ..utils.ffmpeg_utils import trim_video

        output_path = os.path.join(group_dir, "trimmed.mp4")
        success = await trim_video(combined_path, output_path, trim_start, trim_end)
        return output_path if success else None

    async def _upload_video(
        self, group_dir: str, video_path: str, config: dict
    ) -> str | None:
        """Upload video to YouTube via the upload processor."""
        if not self.upload_processor or not self.config.youtube.enabled:
            logger.info("TTT_JOBS: YouTube uploads not enabled, skipping")
            return None

        from .tasks.upload.youtube_upload_task import YoutubeUploadTask

        relative_dir = os.path.relpath(group_dir, self.storage_path)
        task = YoutubeUploadTask(group_dir=relative_dir)
        await self.upload_processor.add_work(task)

        logger.info("TTT_JOBS: Queued YouTube upload for %s", relative_dir)
        return None
