"""TTT processing job processor -- polls TTT for pending jobs and executes them."""

import asyncio
import logging
import os
import platform
from typing import Optional

from .base_polling_processor import PollingProcessor
from ..utils.config import Config

logger = logging.getLogger(__name__)


class TTTJobProcessor(PollingProcessor):
    """Polls TTT API for processing jobs and executes them via existing pipeline.

    Flow per job:
    1. Poll TTT for pending jobs
    2. Claim job
    3. Drive through pipeline: download -> combine -> trim -> upload
    4. Report progress at each stage
    5. Report completion with YouTube URL
    """

    def __init__(
        self,
        storage_path: str,
        config: Config,
        ttt_client,
        camera=None,
        download_processor=None,
        video_processor=None,
        upload_processor=None,
        poll_interval: int = 30,
    ):
        super().__init__(storage_path, config, poll_interval)
        self.ttt_client = ttt_client
        self.camera = camera
        self.download_processor = download_processor
        self.video_processor = video_processor
        self.upload_processor = upload_processor
        self._processing_jobs: set[str] = set()
        self._service_id: Optional[str] = None
        self._heartbeat_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Start the processor and register service with TTT."""
        await self._register_service()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        await super().start()

    async def stop(self) -> None:
        """Stop the processor and cancel heartbeat."""
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        await super().stop()

    async def _register_service(self) -> None:
        """Register this service instance with TTT."""
        if not self.ttt_client.is_authenticated():
            logger.warning(
                "TTT client not authenticated, skipping service registration"
            )
            return
        try:
            machine_name = self.config.ttt.machine_name or platform.node()
            capabilities = {
                "ffmpeg": True,
                "autocam": self.config.autocam.enabled,
                "camera_type": self.config.camera.type,
                "camera_ip": self.config.camera.device_ip,
            }
            result = await asyncio.to_thread(
                self.ttt_client.register_service, machine_name, capabilities
            )
            self._service_id = result.get("id") if result else None
            logger.info(
                "TTT_JOBS: Registered service as '%s' (id=%s)",
                machine_name,
                self._service_id,
            )
        except Exception as e:
            logger.error("TTT_JOBS: Failed to register service: %s", e)

    async def _heartbeat_loop(self) -> None:
        """Send periodic heartbeat to TTT."""
        while True:
            try:
                await asyncio.sleep(60)
                if self._service_id and self.ttt_client.is_authenticated():
                    await asyncio.to_thread(
                        self.ttt_client.send_heartbeat, self._service_id, "online"
                    )
                    logger.debug("TTT_JOBS: Heartbeat sent")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("TTT_JOBS: Heartbeat failed: %s", e)

    async def discover_work(self) -> None:
        """Poll TTT for pending processing jobs."""
        if not self.ttt_client.is_authenticated():
            logger.debug("TTT_JOBS: Client not authenticated, skipping poll")
            return

        try:
            jobs = await asyncio.to_thread(self.ttt_client.get_pending_jobs)
        except Exception as e:
            logger.error("TTT_JOBS: Failed to poll for jobs: %s", e)
            return

        if not jobs:
            return

        for job in jobs:
            job_id = job.get("id")
            if not job_id or job_id in self._processing_jobs:
                continue

            self._processing_jobs.add(job_id)
            asyncio.create_task(self._process_job(job))

    async def _process_job(self, job: dict) -> None:
        """Process a single TTT job through the pipeline."""
        job_id = job["id"]
        try:
            # Claim the job
            try:
                await asyncio.to_thread(self.ttt_client.claim_job, job_id)
                logger.info("TTT_JOBS: Claimed job %s", job_id)
            except Exception as e:
                logger.error("TTT_JOBS: Failed to claim job %s: %s", job_id, e)
                return

            config = job.get("config", {})

            # Step 1: Report downloading status
            await self._update_progress(
                job_id,
                "downloading",
                {"percent": 0, "message": "Starting download from camera"},
            )

            group_dir = await self._resolve_or_create_group(job, config)
            if not group_dir:
                await self._fail_job(
                    job_id, "Could not resolve recording group directory"
                )
                return

            # Step 2: Wait for combine (or trigger it)
            await self._update_progress(
                job_id,
                "combining",
                {"percent": 30, "message": "Combining video files"},
            )
            combined_path = await self._wait_for_combined(group_dir)
            if not combined_path:
                await self._fail_job(job_id, "Combined video not found")
                return

            # Step 3: Trim if config specifies trim times
            trim_start = config.get("trim_start")
            trim_end = config.get("trim_end")
            if trim_start is not None:
                await self._update_progress(
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
                job_id,
                "uploading",
                {"percent": 70, "message": "Uploading to YouTube"},
            )
            youtube_url = await self._upload_video(group_dir, upload_path, config)

            # Step 5: Complete
            result = {"youtube_url": youtube_url} if youtube_url else {}
            await asyncio.to_thread(self.ttt_client.complete_job, job_id, result)
            logger.info("TTT_JOBS: Completed job %s", job_id)

        except Exception as e:
            logger.error(
                "TTT_JOBS: Error processing job %s: %s", job_id, e, exc_info=True
            )
            await self._fail_job(job_id, str(e))
        finally:
            self._processing_jobs.discard(job_id)

    async def _update_progress(self, job_id: str, status: str, progress: dict) -> None:
        """Update job progress in TTT."""
        try:
            await asyncio.to_thread(
                self.ttt_client.update_job_progress, job_id, status, progress
            )
        except Exception as e:
            logger.warning("TTT_JOBS: Failed to update progress for %s: %s", job_id, e)

    async def _fail_job(self, job_id: str, error: str) -> None:
        """Mark a job as failed in TTT."""
        try:
            await asyncio.to_thread(self.ttt_client.fail_job, job_id, error)
            logger.warning("TTT_JOBS: Job %s failed: %s", job_id, error)
        except Exception as e:
            logger.error("TTT_JOBS: Failed to report failure for %s: %s", job_id, e)

    async def _resolve_or_create_group(self, job: dict, config: dict) -> Optional[str]:
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
    ) -> Optional[str]:
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
        trim_end: Optional[str],
    ) -> Optional[str]:
        """Trim the combined video using FFmpeg."""
        from ..utils.ffmpeg_utils import trim_video

        output_path = os.path.join(group_dir, "trimmed.mp4")
        success = await trim_video(combined_path, output_path, trim_start, trim_end)
        return output_path if success else None

    async def _upload_video(
        self, group_dir: str, video_path: str, config: dict
    ) -> Optional[str]:
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
