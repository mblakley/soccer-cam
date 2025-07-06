import os
import asyncio
import logging

from video_grouper.utils.config import Config
from video_grouper.task_processors import (
    StateAuditor,
    CameraPoller,
    DownloadProcessor,
    VideoProcessor,
    UploadProcessor,
    NtfyProcessor,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d - %(message)s",
)
logger = logging.getLogger(__name__)

DEFAULT_STORAGE_PATH = "./shared_data"


def create_directory(path):
    """Create a directory if it doesn't exist."""
    os.makedirs(path, exist_ok=True)


class VideoGrouperApp:
    """
    Refactored VideoGrouperApp that orchestrates task processors.
    Each task processor is self-contained and manages its own queue and state.
    """

    def __init__(self, config: Config, camera=None):
        """
        Initialize the VideoGrouperApp with task processors.

        Args:
            config: Configuration object
            camera: Camera object (optional, will be created if not provided)
        """
        self.config = config
        self.storage_path = os.path.abspath(config.storage.path)
        logger.info(f"Using storage path: {self.storage_path}")

        # Initialize camera
        if camera:
            self.camera = camera
        else:
            camera_type = config.camera.type
            if camera_type == "dahua":
                from video_grouper.cameras.dahua import DahuaCamera

                logger.info(
                    f"Initializing {camera_type} camera with IP: {config.camera.device_ip}"
                )
                self.camera = DahuaCamera(
                    config=config.camera, storage_path=self.storage_path
                )
            else:
                raise ValueError(f"Unsupported camera type: {camera_type}")

        # Get poll interval from config
        self.poll_interval = config.app.check_interval_seconds

        # Instantiate processors in dependency order
        self.upload_processor = UploadProcessor(
            storage_path=self.storage_path, config=self.config
        )
        self.video_processor = VideoProcessor(
            storage_path=self.storage_path,
            config=self.config,
            upload_processor=self.upload_processor,
        )
        self.download_processor = DownloadProcessor(
            storage_path=self.storage_path,
            config=self.config,
            camera=self.camera,
            video_processor=self.video_processor,
        )
        self.ntfy_processor = None
        if self.config.ntfy.enabled:
            from video_grouper.task_processors.services.ntfy_service import NtfyService

            ntfy_service = NtfyService(self.config.ntfy, self.storage_path)
            self.ntfy_processor = NtfyProcessor(
                storage_path=self.storage_path,
                config=self.config,
                ntfy_service=ntfy_service,
                poll_interval=30,
                video_processor=self.video_processor,
            )
        self.state_auditor = StateAuditor(
            storage_path=self.storage_path,
            config=self.config,
            download_processor=self.download_processor,
            video_processor=self.video_processor,
            upload_processor=self.upload_processor,
            poll_interval=self.poll_interval,
            ntfy_processor=self.ntfy_processor,
        )
        self.camera_poller = CameraPoller(
            storage_path=self.storage_path,
            config=self.config,
            camera=self.camera,
            download_processor=self.download_processor,
            poll_interval=self.poll_interval,
        )

        self.processors = [
            self.state_auditor,
            self.camera_poller,
            self.download_processor,
            self.video_processor,
            self.upload_processor,
        ]
        if self.ntfy_processor:
            self.processors.append(self.ntfy_processor)

        self._shutdown_event = asyncio.Event()
        logger.info("VideoGrouperApp initialized with task processors")

    async def initialize(self):
        """Initialize the application by setting up storage and processors."""
        logger.info("Initializing VideoGrouperApp")
        create_directory(self.storage_path)

        # Initialize all processors
        for processor in self.processors:
            await processor.start()

        logger.info("VideoGrouperApp initialization complete")

    async def run(self):
        """Run the application."""
        logger.info("Running VideoGrouperApp")
        await self.initialize()

        # All processors are already running their own loops
        # Just wait for shutdown event
        try:
            await self._shutdown_event.wait()
        except KeyboardInterrupt:
            logger.info("Received shutdown signal")
        finally:
            await self.shutdown()

    async def shutdown(self):
        """Shut down the application."""
        logger.info("Shutting down VideoGrouperApp")

        # Signal shutdown to wake up the run() method if it's waiting
        self._shutdown_event.set()

        # Stop all processors
        for processor in self.processors:
            await processor.stop()

        # Close camera connection if open
        if self.camera:
            await self.camera.close()

        logger.info("VideoGrouperApp shutdown complete")

    # Convenience methods for external access to processors

    async def add_download_task(self, recording_file):
        """Add a task to the download queue."""
        await self.download_processor.add_work(recording_file)

    async def add_video_task(self, ffmpeg_task):
        """Add a task to the video processing queue."""
        await self.video_processor.add_work(ffmpeg_task)

    async def add_youtube_task(self, youtube_task):
        """Add a task to the YouTube upload queue."""
        await self.upload_processor.add_work(youtube_task)

    def get_queue_sizes(self):
        """Get the current queue sizes for monitoring."""
        return {
            "download": self.download_processor.get_queue_size(),
            "video": self.video_processor.get_queue_size(),
            "youtube": self.upload_processor.get_queue_size(),
            "ntfy": self.ntfy_processor.get_queue_size() if self.ntfy_processor else -1,
        }

    def get_processor_status(self):
        """Get status of all processors."""
        return {
            "state_auditor": "running"
            if self.state_auditor._processor_task
            and not self.state_auditor._processor_task.done()
            else "stopped",
            "camera_poller": "running"
            if self.camera_poller._processor_task
            and not self.camera_poller._processor_task.done()
            else "stopped",
            "download_processor": "running"
            if self.download_processor._processor_task
            and not self.download_processor._processor_task.done()
            else "stopped",
            "video_processor": "running"
            if self.video_processor._processor_task
            and not self.video_processor._processor_task.done()
            else "stopped",
            "upload_processor": "running"
            if self.upload_processor._processor_task
            and not self.upload_processor._processor_task.done()
            else "stopped",
            "ntfy_processor": "running"
            if self.ntfy_processor
            and self.ntfy_processor._processor_task
            and not self.ntfy_processor._processor_task.done()
            else "stopped",
        }
