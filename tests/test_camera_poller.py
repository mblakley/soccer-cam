"""Tests for the CameraPoller processor."""

import os
import tempfile
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime
import pytest
import pytz

from pathlib import Path

from video_grouper.task_processors.camera_poller import (
    CameraPoller,
    find_group_directory,
)
from video_grouper.models import RecordingFile
from video_grouper.models import DirectoryState
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
        cameras=[
            CameraConfig(
                name="default",
                type="dahua",
                device_ip="127.0.0.1",
                username="admin",
                password="password",
            )
        ],
        storage=StorageConfig(path=temp_storage),
        recording=RecordingConfig(),
        processing=ProcessingConfig(),
        logging=LoggingConfig(),
        app=AppConfig(
            storage_path=temp_storage,
            check_interval_seconds=1,
            timezone="America/New_York",
        ),
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
    camera.stop_recording = AsyncMock(return_value=True)
    camera.get_recording_status = AsyncMock(return_value=True)
    camera.delete_files = AsyncMock(return_value=0)
    camera.is_connected = True
    camera.close = AsyncMock()
    # Camera.config is read by CameraPoller for auto_stop_recording and metadata
    cam_config = Mock()
    cam_config.auto_stop_recording = True
    cam_config.type = "dahua"
    cam_config.name = "default"
    camera.config = cam_config
    return camera


def _make_poller(temp_storage, mock_config, mock_camera, mock_download_processor=None):
    """Helper to create a CameraPoller with common defaults."""
    if mock_download_processor is None:
        mock_download_processor = Mock()
        mock_download_processor.get_queue_size = Mock(return_value=0)
        mock_download_processor._in_progress_item = None
    return CameraPoller(temp_storage, mock_config, mock_camera, mock_download_processor)


class TestCameraPoller:
    """Test cases for CameraPoller class."""

    @pytest.mark.asyncio
    async def test_camera_poller_initialization(
        self, temp_storage, mock_config, mock_camera
    ):
        """Test CameraPoller initialization."""
        mock_download_processor = Mock()
        poller = CameraPoller(
            temp_storage,
            mock_config,
            mock_camera,
            mock_download_processor,
            poll_interval=5,
        )

        assert poller.storage_path == temp_storage
        assert poller.config == mock_config
        assert poller.camera == mock_camera
        assert poller.download_processor == mock_download_processor
        assert poller.poll_interval == 5

    @pytest.mark.asyncio
    async def test_set_download_processor(self, temp_storage, mock_config, mock_camera):
        """Test setting download processor reference."""
        mock_download_processor = Mock()
        poller = CameraPoller(
            temp_storage, mock_config, mock_camera, mock_download_processor
        )

        assert poller.download_processor == mock_download_processor

    @pytest.mark.asyncio
    async def test_discover_work_camera_unavailable(
        self, temp_storage, mock_config, mock_camera
    ):
        """Test discover_work when camera is unavailable."""
        mock_camera.check_availability.return_value = False

        mock_download_processor = Mock()
        poller = CameraPoller(
            temp_storage, mock_config, mock_camera, mock_download_processor
        )

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

        mock_download_processor = Mock()
        poller = CameraPoller(
            temp_storage, mock_config, mock_camera, mock_download_processor
        )

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
        mock_download_processor = Mock()
        mock_download_processor.add_work = AsyncMock()

        poller = CameraPoller(
            temp_storage, mock_config, mock_camera, mock_download_processor
        )

        await poller._sync_files_from_camera()

        # Verify file was processed and queued for download
        mock_download_processor.add_work.assert_called_once()
        queued_file = mock_download_processor.add_work.call_args[0][0]
        assert isinstance(queued_file, RecordingFile)
        assert queued_file.file_path.endswith("test1.dav")

    @pytest.mark.asyncio
    async def test_connected_timeframe_filtering(
        self, temp_storage, mock_config, mock_camera
    ):
        """Test that files overlapping with connected timeframes are filtered out."""
        # Mock connected timeframes (camera was connected from 10:00 to 10:10 UTC)
        connected_start = datetime(2023, 1, 1, 10, 0, 0, tzinfo=pytz.utc)
        connected_end = datetime(2023, 1, 1, 10, 10, 0, tzinfo=pytz.utc)
        mock_camera.get_connected_timeframes.return_value = [
            (connected_start, connected_end)
        ]

        # Mock files - timestamps are in local time (America/New_York)
        # For files to overlap with UTC 10:00-10:10, they need to be in local time
        # America/New_York is UTC-5 in January, so local 05:00-05:10 corresponds to UTC 10:00-10:10
        mock_files = [
            {
                "path": "/test_overlapping.dav",
                "startTime": "2023-01-01 05:05:00",  # Local time, converts to UTC 10:05:00 (overlaps)
                "endTime": "2023-01-01 05:15:00",  # Local time, converts to UTC 10:15:00
            },
            {
                "path": "/test_valid.dav",
                "startTime": "2023-01-01 06:00:00",  # Local time, converts to UTC 11:00:00 (after connected time)
                "endTime": "2023-01-01 06:05:00",  # Local time, converts to UTC 11:05:00
            },
        ]
        mock_camera.get_file_list.return_value = mock_files

        mock_download_processor = Mock()
        mock_download_processor.add_work = AsyncMock()

        poller = CameraPoller(
            temp_storage, mock_config, mock_camera, mock_download_processor
        )

        await poller._sync_files_from_camera()

        # Only the non-overlapping file should be queued
        mock_download_processor.add_work.assert_called_once()
        queued_file = mock_download_processor.add_work.call_args[0][0]
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

        mock_download_processor = Mock()
        mock_download_processor.add_work = AsyncMock()

        poller = CameraPoller(
            temp_storage, mock_config, mock_camera, mock_download_processor
        )

        await poller._sync_files_from_camera()

        # File should not be queued again
        mock_download_processor.add_work.assert_not_called()

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

        # New file starting within 5 seconds should use same group
        file_start_time = datetime(2023, 1, 1, 10, 5, 3)  # 3 seconds after last file

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

        # New file starting more than 5 seconds later should create new group
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

        # File completely within connected timeframe
        # Local time 05:01-05:05 converts to UTC 10:01-10:05 (within 10:00-10:10)
        mock_files = [
            {
                "path": "/test_within.dav",
                "startTime": "2023-01-01 05:01:00",  # Local time, converts to UTC 10:01:00
                "endTime": "2023-01-01 05:05:00",  # Local time, converts to UTC 10:05:00
            }
        ]
        mock_camera.get_file_list.return_value = mock_files

        mock_download_processor = Mock()
        mock_download_processor.add_work = AsyncMock()

        poller = CameraPoller(
            temp_storage, mock_config, mock_camera, mock_download_processor
        )

        await poller._sync_files_from_camera()

        mock_download_processor.add_work.assert_not_called()

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

        # File containing the connected timeframe
        # Local time 04:00-06:00 converts to UTC 09:00-11:00 (contains 10:00-10:10)
        mock_files = [
            {
                "path": "/test_containing.dav",
                "startTime": "2023-01-01 04:00:00",  # Local time, converts to UTC 09:00:00
                "endTime": "2023-01-01 06:00:00",  # Local time, converts to UTC 11:00:00
            }
        ]
        mock_camera.get_file_list.return_value = mock_files

        mock_download_processor = Mock()
        mock_download_processor.add_work = AsyncMock()

        poller = CameraPoller(
            temp_storage, mock_config, mock_camera, mock_download_processor
        )

        await poller._sync_files_from_camera()

        mock_download_processor.add_work.assert_not_called()

    @patch("video_grouper.task_processors.camera_poller.DirectoryState")
    @patch("os.path.exists")
    def test_find_group_directory_boundary_5s_joins_group(
        self, mock_exists, mock_directory_state, temp_storage
    ):
        """Test that a file exactly 5 seconds after last file joins the same group."""
        existing_group = os.path.join(temp_storage, "2023.01.01-10.00.00")

        mock_exists.return_value = True

        mock_dir_state_instance = Mock()
        mock_last_file = Mock()
        mock_last_file.end_time = datetime(2023, 1, 1, 10, 5, 0)
        mock_dir_state_instance.get_last_file.return_value = mock_last_file
        mock_directory_state.return_value = mock_dir_state_instance

        # File starting exactly 5 seconds after last file should join group
        file_start_time = datetime(2023, 1, 1, 10, 5, 5)

        group_dir = find_group_directory(
            file_start_time, temp_storage, [existing_group]
        )

        assert group_dir == existing_group

    def test_find_group_directory_boundary_6s_creates_new_group(self, temp_storage):
        """Test that a file 6 seconds after last file creates a new group."""
        existing_group = os.path.join(temp_storage, "2023.01.01-10.00.00")
        os.makedirs(existing_group)

        dir_state = DirectoryState(existing_group)
        existing_file = RecordingFile(
            start_time=datetime(2023, 1, 1, 10, 0, 0),
            end_time=datetime(2023, 1, 1, 10, 5, 0),
            file_path=os.path.join(existing_group, "test1.dav"),
            metadata={"path": "/test1.dav"},
        )
        import asyncio

        asyncio.run(dir_state.add_file(existing_file.file_path, existing_file))

        # File starting 6 seconds after last file should create new group
        file_start_time = datetime(2023, 1, 1, 10, 5, 6)

        group_dir = find_group_directory(
            file_start_time, temp_storage, [existing_group]
        )

        expected_new_dir = os.path.join(temp_storage, "2023.01.01-10.05.06")
        assert group_dir == expected_new_dir
        assert os.path.exists(group_dir)


class TestAutoStopRecording:
    """Tests for continuous recording suppression."""

    @pytest.mark.asyncio
    async def test_stop_recording_called_when_camera_is_recording(
        self, temp_storage, mock_config, mock_camera
    ):
        """stop_recording is called when get_recording_status returns True."""
        mock_camera.get_recording_status.return_value = True
        poller = _make_poller(temp_storage, mock_config, mock_camera)
        await poller.discover_work()
        mock_camera.get_recording_status.assert_called_once()
        mock_camera.stop_recording.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_recording_not_called_when_already_stopped(
        self, temp_storage, mock_config, mock_camera
    ):
        """stop_recording is NOT called when recording is already off."""
        mock_camera.get_recording_status.return_value = False
        poller = _make_poller(temp_storage, mock_config, mock_camera)
        await poller.discover_work()
        mock_camera.get_recording_status.assert_called_once()
        mock_camera.stop_recording.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_recording_checked_on_every_poll(
        self, temp_storage, mock_config, mock_camera
    ):
        """Recording status is checked on every poll cycle."""
        mock_camera.get_recording_status.return_value = False
        poller = _make_poller(temp_storage, mock_config, mock_camera)
        await poller.discover_work()
        await poller.discover_work()
        await poller.discover_work()
        assert mock_camera.get_recording_status.call_count == 3

    @pytest.mark.asyncio
    async def test_stop_recording_called_when_recording_resumes(
        self, temp_storage, mock_config, mock_camera
    ):
        """If recording re-enables between polls, it is caught and stopped."""
        mock_camera.get_recording_status.side_effect = [True, False, True]
        poller = _make_poller(temp_storage, mock_config, mock_camera)
        await poller.discover_work()  # recording=True -> stop
        await poller.discover_work()  # recording=False -> skip
        await poller.discover_work()  # recording=True -> stop again
        assert mock_camera.stop_recording.call_count == 2

    @pytest.mark.asyncio
    async def test_stop_recording_called_again_after_reconnect(
        self, temp_storage, mock_config, mock_camera
    ):
        """Recording check resumes after disconnect/reconnect."""
        mock_camera.get_recording_status.return_value = True
        poller = _make_poller(temp_storage, mock_config, mock_camera)

        # First connection
        await poller.discover_work()
        assert mock_camera.stop_recording.call_count == 1

        # Disconnect
        mock_camera.check_availability.return_value = False
        await poller.discover_work()

        # Reconnect
        mock_camera.check_availability.return_value = True
        await poller.discover_work()
        assert mock_camera.stop_recording.call_count == 2

    @pytest.mark.asyncio
    async def test_stop_recording_not_called_when_disabled(
        self, temp_storage, mock_config, mock_camera
    ):
        """Nothing is called when auto_stop_recording is False."""
        mock_camera.config.auto_stop_recording = False
        poller = _make_poller(temp_storage, mock_config, mock_camera)
        await poller.discover_work()
        mock_camera.get_recording_status.assert_not_called()
        mock_camera.stop_recording.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_recording_failure_allows_retry_next_poll(
        self, temp_storage, mock_config, mock_camera
    ):
        """Failed stop is retried on the next poll (no flag blocking retry)."""
        mock_camera.get_recording_status.return_value = True
        mock_camera.stop_recording.return_value = False
        poller = _make_poller(temp_storage, mock_config, mock_camera)
        await poller.discover_work()
        await poller.discover_work()
        assert mock_camera.stop_recording.call_count == 2


class TestHomeRecordingDeletion:
    """Tests for deleting home recordings from the camera with user confirmation."""

    def _setup_home_files(self, mock_camera):
        """Set up connected timeframe and home/field files on mock camera."""
        connected_start = datetime(2023, 1, 1, 10, 0, 0, tzinfo=pytz.utc)
        connected_end = datetime(2023, 1, 1, 10, 10, 0, tzinfo=pytz.utc)
        mock_camera.get_connected_timeframes.return_value = [
            (connected_start, connected_end)
        ]
        mock_camera.get_file_list.return_value = [
            {
                "path": "/home_clip.dav",
                "startTime": "2023-01-01 05:05:00",
                "endTime": "2023-01-01 05:08:00",
            },
            {
                "path": "/field_clip.dav",
                "startTime": "2023-01-01 06:00:00",
                "endTime": "2023-01-01 06:05:00",
            },
        ]

    def _approve_cleanup(self, poller):
        """Write an approved cleanup state file."""
        import json

        state = {"files": [{"path": "/home_clip.dav"}], "approved": True}
        with open(poller._cleanup_state_path, "w") as f:
            json.dump(state, f)

    @pytest.mark.asyncio
    async def test_home_files_deleted_after_user_approves(
        self, temp_storage, mock_config, mock_camera
    ):
        """Files are deleted only after user approves (via state file)."""
        self._setup_home_files(mock_camera)
        mock_camera.delete_files.return_value = 1

        mock_download_processor = Mock()
        mock_download_processor.add_work = AsyncMock()
        poller = _make_poller(
            temp_storage, mock_config, mock_camera, mock_download_processor
        )
        self._approve_cleanup(poller)

        await poller._sync_files_from_camera()

        mock_camera.delete_files.assert_called_once_with(["/home_clip.dav"])

    @pytest.mark.asyncio
    async def test_cleanup_state_written_on_first_discovery(
        self, temp_storage, mock_config, mock_camera
    ):
        """Cleanup state file is written when home files are first found."""
        import json

        self._setup_home_files(mock_camera)

        mock_download_processor = Mock()
        mock_download_processor.add_work = AsyncMock()
        poller = _make_poller(
            temp_storage, mock_config, mock_camera, mock_download_processor
        )

        await poller._sync_files_from_camera()

        # State file should exist with the home file listed
        assert Path(poller._cleanup_state_path).exists()
        with open(poller._cleanup_state_path, "r") as f:
            state = json.load(f)
        assert len(state["files"]) == 1
        assert state["files"][0]["path"] == "/home_clip.dav"
        assert state["approved"] is False

        # delete_files should NOT be called yet
        mock_camera.delete_files.assert_not_called()

    @pytest.mark.asyncio
    async def test_ntfy_notification_sent_on_first_discovery(
        self, temp_storage, mock_config, mock_camera
    ):
        """NTFY notification is sent when home files are first found."""
        self._setup_home_files(mock_camera)

        mock_download_processor = Mock()
        mock_download_processor.add_work = AsyncMock()
        poller = _make_poller(
            temp_storage, mock_config, mock_camera, mock_download_processor
        )
        mock_ntfy = Mock()
        mock_ntfy.send_notification = AsyncMock(return_value=True)
        mock_ntfy.config = mock_config.ntfy
        mock_ntfy.register_response_handler = Mock()
        poller.ntfy_service = mock_ntfy

        await poller._sync_files_from_camera()

        mock_ntfy.send_notification.assert_called_once()
        call_kwargs = mock_ntfy.send_notification.call_args[1]
        assert "Home Recordings Found" in call_kwargs["title"]
        assert "1 recording" in call_kwargs["message"]
        mock_camera.delete_files.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_deletion_when_not_approved(
        self, temp_storage, mock_config, mock_camera
    ):
        """Files are not deleted when cleanup state exists but not approved."""
        import json

        self._setup_home_files(mock_camera)

        mock_download_processor = Mock()
        mock_download_processor.add_work = AsyncMock()
        poller = _make_poller(
            temp_storage, mock_config, mock_camera, mock_download_processor
        )
        # Write state with approved=False (user hasn't responded yet)
        state = {"files": [{"path": "/home_clip.dav"}], "approved": False}
        with open(poller._cleanup_state_path, "w") as f:
            json.dump(state, f)

        await poller._sync_files_from_camera()

        mock_camera.delete_files.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_deletion_when_no_home_files(
        self, temp_storage, mock_config, mock_camera
    ):
        """delete_files is not called when no files overlap connected timeframes."""
        mock_camera.get_connected_timeframes.return_value = []
        mock_camera.get_file_list.return_value = [
            {
                "path": "/field_clip.dav",
                "startTime": "2023-01-01 06:00:00",
                "endTime": "2023-01-01 06:05:00",
            },
        ]

        mock_download_processor = Mock()
        mock_download_processor.add_work = AsyncMock()
        poller = _make_poller(
            temp_storage, mock_config, mock_camera, mock_download_processor
        )

        await poller._sync_files_from_camera()

        mock_camera.delete_files.assert_not_called()

    @pytest.mark.asyncio
    async def test_deletion_failure_does_not_block_processing(
        self, temp_storage, mock_config, mock_camera
    ):
        """delete_files raising an exception does not prevent file sync."""
        self._setup_home_files(mock_camera)
        mock_camera.delete_files.side_effect = Exception("API error")

        mock_download_processor = Mock()
        mock_download_processor.add_work = AsyncMock()
        poller = _make_poller(
            temp_storage, mock_config, mock_camera, mock_download_processor
        )
        self._approve_cleanup(poller)

        await poller._sync_files_from_camera()

        # Field clip should still be processed despite delete failure
        mock_download_processor.add_work.assert_called_once()
        queued_file = mock_download_processor.add_work.call_args[0][0]
        assert "field_clip.dav" in queued_file.file_path

    @pytest.mark.asyncio
    async def test_no_deletion_when_auto_stop_disabled(
        self, temp_storage, mock_config, mock_camera
    ):
        """delete_files is not called when auto_stop_recording is False."""
        mock_camera.config.auto_stop_recording = False
        self._setup_home_files(mock_camera)

        mock_download_processor = Mock()
        mock_download_processor.add_work = AsyncMock()
        poller = _make_poller(
            temp_storage, mock_config, mock_camera, mock_download_processor
        )

        await poller._sync_files_from_camera()

        mock_camera.delete_files.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleanup_state_cleared_on_disconnect(
        self, temp_storage, mock_config, mock_camera
    ):
        """Cleanup state file is removed when camera disconnects."""
        import json

        poller = _make_poller(temp_storage, mock_config, mock_camera)
        # Write a state file
        with open(poller._cleanup_state_path, "w") as f:
            json.dump({"files": [{"path": "/x.dav"}], "approved": False}, f)

        # Camera disconnects
        mock_camera.check_availability.return_value = False
        await poller.discover_work()

        assert not Path(poller._cleanup_state_path).exists()

    @pytest.mark.asyncio
    async def test_cleanup_state_cleared_after_successful_delete(
        self, temp_storage, mock_config, mock_camera
    ):
        """Cleanup state file is removed after successful deletion."""
        self._setup_home_files(mock_camera)
        mock_camera.delete_files.return_value = 1

        mock_download_processor = Mock()
        mock_download_processor.add_work = AsyncMock()
        poller = _make_poller(
            temp_storage, mock_config, mock_camera, mock_download_processor
        )
        self._approve_cleanup(poller)

        await poller._sync_files_from_camera()

        assert not Path(poller._cleanup_state_path).exists()

    @pytest.mark.asyncio
    async def test_handle_ntfy_response_yes_sets_approved(
        self, temp_storage, mock_config, mock_camera
    ):
        """NTFY 'yes' response sets approved=True in state file."""
        import json

        poller = _make_poller(temp_storage, mock_config, mock_camera)
        mock_ntfy = Mock()
        mock_ntfy.unregister_response_handler = Mock()
        poller.ntfy_service = mock_ntfy

        # Write initial state
        with open(poller._cleanup_state_path, "w") as f:
            json.dump({"files": [{"path": "/x.dav"}], "approved": False}, f)

        await poller._handle_deletion_response("yes, delete home recordings")

        with open(poller._cleanup_state_path, "r") as f:
            state = json.load(f)
        assert state["approved"] is True

    @pytest.mark.asyncio
    async def test_handle_ntfy_response_no_clears_state(
        self, temp_storage, mock_config, mock_camera
    ):
        """NTFY 'no' response clears the cleanup state file."""
        import json

        poller = _make_poller(temp_storage, mock_config, mock_camera)
        mock_ntfy = Mock()
        mock_ntfy.unregister_response_handler = Mock()
        poller.ntfy_service = mock_ntfy

        # Write initial state
        with open(poller._cleanup_state_path, "w") as f:
            json.dump({"files": [{"path": "/x.dav"}], "approved": False}, f)

        await poller._handle_deletion_response("no, keep home recordings")

        assert not Path(poller._cleanup_state_path).exists()


class TestUnplugNotification:
    """Tests for unplug-notification-on-downloads-complete feature."""

    @pytest.mark.asyncio
    async def test_notification_sent_when_downloads_complete(
        self, temp_storage, mock_config, mock_camera
    ):
        """Notification sent when camera connected, no new files, download queue empty."""
        mock_config.ntfy.enabled = True

        # Simulate: first poll finds files, second poll finds none
        mock_camera.get_file_list.side_effect = [
            [
                {
                    "path": "/test1.dav",
                    "startTime": "2023-01-01 10:00:00",
                    "endTime": "2023-01-01 10:05:00",
                }
            ],
            [],
        ]
        mock_download_processor = Mock()
        mock_download_processor.get_queue_size = Mock(return_value=0)
        mock_download_processor._in_progress_item = None
        mock_download_processor.add_work = AsyncMock()

        poller = _make_poller(
            temp_storage, mock_config, mock_camera, mock_download_processor
        )
        poller.ntfy_service = Mock()
        poller.ntfy_service.send_notification = AsyncMock(return_value=True)

        # First poll finds files -> no notification
        await poller.discover_work()
        poller.ntfy_service.send_notification.assert_not_called()

        # Second poll finds no files, queue empty -> notification sent
        await poller.discover_work()
        poller.ntfy_service.send_notification.assert_called_once()
        call_kwargs = poller.ntfy_service.send_notification.call_args[1]
        assert "Downloads Complete" in call_kwargs["title"]

    @pytest.mark.asyncio
    async def test_notification_not_sent_when_queue_has_items(
        self, temp_storage, mock_config, mock_camera
    ):
        """No notification when download queue still has items."""
        mock_config.ntfy.enabled = True
        mock_download_processor = Mock()
        mock_download_processor.get_queue_size = Mock(return_value=2)
        mock_download_processor._in_progress_item = None
        mock_download_processor.add_work = AsyncMock()

        poller = _make_poller(
            temp_storage, mock_config, mock_camera, mock_download_processor
        )
        poller.ntfy_service = Mock()
        poller.ntfy_service.send_notification = AsyncMock(return_value=True)
        poller._last_poll_found_files = False

        await poller.discover_work()
        poller.ntfy_service.send_notification.assert_not_called()

    @pytest.mark.asyncio
    async def test_notification_not_sent_when_item_in_progress(
        self, temp_storage, mock_config, mock_camera
    ):
        """No notification when a download is currently in progress."""
        mock_config.ntfy.enabled = True
        mock_download_processor = Mock()
        mock_download_processor.get_queue_size = Mock(return_value=0)
        mock_download_processor._in_progress_item = Mock()  # something in progress
        mock_download_processor.add_work = AsyncMock()

        poller = _make_poller(
            temp_storage, mock_config, mock_camera, mock_download_processor
        )
        poller.ntfy_service = Mock()
        poller.ntfy_service.send_notification = AsyncMock(return_value=True)
        poller._last_poll_found_files = False

        await poller.discover_work()
        poller.ntfy_service.send_notification.assert_not_called()

    @pytest.mark.asyncio
    async def test_notification_not_sent_when_files_still_being_found(
        self, temp_storage, mock_config, mock_camera
    ):
        """No notification when the last poll found files."""
        mock_config.ntfy.enabled = True
        mock_camera.get_file_list.return_value = [
            {
                "path": "/test1.dav",
                "startTime": "2023-01-01 10:00:00",
                "endTime": "2023-01-01 10:05:00",
            }
        ]
        mock_download_processor = Mock()
        mock_download_processor.get_queue_size = Mock(return_value=0)
        mock_download_processor._in_progress_item = None
        mock_download_processor.add_work = AsyncMock()

        poller = _make_poller(
            temp_storage, mock_config, mock_camera, mock_download_processor
        )
        poller.ntfy_service = Mock()
        poller.ntfy_service.send_notification = AsyncMock(return_value=True)

        await poller.discover_work()
        poller.ntfy_service.send_notification.assert_not_called()

    @pytest.mark.asyncio
    async def test_notification_sent_only_once(
        self, temp_storage, mock_config, mock_camera
    ):
        """Notification is only sent once per connection session."""
        mock_config.ntfy.enabled = True
        poller = _make_poller(temp_storage, mock_config, mock_camera)
        poller.ntfy_service = Mock()
        poller.ntfy_service.send_notification = AsyncMock(return_value=True)
        poller._last_poll_found_files = False

        await poller.discover_work()
        await poller.discover_work()
        await poller.discover_work()
        poller.ntfy_service.send_notification.assert_called_once()

    @pytest.mark.asyncio
    async def test_notification_not_sent_when_camera_disconnected(
        self, temp_storage, mock_config, mock_camera
    ):
        """No notification when camera is not connected."""
        mock_config.ntfy.enabled = True
        mock_camera.is_connected = False
        mock_camera.check_availability.return_value = False

        poller = _make_poller(temp_storage, mock_config, mock_camera)
        poller.ntfy_service = Mock()
        poller.ntfy_service.send_notification = AsyncMock(return_value=True)
        poller._last_poll_found_files = False

        await poller.discover_work()
        poller.ntfy_service.send_notification.assert_not_called()

    @pytest.mark.asyncio
    async def test_notification_not_sent_when_disabled(
        self, temp_storage, mock_config, mock_camera
    ):
        """No notification when unplug_notification is False."""
        mock_config.ntfy.unplug_notification = False
        poller = _make_poller(temp_storage, mock_config, mock_camera)
        poller.ntfy_service = Mock()
        poller.ntfy_service.send_notification = AsyncMock(return_value=True)
        poller._last_poll_found_files = False

        await poller.discover_work()
        poller.ntfy_service.send_notification.assert_not_called()

    @pytest.mark.asyncio
    async def test_notification_not_sent_when_no_ntfy_service(
        self, temp_storage, mock_config, mock_camera
    ):
        """No error when ntfy_service is None."""
        mock_config.ntfy.enabled = True
        poller = _make_poller(temp_storage, mock_config, mock_camera)
        poller._last_poll_found_files = False

        # Should not raise
        await poller.discover_work()
        assert poller._unplug_notified is True

    @pytest.mark.asyncio
    async def test_notification_resets_on_reconnect(
        self, temp_storage, mock_config, mock_camera
    ):
        """Notification flag resets after disconnect so it fires again on reconnect."""
        mock_config.ntfy.enabled = True
        poller = _make_poller(temp_storage, mock_config, mock_camera)
        poller.ntfy_service = Mock()
        poller.ntfy_service.send_notification = AsyncMock(return_value=True)
        poller._last_poll_found_files = False

        # First session: notification sent
        await poller.discover_work()
        assert poller.ntfy_service.send_notification.call_count == 1

        # Disconnect
        mock_camera.check_availability.return_value = False
        mock_camera.is_connected = False
        await poller.discover_work()

        # Reconnect with no files
        mock_camera.check_availability.return_value = True
        mock_camera.is_connected = True
        # First poll after reconnect: _last_poll_found_files resets to True on disconnect
        await poller.discover_work()
        # _last_poll_found_files is now False (no files found)
        await poller.discover_work()
        assert poller.ntfy_service.send_notification.call_count == 2
