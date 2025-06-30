"""Tests for the DownloadProcessor."""

import tempfile
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime
import pytest

from video_grouper.task_processors.download_processor import DownloadProcessor
from video_grouper.models import RecordingFile
from video_grouper.task_processors.tasks import ConvertTask
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


@pytest.fixture
def temp_storage():
    """Create a temporary storage directory for tests."""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def mock_config(temp_storage):
    """Create a mock configuration."""
    return Config(
        camera=CameraConfig(
            type="dahua", device_ip="127.0.0.1", username="admin", password="password"
        ),
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
        youtube=YouTubeConfig(enabled=False),
        autocam=AutocamConfig(enabled=False),
        cloud_sync=CloudSyncConfig(enabled=False),
    )


@pytest.fixture
def mock_camera():
    """Create a mock camera."""
    camera = Mock()
    camera.download_file = AsyncMock(return_value=True)
    return camera


class TestDownloadProcessor:
    """Test cases for DownloadProcessor class."""

    @pytest.mark.asyncio
    async def test_download_processor_initialization(
        self, temp_storage, mock_config, mock_camera
    ):
        """Test DownloadProcessor initialization."""
        processor = DownloadProcessor(temp_storage, mock_config, mock_camera)

        assert processor.storage_path == temp_storage
        assert processor.config == mock_config
        assert processor.camera == mock_camera
        assert processor.video_processor is None

    @pytest.mark.asyncio
    async def test_set_video_processor(self, temp_storage, mock_config, mock_camera):
        """Test setting video processor reference."""
        processor = DownloadProcessor(temp_storage, mock_config, mock_camera)
        mock_video = Mock()

        processor.set_video_processor(mock_video)

        assert processor.video_processor == mock_video

    @pytest.mark.asyncio
    @patch("video_grouper.task_processors.download_processor.DirectoryState")
    async def test_successful_download(
        self, mock_directory_state, temp_storage, mock_config, mock_camera
    ):
        """Test successful file download."""
        # Create recording file
        recording_file = RecordingFile(
            start_time=datetime(2023, 1, 1, 10, 0, 0),
            end_time=datetime(2023, 1, 1, 10, 5, 0),
            file_path="/test/group/test.dav",
            metadata={"path": "/test.dav"},
        )

        # Mock DirectoryState
        mock_dir_state_instance = Mock()
        mock_dir_state_instance.update_file_state = AsyncMock()
        mock_directory_state.return_value = mock_dir_state_instance

        # Mock successful download
        mock_camera.download_file.return_value = True

        # Create mock video processor
        mock_video = Mock()
        mock_video.add_work = AsyncMock()

        processor = DownloadProcessor(temp_storage, mock_config, mock_camera)
        processor.set_video_processor(mock_video)

        # Process the download
        await processor.process_item(recording_file)

        mock_camera.download_file.assert_called_once_with(
            file_path="/test.dav", local_path="/test/group/test.dav"
        )
        mock_video.add_work.assert_called_once()

        # Verify convert task was queued
        queued_task = mock_video.add_work.call_args[0][0]
        assert isinstance(queued_task, ConvertTask)
        assert queued_task.file_path == recording_file.file_path

        # Verify file status was updated
        mock_dir_state_instance.update_file_state.assert_any_call(
            "/test/group/test.dav", status="downloading"
        )
        mock_dir_state_instance.update_file_state.assert_any_call(
            "/test/group/test.dav", status="downloaded"
        )

    @pytest.mark.asyncio
    @patch("video_grouper.task_processors.download_processor.DirectoryState")
    async def test_failed_download(
        self, mock_directory_state, temp_storage, mock_config, mock_camera
    ):
        """Test failed file download."""
        recording_file = RecordingFile(
            start_time=datetime(2023, 1, 1, 10, 0, 0),
            end_time=datetime(2023, 1, 1, 10, 5, 0),
            file_path="/test/group/test.dav",
            metadata={"path": "/test.dav"},
        )

        # Mock DirectoryState
        mock_dir_state_instance = Mock()
        mock_dir_state_instance.update_file_state = AsyncMock()
        mock_directory_state.return_value = mock_dir_state_instance

        # Mock failed download
        mock_camera.download_file.return_value = False

        processor = DownloadProcessor(temp_storage, mock_config, mock_camera)

        # Process the download
        await processor.process_item(recording_file)

        mock_camera.download_file.assert_called_once()

        # Verify file status was updated to download_failed
        mock_dir_state_instance.update_file_state.assert_any_call(
            "/test/group/test.dav", status="downloading"
        )
        mock_dir_state_instance.update_file_state.assert_any_call(
            "/test/group/test.dav", status="download_failed"
        )

    @pytest.mark.asyncio
    @patch("video_grouper.task_processors.download_processor.DirectoryState")
    async def test_download_exception_handling(
        self, mock_directory_state, temp_storage, mock_config, mock_camera
    ):
        """Test exception handling during download."""
        recording_file = RecordingFile(
            start_time=datetime(2023, 1, 1, 10, 0, 0),
            end_time=datetime(2023, 1, 1, 10, 5, 0),
            file_path="/test/group/test.dav",
            metadata={"path": "/test.dav"},
        )

        # Mock DirectoryState
        mock_dir_state_instance = Mock()
        mock_dir_state_instance.update_file_state = AsyncMock()
        mock_directory_state.return_value = mock_dir_state_instance

        # Mock download raising exception
        mock_camera.download_file.side_effect = Exception("Download error")

        processor = DownloadProcessor(temp_storage, mock_config, mock_camera)

        # Process the download - should handle exception gracefully
        await processor.process_item(recording_file)

        # Verify file status was updated to download_failed
        mock_dir_state_instance.update_file_state.assert_any_call(
            "/test/group/test.dav", status="downloading"
        )
        mock_dir_state_instance.update_file_state.assert_any_call(
            "/test/group/test.dav", status="download_failed"
        )

    @pytest.mark.asyncio
    @patch("video_grouper.task_processors.download_processor.DirectoryState")
    async def test_download_without_video_processor(
        self, mock_directory_state, temp_storage, mock_config, mock_camera
    ):
        """Test download when video processor is not set."""
        recording_file = RecordingFile(
            start_time=datetime(2023, 1, 1, 10, 0, 0),
            end_time=datetime(2023, 1, 1, 10, 5, 0),
            file_path="/test/group/test.dav",
            metadata={"path": "/test.dav"},
        )

        # Mock DirectoryState
        mock_dir_state_instance = Mock()
        mock_dir_state_instance.update_file_state = AsyncMock()
        mock_directory_state.return_value = mock_dir_state_instance

        # Mock successful download
        mock_camera.download_file.return_value = True

        processor = DownloadProcessor(temp_storage, mock_config, mock_camera)
        # Don't set video processor

        # Process the download
        await processor.process_item(recording_file)

        mock_camera.download_file.assert_called_once()

        # Verify file status was updated even without video processor
        mock_dir_state_instance.update_file_state.assert_any_call(
            "/test/group/test.dav", status="downloading"
        )
        mock_dir_state_instance.update_file_state.assert_any_call(
            "/test/group/test.dav", status="downloaded"
        )

    def test_get_item_key_recording_file(self, temp_storage, mock_config, mock_camera):
        """Test getting unique key for RecordingFile."""
        processor = DownloadProcessor(temp_storage, mock_config, mock_camera)

        recording_file = RecordingFile(
            start_time=datetime(2023, 1, 1, 10, 0, 0),
            end_time=datetime(2023, 1, 1, 10, 5, 0),
            file_path="/test/path/test.dav",
            metadata={"path": "/test.dav"},
        )

        key = processor.get_item_key(recording_file)
        assert key.startswith("recording:/test/path/test.dav:")
