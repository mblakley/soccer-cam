"""Tests for the CameraPoller processor."""

import os
import tempfile
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime
import pytest
import pytz

from video_grouper.task_processors.camera_poller import (
    CameraPoller,
    find_group_directory,
)
from video_grouper.models import RecordingFile
from video_grouper.utils.directory_state import DirectoryState
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
    camera.check_availability = AsyncMock(return_value=True)
    camera.get_file_list = AsyncMock(return_value=[])
    camera.get_connected_timeframes = Mock(return_value=[])
    camera.download_file = AsyncMock(return_value=True)
    camera.close = AsyncMock()
    return camera


class TestCameraPoller:
    """Test cases for CameraPoller class."""

    @pytest.mark.asyncio
    async def test_camera_poller_initialization(
        self, temp_storage, mock_config, mock_camera
    ):
        """Test CameraPoller initialization."""
        poller = CameraPoller(temp_storage, mock_config, mock_camera, poll_interval=5)

        assert poller.storage_path == temp_storage
        assert poller.config == mock_config
        assert poller.camera == mock_camera
        assert poller.download_processor is None
        assert poller.poll_interval == 5

    @pytest.mark.asyncio
    async def test_set_download_processor(self, temp_storage, mock_config, mock_camera):
        """Test setting download processor reference."""
        poller = CameraPoller(temp_storage, mock_config, mock_camera)
        mock_download = Mock()

        poller.set_download_processor(mock_download)

        assert poller.download_processor == mock_download

    @pytest.mark.asyncio
    async def test_discover_work_camera_unavailable(
        self, temp_storage, mock_config, mock_camera
    ):
        """Test discover_work when camera is unavailable."""
        mock_camera.check_availability.return_value = False

        poller = CameraPoller(temp_storage, mock_config, mock_camera)

        # Should handle unavailable camera gracefully
        await poller.discover_work()

        mock_camera.check_availability.assert_called_once()
        mock_camera.get_file_list.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_files_from_camera_no_files(
        self, temp_storage, mock_config, mock_camera
    ):
        """Test syncing when no files are found."""
        mock_camera.get_file_list.return_value = []

        poller = CameraPoller(temp_storage, mock_config, mock_camera)

        await poller._sync_files_from_camera()

        mock_camera.get_file_list.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_files_from_camera_with_files(
        self, temp_storage, mock_config, mock_camera
    ):
        """Test syncing files from camera."""
        # Mock file list from camera
        mock_files = [
            {
                "path": "/test1.dav",
                "startTime": "2023-01-01 10:00:00",
                "endTime": "2023-01-01 10:05:00",
            }
        ]
        mock_camera.get_file_list.return_value = mock_files

        # Create mock download processor
        mock_download = Mock()
        mock_download.add_work = AsyncMock()

        poller = CameraPoller(temp_storage, mock_config, mock_camera)
        poller.set_download_processor(mock_download)

        await poller._sync_files_from_camera()

        # Verify file was processed and queued for download
        mock_download.add_work.assert_called_once()
        queued_file = mock_download.add_work.call_args[0][0]
        assert isinstance(queued_file, RecordingFile)
        assert queued_file.file_path.endswith("test1.dav")

    @pytest.mark.asyncio
    async def test_connected_timeframe_filtering(
        self, temp_storage, mock_config, mock_camera
    ):
        """Test that files overlapping with connected timeframes are filtered out."""
        # Mock connected timeframes (camera was connected from 10:00 to 10:10)
        connected_start = datetime(2023, 1, 1, 10, 0, 0, tzinfo=pytz.utc)
        connected_end = datetime(2023, 1, 1, 10, 10, 0, tzinfo=pytz.utc)
        mock_camera.get_connected_timeframes.return_value = [
            (connected_start, connected_end)
        ]

        # Mock file that overlaps with connected timeframe
        mock_files = [
            {
                "path": "/test_overlapping.dav",
                "startTime": "2023-01-01 10:05:00",  # Overlaps with connected time
                "endTime": "2023-01-01 10:15:00",
            },
            {
                "path": "/test_valid.dav",
                "startTime": "2023-01-01 11:00:00",  # After connected time
                "endTime": "2023-01-01 11:05:00",
            },
        ]
        mock_camera.get_file_list.return_value = mock_files

        mock_download = Mock()
        mock_download.add_work = AsyncMock()

        poller = CameraPoller(temp_storage, mock_config, mock_camera)
        poller.set_download_processor(mock_download)

        await poller._sync_files_from_camera()

        # Only the non-overlapping file should be queued
        mock_download.add_work.assert_called_once()
        queued_file = mock_download.add_work.call_args[0][0]
        assert "test_valid.dav" in queued_file.file_path

    @pytest.mark.asyncio
    @patch("video_grouper.task_processors.camera_poller.DirectoryState")
    @patch("video_grouper.task_processors.camera_poller.find_group_directory")
    async def test_skip_existing_files(
        self,
        mock_find_group,
        mock_directory_state,
        temp_storage,
        mock_config,
        mock_camera,
    ):
        """Test that existing files are skipped."""
        group_dir = "/test/group"

        # Mock DirectoryState to return existing file
        mock_dir_state_instance = Mock()
        mock_existing_file = Mock()
        mock_existing_file.metadata = {"path": "/test1.dav"}
        mock_dir_state_instance.files = {"/test/group/test1.dav": mock_existing_file}
        mock_directory_state.return_value = mock_dir_state_instance

        # Mock find_group_directory to return our test group
        mock_find_group.return_value = group_dir

        # Mock camera returning the same file
        mock_files = [
            {
                "path": "/test1.dav",
                "startTime": "2023-01-01 10:00:00",
                "endTime": "2023-01-01 10:05:00",
            }
        ]
        mock_camera.get_file_list.return_value = mock_files

        mock_download = Mock()
        mock_download.add_work = AsyncMock()

        poller = CameraPoller(temp_storage, mock_config, mock_camera)
        poller.set_download_processor(mock_download)

        await poller._sync_files_from_camera()

        # File should not be queued again
        mock_download.add_work.assert_not_called()

    def test_find_group_directory_new_group(self, temp_storage):
        """Test finding group directory when creating a new group."""
        file_start_time = datetime(2023, 1, 1, 10, 0, 0)

        group_dir = find_group_directory(file_start_time, temp_storage, [])

        expected_dir = os.path.join(temp_storage, "2023.01.01-10.00.00")
        assert group_dir == expected_dir
        assert os.path.exists(group_dir)

    @patch("video_grouper.task_processors.camera_poller.DirectoryState")
    @patch("os.path.exists")
    def test_find_group_directory_existing_group(
        self, mock_exists, mock_directory_state, temp_storage
    ):
        """Test finding group directory when file fits in existing group."""
        existing_group = os.path.join(temp_storage, "2023.01.01-10.00.00")

        # Mock that the state file exists
        mock_exists.return_value = True

        # Mock DirectoryState to return a file ending at 10:05:00
        mock_dir_state_instance = Mock()
        mock_last_file = Mock()
        mock_last_file.end_time = datetime(2023, 1, 1, 10, 5, 0)
        mock_dir_state_instance.get_last_file.return_value = mock_last_file
        mock_directory_state.return_value = mock_dir_state_instance

        # New file starting within 15 seconds should use same group
        file_start_time = datetime(2023, 1, 1, 10, 5, 10)  # 10 seconds after last file

        group_dir = find_group_directory(
            file_start_time, temp_storage, [existing_group]
        )

        assert group_dir == existing_group

    def test_find_group_directory_gap_too_large(self, temp_storage):
        """Test finding group directory when gap is too large for existing group."""
        # Create existing group
        existing_group = os.path.join(temp_storage, "2023.01.01-10.00.00")
        os.makedirs(existing_group)

        # Create state with a file ending at 10:05:00
        dir_state = DirectoryState(existing_group)
        existing_file = RecordingFile(
            start_time=datetime(2023, 1, 1, 10, 0, 0),
            end_time=datetime(2023, 1, 1, 10, 5, 0),
            file_path=os.path.join(existing_group, "test1.dav"),
            metadata={"path": "/test1.dav"},
        )
        # Use synchronous method for test
        import asyncio

        asyncio.run(dir_state.add_file(existing_file.file_path, existing_file))

        # New file starting more than 15 seconds later should create new group
        file_start_time = datetime(2023, 1, 1, 10, 5, 30)  # 30 seconds after last file

        group_dir = find_group_directory(
            file_start_time, temp_storage, [existing_group]
        )

        expected_new_dir = os.path.join(temp_storage, "2023.01.01-10.05.30")
        assert group_dir == expected_new_dir
        assert os.path.exists(group_dir)

    @pytest.mark.asyncio
    async def test_filter_file_within_connected_timeframe_poller(
        self, temp_storage, mock_config, mock_camera
    ):
        """Test that files completely within a connected timeframe are filtered out."""
        connected_start = datetime(2023, 1, 1, 10, 0, tzinfo=pytz.utc)
        connected_end = datetime(2023, 1, 1, 10, 10, tzinfo=pytz.utc)
        mock_camera.get_connected_timeframes.return_value = [
            (connected_start, connected_end)
        ]

        mock_files = [
            {
                "path": "/test_within.dav",
                "startTime": "2023-01-01 10:01:00",
                "endTime": "2023-01-01 10:05:00",
            }
        ]
        mock_camera.get_file_list.return_value = mock_files

        mock_download = Mock()
        mock_download.add_work = AsyncMock()

        poller = CameraPoller(temp_storage, mock_config, mock_camera)
        poller.set_download_processor(mock_download)

        await poller._sync_files_from_camera()

        mock_download.add_work.assert_not_called()

    @pytest.mark.asyncio
    async def test_filter_file_containing_connected_timeframe_poller(
        self, temp_storage, mock_config, mock_camera
    ):
        """Test that files containing a connected timeframe are filtered out."""
        connected_start = datetime(2023, 1, 1, 10, 0, tzinfo=pytz.utc)
        connected_end = datetime(2023, 1, 1, 10, 10, tzinfo=pytz.utc)
        mock_camera.get_connected_timeframes.return_value = [
            (connected_start, connected_end)
        ]

        mock_files = [
            {
                "path": "/test_containing.dav",
                "startTime": "2023-01-01 09:00:00",
                "endTime": "2023-01-01 11:00:00",
            }
        ]
        mock_camera.get_file_list.return_value = mock_files

        mock_download = Mock()
        mock_download.add_work = AsyncMock()

        poller = CameraPoller(temp_storage, mock_config, mock_camera)
        poller.set_download_processor(mock_download)

        await poller._sync_files_from_camera()

        mock_download.add_work.assert_not_called()
