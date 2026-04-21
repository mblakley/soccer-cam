import os
import asyncio
from pathlib import Path

from video_grouper.api_integrations.ntfy_response import create_ntfy_response_service
from video_grouper.api_integrations.ttt_reporter import TTTReporter
from video_grouper.api_integrations.command_executor import CommandExecutor
from video_grouper.utils.config import Config
from video_grouper.utils.error_tracker import ErrorTracker
from video_grouper.utils.logger import setup_logging_from_config, get_logger
from video_grouper.task_processors import (
    StateAuditor,
    CameraPoller,
    DownloadProcessor,
    VideoProcessor,
    UploadProcessor,
    NtfyProcessor,
    ClipProcessor,
    ClipDiscoveryProcessor,
)
from video_grouper.task_processors.register_tasks import register_all_tasks

# Configure logging will be done after config is loaded
logger = get_logger(__name__)


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
            camera: Camera object or dict of {name: Camera} (optional, will be created if not provided)
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

        # Shared error tracker — used by all processors and the TTT reporter
        self.error_tracker = ErrorTracker(max_errors=100)

        # Validate storage path is usable
        self._validate_storage_path()

        # Get poll interval from config
        self.poll_interval = config.app.check_interval_seconds

        # Instantiate shared processors in dependency order
        self.upload_processor = UploadProcessor(
            storage_path=self.storage_path, config=self.config
        )
        self.video_processor = VideoProcessor(
            storage_path=self.storage_path,
            config=self.config,
            upload_processor=self.upload_processor,
        )

        # AutoCam processors run in the TRAY APP (needs desktop for GUI automation),
        # not in the service. See video_grouper/tray/main.py SystemTrayIcon.__init__.

        # Initialize per-camera processors
        self.cameras: dict = {}
        self.download_processors: dict = {}
        self.camera_pollers: dict = {}

        if isinstance(camera, dict):
            # Dict of {name: Camera} provided
            provided_cameras = camera
        elif camera is not None:
            # Single camera provided (backward compat for tests)
            provided_cameras = {config.camera.name: camera}
        else:
            provided_cameras = {}

        for cam_config in config.cameras:
            cam_name = cam_config.name

            if not cam_config.enabled:
                logger.info("Camera %s is disabled, skipping", cam_name)
                continue

            if cam_name in provided_cameras:
                cam = provided_cameras[cam_name]
            else:
                cam = self._create_camera(cam_config, self.storage_path)

            self.cameras[cam_name] = cam

            dl_proc = DownloadProcessor(
                storage_path=self.storage_path,
                config=self.config,
                camera=cam,
                video_processor=self.video_processor,
            )
            self.download_processors[cam_name] = dl_proc

            poller = CameraPoller(
                storage_path=self.storage_path,
                config=self.config,
                camera=cam,
                download_processor=dl_proc,
                poll_interval=self.poll_interval,
            )
            self.camera_pollers[cam_name] = poller

        # Backward compat: expose first camera's processors as singular attributes
        if self.cameras:
            first_name = next(iter(self.cameras))
            self._first_camera_name = first_name
        else:
            self._first_camera_name = None

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
                logger.error(f"PlayMetricsService failed to initialize: {e}")
                logger.warning("Continuing without PlayMetrics integration")
                playmetrics_service = create_playmetrics_service(
                    type("_Cfg", (), {"enabled": False, "teams": []})()
                )

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

            # Wire event-driven transitions into VideoProcessor
            self.video_processor.match_info_service = match_info_service
            self.video_processor.ntfy_processor = self.ntfy_processor

            # Wire ntfy_service into UploadProcessor for auth failure notifications
            # and playlist name requests
            self.upload_processor.ntfy_service = ntfy_service

            # Wire ntfy_service into all CameraPollers for unplug notifications
            for poller in self.camera_pollers.values():
                poller.ntfy_service = ntfy_service

        # TTT Clip Request Processor (optional)
        self.clip_request_processor = None
        if self.config.ttt.enabled:
            try:
                from video_grouper.task_processors.clip_request_processor import (
                    ClipRequestProcessor,
                )
                from video_grouper.api_integrations.ttt_api import TTTApiClient
                from video_grouper.utils.google_drive_upload import GoogleDriveUploader

                ttt_client = TTTApiClient(
                    supabase_url=self.config.ttt.supabase_url,
                    anon_key=self.config.ttt.anon_key,
                    api_base_url=self.config.ttt.api_base_url,
                    storage_path=self.storage_path,
                )
                # Login with stored credentials
                if self.config.ttt.email and self.config.ttt.password:
                    try:
                        ttt_client.login(
                            self.config.ttt.email, self.config.ttt.password
                        )
                        logger.info("TTT API client authenticated")
                    except Exception as e:
                        logger.error(f"TTT login failed: {e}")

                # Initialize plugin manager
                try:
                    from video_grouper.plugins.plugin_manager import PluginManager

                    self.plugin_manager = PluginManager(
                        ttt_client=ttt_client,
                        storage_path=Path(self.storage_path),
                        public_keys=self.config.ttt.plugin_signing_public_keys,
                        refresh_headroom_days=self.config.ttt.plugin_refresh_headroom_days,
                    )
                    self.plugin_manager.sync_plugins()
                    self.plugin_manager.load_plugins()
                    logger.info(
                        "Plugin manager initialized, %d plugins loaded",
                        len(self.plugin_manager.get_loaded_plugins()),
                    )
                except Exception:
                    logger.warning("Failed to initialize plugin manager", exc_info=True)

                drive_uploader = GoogleDriveUploader(self.storage_path)

                # YouTube uploader for delivery_method='youtube' clip requests.
                # Reuses the camera manager's existing YouTube OAuth tokens.
                youtube_uploader = None
                try:
                    from video_grouper.utils.youtube_upload import (
                        YouTubeUploader,
                        get_youtube_paths,
                    )

                    credentials_file, token_file = get_youtube_paths(self.storage_path)
                    if os.path.exists(token_file):
                        youtube_uploader = YouTubeUploader(credentials_file, token_file)
                        logger.info(
                            "TTT clip requests: YouTube uploader initialized (camera manager's channel)"
                        )
                    else:
                        logger.info(
                            "TTT clip requests: YouTube token not present; youtube delivery will be skipped until the manager authenticates"
                        )
                except Exception:
                    logger.warning(
                        "Failed to initialize YouTube uploader for clip requests",
                        exc_info=True,
                    )

                # Get ntfy_service from ntfy_processor if available
                ntfy_service = None
                if self.ntfy_processor and hasattr(self.ntfy_processor, "ntfy_service"):
                    ntfy_service = self.ntfy_processor.ntfy_service

                self.clip_request_processor = ClipRequestProcessor(
                    storage_path=self.storage_path,
                    config=self.config,
                    ttt_client=ttt_client,
                    drive_uploader=drive_uploader,
                    ntfy_service=ntfy_service,
                    youtube_uploader=youtube_uploader,
                    poll_interval=self.config.ttt.clip_request_poll_interval,
                )
                logger.info("TTT ClipRequestProcessor initialized")
            except Exception as e:
                logger.error(f"Failed to initialize TTT ClipRequestProcessor: {e}")

        # Moment tagging — clip generation and highlight compilation
        self.clip_processor = None
        self.clip_discovery_processor = None
        self._moment_api_client = None

        if self.config.moment_tagging.enabled:
            from video_grouper.api_integrations.moment_api_client import MomentApiClient

            logger.info("Moment tagging enabled -- initializing clip processors")
            self._moment_api_client = MomentApiClient(
                api_base_url=self.config.moment_tagging.api_base_url,
                service_role_key=self.config.moment_tagging.service_role_key,
            )

            # Reuse existing YouTube uploader if available
            youtube_uploader = None
            if self.config.youtube.enabled:
                try:
                    from video_grouper.utils.youtube_upload import (
                        YouTubeUploader,
                        get_youtube_paths,
                    )

                    creds_file, token_file = get_youtube_paths(self.storage_path)
                    youtube_uploader = YouTubeUploader(creds_file, token_file)
                except Exception as e:
                    logger.warning(
                        "Could not initialize YouTube uploader for clips: %s", e
                    )

            self.clip_processor = ClipProcessor(
                storage_path=self.storage_path,
                config=self.config,
                api_client=self._moment_api_client,
                youtube_uploader=youtube_uploader,
            )
            self.clip_discovery_processor = ClipDiscoveryProcessor(
                storage_path=self.storage_path,
                config=self.config,
                api_client=self._moment_api_client,
                clip_processor=self.clip_processor,
                poll_interval=self.poll_interval,
            )

        # TTT Job Processor (optional)
        self.ttt_job_processor = None
        if self.config.ttt.enabled and self.config.ttt.job_polling_enabled:
            try:
                ttt_client = None
                if self.clip_request_processor:
                    ttt_client = self.clip_request_processor.ttt_client

                if not ttt_client:
                    from video_grouper.api_integrations.ttt_api import TTTApiClient

                    ttt_client = TTTApiClient(
                        supabase_url=self.config.ttt.supabase_url,
                        anon_key=self.config.ttt.anon_key,
                        api_base_url=self.config.ttt.api_base_url,
                        storage_path=self.storage_path,
                    )
                    if self.config.ttt.email and self.config.ttt.password:
                        try:
                            ttt_client.login(
                                self.config.ttt.email, self.config.ttt.password
                            )
                        except Exception as e:
                            logger.error(f"TTT login failed for job processor: {e}")

                from video_grouper.task_processors.ttt_job_processor import (
                    TTTJobProcessor,
                )

                self.ttt_job_processor = TTTJobProcessor(
                    storage_path=self.storage_path,
                    config=self.config,
                    ttt_client=ttt_client,
                    camera=self.camera,
                    download_processor=self.download_processor,
                    video_processor=self.video_processor,
                    upload_processor=self.upload_processor,
                    poll_interval=self.config.ttt.job_poll_interval,
                )
                logger.info("TTT JobProcessor initialized")
            except Exception as e:
                logger.error(f"Failed to initialize TTT JobProcessor: {e}")

        # TTT Reporter — optional, best-effort status reporting back to TTT
        # Works independently of the clip request processor: enabled when TTT
        # credentials are present, regardless of the full TTT enabled flag.
        ttt_reporter_client = None
        if self.config.ttt.enabled and self.clip_request_processor is not None:
            # Re-use the client that was created for the clip request processor
            try:
                ttt_reporter_client = self.clip_request_processor.ttt_client
            except AttributeError:
                pass
        # Load machine ID for multi-computer awareness
        machine_id = None
        if self.config.ttt.enabled:
            try:
                from video_grouper.utils.machine_id import get_or_create_machine_id

                machine_id = get_or_create_machine_id(self.storage_path)
            except Exception:
                pass

        command_executor = CommandExecutor(self)
        self.ttt_reporter = TTTReporter(
            ttt_client=ttt_reporter_client,
            config=self.config,
            error_tracker=self.error_tracker,
            command_executor=command_executor,
            machine_id=machine_id,
        )

        # Wire ttt_reporter into all processors for best-effort pipeline reporting
        for poller in self.camera_pollers.values():
            poller.ttt_reporter = self.ttt_reporter
        for dl_proc in self.download_processors.values():
            dl_proc.ttt_reporter = self.ttt_reporter
            dl_proc.error_tracker = self.error_tracker
        self.video_processor.ttt_reporter = self.ttt_reporter
        self.upload_processor.ttt_reporter = self.ttt_reporter

        self.state_auditor = StateAuditor(
            storage_path=self.storage_path,
            config=self.config,
            download_processors=self.download_processors,
            video_processor=self.video_processor,
            poll_interval=self.poll_interval,
            ntfy_processor=self.ntfy_processor,
        )

        # Queue processors must start (and load_state) BEFORE StateAuditor
        # runs discover_work(), otherwise duplicate items get queued.
        self.processors = list(self.download_processors.values())
        self.processors.extend(
            [
                self.video_processor,
                self.upload_processor,
            ]
        )
        if self.ntfy_processor:
            self.processors.append(self.ntfy_processor)
        if self.clip_request_processor:
            self.processors.append(self.clip_request_processor)
        if self.clip_processor:
            self.processors.append(self.clip_processor)
        if self.clip_discovery_processor:
            self.processors.append(self.clip_discovery_processor)
        if self.ttt_job_processor:
            self.processors.append(self.ttt_job_processor)
        # AutoCam processors run in the tray app, not the service
        # Polling processors last -- StateAuditor discover_work() must see
        # already-loaded queues to avoid duplicate enqueues.
        self.processors.extend(self.camera_pollers.values())
        self.processors.append(self.state_auditor)

        self._shutdown_event = asyncio.Event()

        # Register all task types with the task registry
        register_all_tasks()

        logger.info("VideoGrouperApp initialized with task processors")

    def _validate_storage_path(self):
        """Validate that the storage path is usable.

        Creates the directory if needed, checks write permissions, and
        warns about low disk space.  Raises on fatal errors so the app
        fails early with a clear message.
        """
        from video_grouper.utils.disk_space import check_disk_space

        try:
            os.makedirs(self.storage_path, exist_ok=True)
        except OSError as exc:
            raise RuntimeError(
                f"Cannot create storage directory '{self.storage_path}': {exc}"
            ) from exc

        # Quick write test
        test_file = os.path.join(self.storage_path, ".write_test")
        try:
            with open(test_file, "w") as f:
                f.write("ok")
            os.remove(test_file)
        except OSError as exc:
            raise RuntimeError(
                f"Storage directory '{self.storage_path}' is not writable: {exc}"
            ) from exc

        # Disk space warning (non-fatal)
        min_free_gb = self.config.storage.min_free_gb
        has_space, free_gb = check_disk_space(self.storage_path, min_free_gb)
        if not has_space:
            logger.warning(
                f"Low disk space on storage path: {free_gb:.1f} GB free "
                f"(minimum {min_free_gb} GB recommended)"
            )
        else:
            logger.info(f"Storage path OK: {free_gb:.1f} GB free")

    @staticmethod
    def _create_camera(cam_config, storage_path):
        """Create a Camera instance from a CameraConfig.

        Uses the camera registry so that new camera types can be added
        by implementing the Camera ABC and calling ``register_camera()``.
        See docs/ADDING_A_CAMERA.md for details.
        """
        # Ensure built-in camera modules are imported (triggers registration)
        import video_grouper.cameras.dahua  # noqa: F401
        import video_grouper.cameras.reolink  # noqa: F401

        from video_grouper.cameras import create_camera

        logger.info(
            f"Initializing {cam_config.type} camera '{cam_config.name}' "
            f"with IP: {cam_config.device_ip}"
        )
        return create_camera(cam_config, storage_path)

    @property
    def camera(self):
        """Backward compat: return the first camera."""
        if self._first_camera_name:
            return self.cameras[self._first_camera_name]
        return None

    @property
    def download_processor(self):
        """Backward compat: return the first download processor."""
        if self._first_camera_name:
            return self.download_processors[self._first_camera_name]
        return None

    @property
    def camera_poller(self):
        """Backward compat: return the first camera poller."""
        if self._first_camera_name:
            return self.camera_pollers[self._first_camera_name]
        return None

    async def initialize(self):
        """Initialize the application by setting up storage and processors."""
        logger.info("Initializing VideoGrouperApp")
        os.makedirs(self.storage_path, exist_ok=True)

        # Initialize all processors
        for processor in self.processors:
            await processor.start()

        # Start optional TTT status reporter (best-effort, never blocks startup)
        await self.ttt_reporter.start()

        logger.info("VideoGrouperApp initialization complete")

    async def run(self):
        """Run the application."""
        logger.info("Running VideoGrouperApp")
        await self.initialize()

        # Start NTFY response service when NTFY is enabled
        ntfy_response_service = None
        if self.config.ntfy.enabled:
            try:
                ntfy_response_service = create_ntfy_response_service(self.config.ntfy)
                await ntfy_response_service.start()
                logger.info("NTFY response service started in VideoGrouperApp")
            except Exception as e:
                logger.error(f"Failed to start NTFY response service: {e}")
        else:
            logger.info("NTFY response service disabled in configuration")

        # Start periodic status reporting
        status_task = asyncio.create_task(self._periodic_status_report())

        # Start the headless TTT auth web server when enabled
        auth_server = None
        auth_task = None
        if self.config.ttt.auth_server_enabled:
            try:
                from video_grouper.web.auth_server import create_app
                import uvicorn

                def _auth_status_provider() -> dict:
                    # get_queue_sizes() reports -1 for processors that aren't
                    # enabled in this config (ntfy/clip_request/ttt_jobs gates).
                    # Filter those out so the dashboard only shows live ones.
                    queue_sizes = {
                        k: v for k, v in self.get_queue_sizes().items() if v >= 0
                    }
                    return {
                        "queue_sizes": queue_sizes,
                        "cameras": [
                            {
                                "name": n,
                                "ip": getattr(c, "device_ip", "?"),
                                "connected": getattr(c, "is_connected", None),
                            }
                            for n, c in self.cameras.items()
                            if c is not None
                        ],
                    }

                auth_app = create_app(
                    self.config.ttt,
                    self.storage_path,
                    status_provider=_auth_status_provider,
                )
                uv_config = uvicorn.Config(
                    auth_app,
                    host=self.config.ttt.auth_server_bind,
                    port=self.config.ttt.auth_server_port,
                    log_level="info",
                    access_log=False,
                )
                auth_server = uvicorn.Server(uv_config)
                auth_task = asyncio.create_task(auth_server.serve())
                logger.info(
                    "Headless TTT auth server listening on http://%s:%d",
                    self.config.ttt.auth_server_bind,
                    self.config.ttt.auth_server_port,
                )
            except Exception as e:
                logger.error(f"Failed to start headless TTT auth server: {e}")

        # All processors are already running their own loops
        # Just wait for shutdown event
        try:
            await self._shutdown_event.wait()
        finally:
            status_task.cancel()

            if auth_server is not None:
                auth_server.should_exit = True
                if auth_task is not None:
                    try:
                        await auth_task
                    except Exception as e:
                        logger.error(f"Error stopping auth server: {e}")

            # Stop NTFY response service if it was started
            if ntfy_response_service:
                try:
                    await ntfy_response_service.stop()
                    logger.info("NTFY response service stopped")
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

        # Stop TTT reporter (best-effort, no-op if not configured)
        await self.ttt_reporter.stop()

        # Stop all processors
        for processor in self.processors:
            await processor.stop()

        # Close all camera connections
        for cam in self.cameras.values():
            if cam:
                await cam.close()

        # Close moment API client if open
        if self._moment_api_client:
            await self._moment_api_client.close()

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
        sizes = {
            "download": sum(
                dl.get_queue_size() for dl in self.download_processors.values()
            ),
            "video": self.video_processor.get_queue_size(),
            "youtube": self.upload_processor.get_queue_size(),
            "ntfy": self.ntfy_processor.get_queue_size() if self.ntfy_processor else -1,
            "clip_request": len(self.clip_request_processor._processing)
            if self.clip_request_processor
            else -1,
            "ttt_jobs": len(self.ttt_job_processor._processing_jobs)
            if self.ttt_job_processor
            else -1,
        }
        if self.clip_processor:
            sizes["clips"] = self.clip_processor.get_queue_size()
        return sizes

    @staticmethod
    def _processor_status(processor) -> str:
        """Return 'running', 'stopped', or 'disabled' for a processor."""
        if processor is None:
            return "disabled"
        if processor._processor_task and not processor._processor_task.done():
            return "running"
        return "stopped"

    def get_processor_status(self):
        """Get status of all processors."""
        status = {
            "state_auditor": "startup_only",
            "camera_poller": self._processor_status(self.camera_poller),
            "download_processor": self._processor_status(self.download_processor),
            "video_processor": self._processor_status(self.video_processor),
            "upload_processor": self._processor_status(self.upload_processor),
            "ntfy_processor": self._processor_status(self.ntfy_processor),
            "clip_request_processor": self._processor_status(
                self.clip_request_processor
            ),
            "ttt_job_processor": self._processor_status(self.ttt_job_processor),
        }
        # Add per-camera statuses if multiple cameras
        if len(self.cameras) > 1:
            for name, poller in self.camera_pollers.items():
                status[f"camera_poller.{name}"] = self._processor_status(poller)
            for name, dl in self.download_processors.items():
                status[f"download_processor.{name}"] = self._processor_status(dl)
        if self.clip_processor is not None:
            status["clip_processor"] = self._processor_status(self.clip_processor)
            status["clip_discovery"] = self._processor_status(
                self.clip_discovery_processor
            )
        return status
