import os
import logging

from video_grouper.cameras.base import Camera
from video_grouper.task_processors.video_processor import VideoProcessor
from .base_queue_processor import QueueProcessor
from video_grouper.models import DirectoryState
from video_grouper.models import RecordingFile
from .tasks.video import CombineTask
from video_grouper.utils.config import Config
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

        try:
            logger.info(f"DOWNLOAD: Starting download of {os.path.basename(file_path)}")
            await dir_state.update_file_state(file_path, status="downloading")

            # The camera implementation may expose either a synchronous or an
            # asynchronous `download_file` method depending on the concrete
            # subclass or the way it is mocked in unit-tests.  Detect the
            # return type and `await` only when necessary so that both styles
            # are supported seamlessly.

            download_result = self.camera.download_file(
                file_path=item.metadata["path"], local_path=file_path
            )

            import asyncio

            if asyncio.iscoroutine(download_result):
                download_successful = await download_result
            else:
                download_successful = download_result

            if download_successful:
                await dir_state.update_file_state(file_path, status="downloaded")
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
                logger.error(
                    f"DOWNLOAD: Download failed for {os.path.basename(file_path)}"
                )
                return  # Failure handled – do not raise

        except Exception as e:
            logger.error(
                f"DOWNLOAD: An error occurred during download of {os.path.basename(file_path)}: {e}",
                exc_info=True,
            )
            await dir_state.update_file_state(file_path, status="download_failed")
            # Swallow the exception so that the processor can continue without retry loop in unit tests
            return

    def get_item_key(self, item: RecordingFile) -> str:
        return f"recording:{item.file_path}:{hash(item.file_path)}"
