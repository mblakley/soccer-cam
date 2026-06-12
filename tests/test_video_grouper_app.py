"""Integration tests for the refactored VideoGrouperApp."""

import os
import tempfile
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest

from video_grouper.models import RecordingFile
from video_grouper.task_processors.tasks.upload import YoutubeUploadTask
from video_grouper.task_processors.tasks.video import CombineTask
from video_grouper.utils.config import (
    AppConfig,
    AutocamConfig,
    CameraConfig,
    CloudSyncConfig,
    Config,
    LoggingConfig,
    NtfyConfig,
    PlayMetricsConfig,
    ProcessingConfig,
    RecordingConfig,
    StorageConfig,
    TeamSnapConfig,
    YouTubeConfig,
)
from video_grouper.utils.logger import close_loggers
from video_grouper.video_grouper_app import VideoGrouperApp


@pytest.fixture(autouse=True)
def cleanup_loggers():
    """Clean up loggers after each test to prevent file handle issues."""
    yield
    # Close all loggers to release file handles
    close_loggers()


@pytest.fixture
def temp_storage():
    """Create a temporary storage directory for testing."""
    import shutil
    import time

    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir
        # Add a small delay to allow file handles to be released
        time.sleep(0.1)
        # Force cleanup of any remaining files
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass


@pytest.fixture
def mock_config(temp_storage):
    """Create a mock configuration object."""
    return Config(
        cameras=[
            CameraConfig(
                name="default",
                type="dahua",
                device_ip="192.168.1.100",
                username="admin",
                password="password",
            )
        ],
        storage=StorageConfig(path=temp_storage),
        recording=RecordingConfig(),
        processing=ProcessingConfig(),
        logging=LoggingConfig(),
        app=AppConfig(storage_path=temp_storage, check_interval_seconds=1),
        teamsnap=TeamSnapConfig(enabled=False, team_id="1", my_team_name="Team A"),
        teamsnap_teams=[],
        playmetrics=PlayMetricsConfig(
            enabled=False, username="user", password="pass", team_name="Team A"
        ),
        playmetrics_teams=[],
        ntfy=NtfyConfig(enabled=False, server_url="http://ntfy.sh", topic="test"),
        youtube=YouTubeConfig(enabled=True),
        autocam=AutocamConfig(enabled=False),
        cloud_sync=CloudSyncConfig(enabled=False),
    )


@pytest.fixture
def mock_camera():
    """Create a mock camera object."""
    camera = Mock()
    camera.check_availability = AsyncMock(return_value=True)
    camera.get_file_list = AsyncMock(return_value=[])
    camera.get_connected_timeframes = Mock(return_value=[])
    camera.download_file = AsyncMock(return_value=True)
    camera.stop_recording = AsyncMock(return_value=True)
    camera.start_recording = AsyncMock(return_value=True)
    camera.get_recording_status = AsyncMock(return_value=True)
    camera.delete_files = AsyncMock(return_value=0)
    camera.supports_file_deletion = True
    camera.is_connected = True
    camera.close = AsyncMock()
    return camera


def create_mock_youtube_upload_task(group_dir: str) -> YoutubeUploadTask:
    """Create a mock YoutubeUploadTask with required dependencies."""
    return YoutubeUploadTask(group_dir=group_dir)


def shutdown_app(app):
    """Helper function to properly shutdown a VideoGrouperApp instance."""
    import asyncio

    try:
        # Check if we're already in an event loop
        asyncio.get_running_loop()
        # If we're in a loop, we can't use run_until_complete
        # Instead, we'll just close the loggers directly
        from video_grouper.utils.logger import close_loggers

        close_loggers()
    except RuntimeError:
        # No loop running, use asyncio.run
        try:
            asyncio.run(app.shutdown())
        except RuntimeError:
            # If that fails too, just close loggers
            from video_grouper.utils.logger import close_loggers

            close_loggers()


class TestVideoGrouperAppRefactored:
    """Test the refactored VideoGrouperApp."""

    def test_initialization(self, mock_config, mock_camera):
        """Test VideoGrouperApp initialization."""
        app = VideoGrouperApp(mock_config, camera=mock_camera)

        assert app.config == mock_config
        assert app.camera == mock_camera
        assert app.poll_interval == 1

        # Verify all processors are initialized
        assert app.state_auditor is not None
        assert app.camera_poller is not None
        assert app.download_processor is not None
        assert app.video_processor is not None
        assert app.upload_processor is not None

        # Verify processors are wired correctly
        assert app.state_auditor.download_processor == app.download_processor
        assert app.state_auditor.video_processor == app.video_processor
        # Note: StateAuditor no longer has upload_processor - uploads are handled by tray agent
        assert app.camera_poller.download_processor == app.download_processor
        assert app.download_processor.video_processor == app.video_processor
        assert app.video_processor.upload_processor == app.upload_processor

        # Clean up to prevent file handle issues
        shutdown_app(app)

    def test_initialization_with_camera_creation(self, mock_config):
        """Test VideoGrouperApp initialization with automatic camera creation."""
        with patch("video_grouper.cameras.dahua.DahuaCamera") as mock_dahua:
            mock_camera_instance = Mock()
            mock_camera_instance.close = AsyncMock()
            mock_dahua.return_value = mock_camera_instance

            app = VideoGrouperApp(mock_config)

            assert app.camera == mock_camera_instance
            mock_dahua.assert_called_once()

            # Clean up to prevent file handle issues
            shutdown_app(app)

    @pytest.mark.asyncio
    async def test_processor_lifecycle(self, mock_config, mock_camera):
        """Test processor lifecycle management."""
        app = VideoGrouperApp(mock_config, camera=mock_camera)

        # Initialize should start all processors
        await app.initialize()

        # All processors run a polling/processing loop — including the
        # StateAuditor, which polls so mid-session changes (e.g. a manual
        # match_info.ini edit) get picked up without a restart.
        for processor in app.processors:
            assert processor._processor_task is not None
            assert not processor._processor_task.done()

        # Shutdown should stop all processors
        await app.shutdown()

        # All processors' tasks should be stopped
        for processor in app.processors:
            assert processor._processor_task.done()

    @pytest.mark.asyncio
    async def test_add_download_task(self, mock_config, mock_camera):
        """Test adding download task through convenience method."""
        app = VideoGrouperApp(mock_config, camera=mock_camera)

        try:
            recording_file = RecordingFile(
                start_time=datetime(2023, 1, 1, 10, 0, 0),
                end_time=datetime(2023, 1, 1, 10, 5, 0),
                file_path="/test/path/test.dav",
                metadata={"path": "/test.dav"},
            )

            await app.add_download_task(recording_file)

            assert app.download_processor.get_queue_size() == 1
        finally:
            # Ensure proper cleanup to prevent asyncio warnings
            shutdown_app(app)

    @pytest.mark.asyncio
    async def test_add_video_task(self, mock_config, mock_camera):
        """Test adding video task through convenience method."""
        app = VideoGrouperApp(mock_config, camera=mock_camera)

        try:
            combine_task = CombineTask(group_dir="/test/path")

            await app.add_video_task(combine_task)

            # Verify the task was added to the video processor
            assert app.video_processor.get_queue_size() == 1

        finally:
            shutdown_app(app)

    @pytest.mark.asyncio
    async def test_add_youtube_task(self, mock_config, mock_camera):
        """Test adding YouTube task through convenience method."""
        app = VideoGrouperApp(mock_config, camera=mock_camera)

        try:
            upload_task = create_mock_youtube_upload_task("/test/path/group")

            await app.add_youtube_task(upload_task)

            assert app.upload_processor.get_queue_size() == 1
        finally:
            # Ensure proper cleanup to prevent asyncio warnings
            shutdown_app(app)

    def test_get_queue_sizes(self, mock_config, mock_camera):
        """Test getting queue sizes."""
        app = VideoGrouperApp(mock_config, camera=mock_camera)

        try:
            sizes = app.get_queue_sizes()

            assert "download" in sizes
            assert "video" in sizes
            assert "youtube" in sizes
            assert "ntfy" in sizes

            # All queues should be empty initially
            assert sizes["download"] == 0
            assert sizes["video"] == 0
            assert sizes["youtube"] == 0
        finally:
            # Ensure proper cleanup to prevent asyncio warnings
            shutdown_app(app)

    def test_get_queue_status_summary_does_not_attribute_error_on_ttt_processors(
        self, mock_config, mock_camera
    ):
        """Regression: after the TTT handler unification, clip_request and
        ttt_jobs are QueueProcessors not the old set-based polling handlers.
        The status summary used to reach into ``_processing`` /
        ``_processing_jobs`` which no longer exist, AttributeError'ing on
        every tick. Now every TTT processor goes through the same uniform
        ``get_queue_size`` / ``get_in_progress_summary`` API as the rest."""
        from unittest.mock import MagicMock

        app = VideoGrouperApp(mock_config, camera=mock_camera)
        try:
            # Inject minimal TTT processor doubles so the code paths that
            # used to reach into the gone-after-refactor attributes execute.
            for attr in (
                "clip_request_processor",
                "highlight_reel_processor",
                "ttt_job_processor",
                "reprocess_request_processor",
            ):
                p = MagicMock()
                p.get_queue_size.return_value = 0
                p.get_in_progress_summary.return_value = None
                setattr(app, attr, p)

            summary = app.get_queue_status_summary()
            # Must not raise; must produce a uniform entry per TTT processor.
            assert "clip_request" in summary
            assert "highlight_reel" in summary
            assert "ttt_jobs" in summary
            assert "reprocess_request" in summary
        finally:
            shutdown_app(app)

    def test_get_processor_status(self, mock_config, mock_camera):
        """Test getting processor status."""
        app = VideoGrouperApp(mock_config, camera=mock_camera)

        try:
            status = app.get_processor_status()

            assert "state_auditor" in status
            assert "camera_poller" in status
            assert "download_processor" in status
            assert "video_processor" in status
            assert "upload_processor" in status
            assert "ntfy_processor" in status

            # All processors should be stopped initially (except optional processors
            # which may be disabled, and StateAuditor which is startup-only)
            for processor_name, processor_status in status.items():
                if processor_name in (
                    "ntfy_processor",
                    "clip_request_processor",
                    "ttt_job_processor",
                ):
                    # Optional processors can be "stopped" or "disabled" depending on config
                    assert processor_status in ["stopped", "disabled"]
                elif processor_name == "state_auditor":
                    assert processor_status == "startup_only"
                else:
                    assert processor_status == "stopped"
        finally:
            # Ensure proper cleanup to prevent asyncio warnings
            shutdown_app(app)

    @pytest.mark.asyncio
    async def test_integration_workflow(self, mock_config, mock_camera, temp_storage):
        """Test a complete workflow integration."""
        app = VideoGrouperApp(mock_config, camera=mock_camera)

        try:
            # Use mock paths instead of creating actual files
            group_dir = "/test/group"
            test_file = "/test/group/test.dav"

            # Create and add tasks
            recording_file = RecordingFile(
                start_time=datetime(2023, 1, 1, 10, 0, 0),
                end_time=datetime(2023, 1, 1, 10, 5, 0),
                file_path=test_file,
                metadata={"path": "/test.dav"},
            )

            combine_task = CombineTask(group_dir=group_dir)
            upload_task = create_mock_youtube_upload_task(group_dir)

            # Add tasks to queues
            await app.add_download_task(recording_file)
            await app.add_video_task(combine_task)
            await app.add_youtube_task(upload_task)

            # Verify tasks were added
            queue_sizes = app.get_queue_sizes()
            assert queue_sizes["download"] == 1
            assert queue_sizes["video"] == 1
            assert queue_sizes["youtube"] == 1
        finally:
            # Ensure proper cleanup to prevent asyncio warnings
            shutdown_app(app)

    @pytest.mark.asyncio
    async def test_error_handling_during_initialization(self, mock_config):
        """Test error handling during initialization."""
        # Test with invalid camera configuration
        mock_config.camera.type = "invalid_camera"

        try:
            with pytest.raises(ValueError, match="Unknown camera type"):
                VideoGrouperApp(mock_config)
        finally:
            # Ensure loggers are closed even if exception occurs
            from video_grouper.utils.logger import close_loggers

            close_loggers()

    @pytest.mark.asyncio
    async def test_camera_close_on_shutdown(self, mock_config, mock_camera):
        """Test that camera is properly closed on shutdown."""
        app = VideoGrouperApp(mock_config, camera=mock_camera)

        try:
            await app.initialize()
            await app.shutdown()

            mock_camera.close.assert_called_once()
        finally:
            # Ensure proper cleanup to prevent asyncio warnings
            shutdown_app(app)

    @pytest.mark.asyncio
    async def test_storage_path_handling(self, mock_config, mock_camera):
        """Test storage path is properly handled."""
        app = VideoGrouperApp(mock_config, camera=mock_camera)

        try:
            # Storage path should be absolute
            assert os.path.isabs(app.storage_path)

            # All processors should have the same storage path
            for processor in app.processors:
                assert processor.storage_path == app.storage_path
        finally:
            # Ensure proper cleanup to prevent asyncio warnings
            shutdown_app(app)

    def test_video_processor_wired_with_ntfy_services(self, temp_storage, mock_camera):
        """When NTFY is enabled, VideoProcessor should be wired with match_info_service and ntfy_processor."""
        ntfy_config = Config(
            cameras=[
                CameraConfig(
                    name="default",
                    type="dahua",
                    device_ip="192.168.1.100",
                    username="admin",
                    password="password",
                )
            ],
            storage=StorageConfig(path=temp_storage),
            recording=RecordingConfig(),
            processing=ProcessingConfig(),
            logging=LoggingConfig(),
            app=AppConfig(storage_path=temp_storage, check_interval_seconds=1),
            teamsnap=TeamSnapConfig(enabled=False, team_id="1", my_team_name="Team A"),
            teamsnap_teams=[],
            playmetrics=PlayMetricsConfig(
                enabled=False,
                username="user",
                password="pass",
                team_name="Team A",
            ),
            playmetrics_teams=[],
            ntfy=NtfyConfig(enabled=True, server_url="http://ntfy.sh", topic="test"),
            youtube=YouTubeConfig(enabled=True),
            autocam=AutocamConfig(enabled=False),
            cloud_sync=CloudSyncConfig(enabled=False),
        )

        try:
            app = VideoGrouperApp(ntfy_config, camera=mock_camera)

            # VideoProcessor should have match_info_service and ntfy_processor wired
            assert app.video_processor.match_info_service is not None
            assert app.video_processor.ntfy_processor is app.ntfy_processor
        finally:
            shutdown_app(app)

    def test_video_processor_no_ntfy_services_when_disabled(
        self, mock_config, mock_camera
    ):
        """When NTFY is disabled, VideoProcessor should have None for services."""
        app = VideoGrouperApp(mock_config, camera=mock_camera)

        try:
            assert app.video_processor.match_info_service is None
            assert app.video_processor.ntfy_processor is None
        finally:
            shutdown_app(app)

    def test_upload_processor_wired_with_ntfy_service(self, temp_storage, mock_camera):
        """When NTFY is enabled, UploadProcessor should have ntfy_service wired."""
        ntfy_config = Config(
            cameras=[
                CameraConfig(
                    name="default",
                    type="dahua",
                    device_ip="192.168.1.100",
                    username="admin",
                    password="password",
                )
            ],
            storage=StorageConfig(path=temp_storage),
            recording=RecordingConfig(),
            processing=ProcessingConfig(),
            logging=LoggingConfig(),
            app=AppConfig(storage_path=temp_storage, check_interval_seconds=1),
            teamsnap=TeamSnapConfig(enabled=False, team_id="1", my_team_name="Team A"),
            teamsnap_teams=[],
            playmetrics=PlayMetricsConfig(
                enabled=False,
                username="user",
                password="pass",
                team_name="Team A",
            ),
            playmetrics_teams=[],
            ntfy=NtfyConfig(enabled=True, server_url="http://ntfy.sh", topic="test"),
            youtube=YouTubeConfig(enabled=True),
            autocam=AutocamConfig(enabled=False),
            cloud_sync=CloudSyncConfig(enabled=False),
        )

        try:
            app = VideoGrouperApp(ntfy_config, camera=mock_camera)

            # UploadProcessor should have ntfy_service wired
            assert app.upload_processor.ntfy_service is not None
        finally:
            shutdown_app(app)

    def test_upload_processor_no_ntfy_service_when_disabled(
        self, mock_config, mock_camera
    ):
        """When NTFY is disabled, UploadProcessor should have None for ntfy_service."""
        app = VideoGrouperApp(mock_config, camera=mock_camera)

        try:
            assert app.upload_processor.ntfy_service is None
        finally:
            shutdown_app(app)


class TestPipelinePlacement:
    """The config-driven pipeline is the sole post-trim-processing path.

    When [PIPELINE] is active the service constructs the PipelineProcessor +
    PipelineDiscoveryProcessor and adds them to the orchestrator's processor
    list; when it is inactive, neither is constructed.
    """

    def _config_with_pipeline(self, temp_storage, *, enabled, step_type="track"):
        """Build a Config that toggles the [PIPELINE] section.

        A single service-runtime step (``track``) keeps ``is_active()`` true
        without pulling in a tray-runtime step (which is refused on non-Windows).
        """
        from video_grouper.pipeline.config import PipelineConfig, PipelineStepSpec
        from video_grouper.utils.config import Config

        pipeline = PipelineConfig(
            enabled=enabled,
            steps=["s1"] if enabled else [],
            step_specs=(
                {"s1": PipelineStepSpec(step_id="s1", type=step_type, config={})}
                if enabled
                else {}
            ),
        )
        return Config(
            cameras=[
                CameraConfig(
                    name="default",
                    type="dahua",
                    device_ip="192.168.1.100",
                    username="admin",
                    password="password",
                )
            ],
            storage=StorageConfig(path=temp_storage),
            recording=RecordingConfig(),
            processing=ProcessingConfig(),
            logging=LoggingConfig(),
            app=AppConfig(storage_path=temp_storage, check_interval_seconds=1),
            teamsnap=TeamSnapConfig(enabled=False, team_id="1", my_team_name="Team A"),
            teamsnap_teams=[],
            playmetrics=PlayMetricsConfig(
                enabled=False, username="u", password="p", team_name="Team A"
            ),
            playmetrics_teams=[],
            ntfy=NtfyConfig(enabled=False, server_url="http://ntfy.sh", topic="t"),
            youtube=YouTubeConfig(enabled=True),
            autocam=AutocamConfig(enabled=False),
            cloud_sync=CloudSyncConfig(enabled=False),
            pipeline=pipeline,
        )

    def test_inactive_does_not_instantiate_processors(self, temp_storage, mock_camera):
        cfg = self._config_with_pipeline(temp_storage, enabled=False)
        app = VideoGrouperApp(cfg, camera=mock_camera)
        try:
            assert app.pipeline_processor is None
            assert app.pipeline_discovery_processor is None
            assert app.pipeline_processor not in app.processors
        finally:
            shutdown_app(app)

    def test_active_pipeline_runs_in_service(self, temp_storage, mock_camera):
        cfg = self._config_with_pipeline(temp_storage, enabled=True)
        app = VideoGrouperApp(cfg, camera=mock_camera)
        try:
            assert app.pipeline_processor is not None
            assert app.pipeline_discovery_processor is not None
            # Both join the orchestrator's processor list so they start
            # alongside the rest of the pipeline.
            assert app.pipeline_processor in app.processors
            assert app.pipeline_discovery_processor in app.processors
            # The service runs the pipeline with a direct upload chain.
            assert app.pipeline_processor.upload_processor is app.upload_processor
        finally:
            shutdown_app(app)

    def test_tray_runtime_step_refused_on_linux(self, temp_storage, mock_camera):
        """A tray-runtime step (autocam drives a Windows GUI app) can't run on
        Linux/Docker. The service refuses to start with a clear error."""
        cfg = self._config_with_pipeline(
            temp_storage, enabled=True, step_type="autocam"
        )
        with patch("platform.system", return_value="Linux"):
            with pytest.raises(RuntimeError, match="runtime='tray'"):
                VideoGrouperApp(cfg, camera=mock_camera)
