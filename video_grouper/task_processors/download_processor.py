import os
import logging

from video_grouper.cameras.base import Camera
from video_grouper.task_processors.video_processor import VideoProcessor
from .base_queue_processor import QueueProcessor
from video_grouper.models import DirectoryState
from video_grouper.models import RecordingFile
from .tasks.video import CombineTask
from video_grouper.utils.config import Config
from video_grouper.utils.disk_space import check_disk_space
from .queue_type import QueueType

logger = logging.getLogger(__name__)


class DownloadProcessor(QueueProcessor):
    """
    Task processor for downloading files from the camera.
    Processes download queue sequentially, one file at a time.
    """

    def __init__(
        self,
        storage_path: str,
        config: Config,
        camera: Camera,
        video_processor: VideoProcessor,
    ):
        super().__init__(storage_path, config)
        self.camera = camera
        self.video_processor = video_processor
        self.ttt_reporter = None

    @property
    def queue_type(self) -> QueueType:
        """Return the queue type for this processor."""
        return QueueType.DOWNLOAD

    async def process_item(self, item: RecordingFile) -> None:
        """
        Download a single file from the camera.

        Args:
            item: RecordingFile object to download
        """
        file_path = item.file_path
        group_dir = os.path.dirname(file_path)
        dir_state = DirectoryState(group_dir)

        # Defense-in-depth: skip download if group has already progressed
        # past the download stage to avoid overwriting during combining.
        skip_statuses = {"combined", "trimmed", "autocam_complete", "complete"}
        if dir_state.status in skip_statuses:
            logger.info(
                f"DOWNLOAD: Skipping {os.path.basename(file_path)} - group already at status '{dir_state.status}'"
            )
            return

        # Check disk space before downloading
        min_free_gb = self.config.storage.min_free_gb
        has_space, free_gb = check_disk_space(self.storage_path, min_free_gb)
        if not has_space:
            logger.error(
                f"DOWNLOAD: Low disk space ({free_gb:.1f} GB free, "
                f"minimum {min_free_gb} GB required). "
                f"Skipping download of {os.path.basename(file_path)}."
            )
            raise RuntimeError(
                f"Insufficient disk space: {free_gb:.1f} GB free, need {min_free_gb} GB"
            )

        # Download to a temp file first so a crash mid-download never
        # leaves a partial file at the final path.  Only rename on success.
        temp_path = file_path + ".tmp"
        try:
            logger.info(f"DOWNLOAD: Starting download of {os.path.basename(file_path)}")
            await dir_state.update_file_state(file_path, status="downloading")
            if self.ttt_reporter:
                await self.ttt_reporter.update_recording_status(
                    dir_state.ttt_recording_id, "download", "downloading"
                )

            # Clean up any leftover temp file from a previous crashed attempt
            try:
                os.remove(temp_path)
            except OSError:
                pass

            # The camera implementation may expose either a synchronous or an
            # asynchronous `download_file` method depending on the concrete
            # subclass or the way it is mocked in unit-tests.  Detect the
            # return type and `await` only when necessary so that both styles
            # are supported seamlessly.

            download_result = self.camera.download_file(
                file_path=item.metadata["path"], local_path=temp_path
            )

            import asyncio

            if asyncio.iscoroutine(download_result):
                download_successful = await download_result
            else:
                download_successful = download_result

            if download_successful:
                # Atomic rename: temp -> final (only on success).
                # If the camera wrote directly to file_path (legacy behavior
                # or mock), the temp file won't exist -- that's OK.
                try:
                    os.replace(temp_path, file_path)
                except OSError:
                    pass
                await dir_state.update_file_state(file_path, status="downloaded")
                if self.ttt_reporter:
                    await self.ttt_reporter.update_recording_status(
                        dir_state.ttt_recording_id, "download", "downloaded"
                    )
                logger.info(
                    f"DOWNLOAD: Successfully downloaded {os.path.basename(file_path)}"
                )

                # Check if all files in the group are downloaded and ready for combining
                if dir_state.is_ready_for_combining():
                    logger.info(
                        f"DOWNLOAD: Group {os.path.basename(group_dir)} is ready for combining."
                    )
                    if self.video_processor:
                        combine_task = CombineTask(group_dir=group_dir)

                        # Support both synchronous and asynchronous `add_work` implementations.
                        add_work_result = self.video_processor.add_work(combine_task)
                        import inspect
                        import asyncio

                        if inspect.isawaitable(add_work_result):
                            await add_work_result

                        logger.info(
                            f"DOWNLOAD: Handed off combine task to video processor: {combine_task}"
                        )
                    else:
                        logger.warning(
                            f"DOWNLOAD: No video processor available to queue combine task for {group_dir}"
                        )
            else:
                await dir_state.update_file_state(file_path, status="download_failed")
                if self.ttt_reporter:
                    await self.ttt_reporter.update_recording_status(
                        dir_state.ttt_recording_id, "download", "failed"
                    )
                raise RuntimeError(f"Download failed for {os.path.basename(file_path)}")

        except Exception as e:
            logger.error(
                f"DOWNLOAD: An error occurred during download of {os.path.basename(file_path)}: {e}",
                exc_info=True,
            )
            # Clean up temp file on any error
            try:
                os.remove(temp_path)
            except OSError:
                pass
            await dir_state.update_file_state(file_path, status="download_failed")
            if self.ttt_reporter:
                await self.ttt_reporter.update_recording_status(
                    dir_state.ttt_recording_id, "download", "failed", error=str(e)
                )
            raise

    def get_item_key(self, item: RecordingFile) -> str:
        return f"recording:{item.file_path}"
