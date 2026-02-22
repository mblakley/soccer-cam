import os
import asyncio

from video_grouper.api_integrations.ntfy_response import create_ntfy_response_service
from video_grouper.utils.config import Config
from video_grouper.utils.logger import setup_logging_from_config, get_logger
from video_grouper.task_processors import (
    StateAuditor,
    CameraPoller,
    DownloadProcessor,
    VideoProcessor,
    UploadProcessor,
    NtfyProcessor,
)
from video_grouper.task_processors.register_tasks import register_all_tasks

# Configure logging will be done after config is loaded
logger = get_logger(__name__)

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
        # Setup logging from config
        setup_logging_from_config(config)

        # Initialize mock services if environment variables are set
        try:
            from video_grouper.task_processors.services.mock_services import (
                initialize_mock_services,
            )

            initialize_mock_services()
        except ImportError:
            pass  # Mock services not available, continue with real services

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
            elif camera_type == "simulator":
                from video_grouper.cameras.simulator import SimulatorCamera

                logger.info(f"Initializing {camera_type} camera for testing")
                self.camera = SimulatorCamera(
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
        self.camera_poller = CameraPoller(
            storage_path=self.storage_path,
            config=self.config,
            camera=self.camera,
            download_processor=self.download_processor,
            poll_interval=self.poll_interval,
        )
        self.ntfy_processor = None
        if self.config.ntfy.enabled:
            from video_grouper.task_processors.services.ntfy_service import NtfyService
            from video_grouper.task_processors.services.match_info_service import (
                MatchInfoService,
            )
            from video_grouper.task_processors.services.mock_services import (
                create_teamsnap_service,
                create_playmetrics_service,
            )

            # Create services first
            teamsnap_service = create_teamsnap_service(self.config.teamsnap)
            try:
                playmetrics_service = create_playmetrics_service(
                    self.config.playmetrics
                )
            except RuntimeError as e:
                logger.critical(f"PlayMetricsService failed to initialize: {e}")
                # Optionally, you can use sys.exit(1) to exit the app immediately
                import sys

                sys.exit(1)

            # Create NTFY processor first (without service)
            self.ntfy_processor = NtfyProcessor(
                storage_path=self.storage_path,
                config=self.config,
                ntfy_service=None,  # Will be set after creation
                match_info_service=None,  # Will be set after creation
                poll_interval=30,
                video_processor=self.video_processor,
            )

            # Create NTFY service with callback to processor
            ntfy_service = NtfyService(
                self.config.ntfy,
                self.storage_path,
                completion_callback=self.ntfy_processor._check_match_info_completion,
            )

            # Create match info service
            match_info_service = MatchInfoService(
                teamsnap_service=teamsnap_service,
                playmetrics_service=playmetrics_service,
                ntfy_service=ntfy_service,
            )

            # Set the services in the processor
            self.ntfy_processor.ntfy_service = ntfy_service
            self.ntfy_processor.match_info_service = match_info_service
        self.state_auditor = StateAuditor(
            storage_path=self.storage_path,
            config=self.config,
            download_processor=self.download_processor,
            video_processor=self.video_processor,
            poll_interval=self.poll_interval,
            ntfy_processor=self.ntfy_processor,
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

        # Register all task types with the task registry
        register_all_tasks()

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

        # Start NTFY response service if enabled
        ntfy_response_service = None
        if (
            hasattr(self.config.ntfy, "response_service")
            and self.config.ntfy.response_service
        ):
            try:
                ntfy_response_service = create_ntfy_response_service(self.config.ntfy)
                await ntfy_response_service.start()
                logger.info("✓ NTFY response service started in VideoGrouperApp")
            except Exception as e:
                logger.error(f"Failed to start NTFY response service: {e}")
        else:
            logger.info("NTFY response service disabled in configuration")

        # Start periodic status reporting
        status_task = asyncio.create_task(self._periodic_status_report())

        # All processors are already running their own loops
        # Just wait for shutdown event
        try:
            await self._shutdown_event.wait()
        finally:
            status_task.cancel()

            # Stop NTFY response service if it was started
            if ntfy_response_service:
                try:
                    await ntfy_response_service.stop()
                    logger.info("✓ NTFY response service stopped")
                except Exception as e:
                    logger.error(f"Error stopping NTFY response service: {e}")

            await self.shutdown()

    async def _periodic_status_report(self):
        """Report queue status every 5 minutes."""
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(300)  # 5 minutes
                if not self._shutdown_event.is_set():
                    self._log_queue_status()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in status report: {e}")

    def _log_queue_status(self):
        """Log current queue status for all processors."""
        status = self.get_queue_sizes()
        logger.info(f"QUEUE_STATUS: {status}")

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

        # Close all loggers to release file handles
        from video_grouper.utils.logger import close_loggers

        close_loggers()

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
            else "stopped"
            if self.ntfy_processor
            else "disabled",
        }
