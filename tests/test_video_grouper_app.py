"""Integration tests for the refactored VideoGrouperApp."""

import os
import tempfile
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime
import pytest

from video_grouper.video_grouper_app import VideoGrouperApp
from video_grouper.models import RecordingFile
from video_grouper.task_processors.tasks.video import CombineTask
from video_grouper.task_processors.tasks.upload import YoutubeUploadTask
from video_grouper.utils.config import (
    Config,
    CameraConfig,
    TeamSnapConfig,
    PlayMetricsConfig,
    NtfyConfig,
    YouTubeConfig,
    AutocamConfig,
    CloudSyncConfig,
    AppConfig,
    StorageConfig,
    RecordingConfig,
    ProcessingConfig,
    LoggingConfig,
)
from video_grouper.utils.logger import close_loggers


@pytest.fixture(autouse=True)
def cleanup_loggers():
    """Clean up loggers after each test to prevent file handle issues."""
    yield
    # Close all loggers to release file handles
    close_loggers()


@pytest.fixture
def temp_storage():
    """Create a temporary storage directory for testing."""
    import time
    import shutil

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

        # All processors except StateAuditor (startup-only) should be running
        for processor in app.processors:
            if processor is app.state_auditor:
                # StateAuditor is startup-only: runs discover_work() once, no loop
                assert processor._processor_task is None
            else:
                assert processor._processor_task is not None
                assert not processor._processor_task.done()

        # Shutdown should stop all processors
        await app.shutdown()

        # All processors with tasks should be stopped
        for processor in app.processors:
            if processor is app.state_auditor:
                continue  # No task to check
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
                if processor_name in ("ntfy_processor", "clip_request_processor"):
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
            with pytest.raises(ValueError, match="Unsupported camera type"):
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
