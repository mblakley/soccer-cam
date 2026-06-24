"""Tests for the DownloadProcessor."""

import os
import tempfile
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest

from video_grouper.models import DirectoryState, RecordingFile
from video_grouper.task_processors.download_processor import DownloadProcessor
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

# The autouse ``mock_file_system`` fixture in conftest.py globally stubs
# os.makedirs / os.path.exists / os.path.getsize for the whole suite. The
# truncated-vs-full download tests below need REAL filesystem behavior (they
# create actual temp files and rely on getsize to distinguish a 270-byte
# truncated file from a 1MB complete one). Capture the genuine functions at
# import time — before any patching — so the ``real_filesystem`` fixture can
# restore them for those tests.
_REAL_MAKEDIRS = os.makedirs
_REAL_EXISTS = os.path.exists
_REAL_GETSIZE = os.path.getsize
_REAL_ACCESS = os.access


@pytest.fixture
def temp_storage():
    """Create a temporary storage directory for tests."""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def real_filesystem(mock_file_system):
    """Restore genuine os filesystem calls over the autouse mock_file_system.

    Tests that exercise DirectoryState against real temp files need
    os.makedirs/exists/getsize/access to actually hit disk.
    """
    mock_file_system["makedirs"].side_effect = _REAL_MAKEDIRS
    mock_file_system["exists"].side_effect = _REAL_EXISTS
    mock_file_system["getsize"].side_effect = _REAL_GETSIZE
    mock_file_system["access"].side_effect = _REAL_ACCESS
    yield


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
        mock_video_processor = Mock()
        processor = DownloadProcessor(
            temp_storage, mock_config, mock_camera, mock_video_processor
        )

        assert processor.storage_path == temp_storage
        assert processor.config == mock_config
        assert processor.camera == mock_camera
        assert processor.video_processor == mock_video_processor

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
        mock_dir_state_instance.get_file_by_path = Mock(return_value=recording_file)
        mock_dir_state_instance.is_file_fully_downloaded = Mock(return_value=True)
        mock_dir_state_instance.get_incomplete_downloads = Mock(return_value=[])
        mock_dir_state_instance.update_file_state = AsyncMock()
        mock_dir_state_instance.is_ready_for_combining = Mock(return_value=True)
        mock_directory_state.return_value = mock_dir_state_instance

        # Mock successful download
        mock_camera.download_file.return_value = True

        # Create mock video processor
        mock_video_processor = Mock()
        mock_video_processor.add_work = AsyncMock()

        processor = DownloadProcessor(
            temp_storage, mock_config, mock_camera, mock_video_processor
        )

        # Process the download
        await processor.process_item(recording_file)

        mock_camera.download_file.assert_called_once_with(
            file_path="/test.dav", local_path="/test/group/test.dav"
        )
        mock_video_processor.add_work.assert_called_once()

        # Verify combine task was queued
        queued_task = mock_video_processor.add_work.call_args[0][0]
        assert isinstance(queued_task, CombineTask)
        assert queued_task.group_dir == os.path.dirname(recording_file.file_path)

        # Verify file status was updated
        mock_dir_state_instance.update_file_state.assert_any_call(
            "/test/group/test.dav", status="downloading"
        )
        mock_dir_state_instance.update_file_state.assert_any_call(
            "/test/group/test.dav", status="downloaded"
        )

    @pytest.mark.asyncio
    @patch("video_grouper.task_processors.download_processor.DirectoryState")
    async def test_successful_download_not_ready_for_combining(
        self, mock_directory_state, temp_storage, mock_config, mock_camera
    ):
        """Test successful file download when group is not ready for combining."""
        # Create recording file
        recording_file = RecordingFile(
            start_time=datetime(2023, 1, 1, 10, 0, 0),
            end_time=datetime(2023, 1, 1, 10, 5, 0),
            file_path="/test/group/test.dav",
            metadata={"path": "/test.dav"},
        )

        # Mock DirectoryState
        mock_dir_state_instance = Mock()
        mock_dir_state_instance.get_file_by_path = Mock(return_value=recording_file)
        mock_dir_state_instance.is_file_fully_downloaded = Mock(return_value=True)
        mock_dir_state_instance.get_incomplete_downloads = Mock(return_value=[])
        mock_dir_state_instance.update_file_state = AsyncMock()
        mock_dir_state_instance.is_ready_for_combining = Mock(return_value=False)
        mock_directory_state.return_value = mock_dir_state_instance

        # Mock successful download
        mock_camera.download_file.return_value = True

        # Create mock video processor
        mock_video_processor = Mock()
        mock_video_processor.add_work = AsyncMock()

        processor = DownloadProcessor(
            temp_storage, mock_config, mock_camera, mock_video_processor
        )

        # Process the download
        await processor.process_item(recording_file)

        mock_camera.download_file.assert_called_once_with(
            file_path="/test.dav", local_path="/test/group/test.dav"
        )

        # Verify no combine task was queued since group is not ready
        mock_video_processor.add_work.assert_not_called()

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

        mock_video_processor = Mock()
        processor = DownloadProcessor(
            temp_storage, mock_config, mock_camera, mock_video_processor
        )

        # Process the download -- code re-raises after marking download_failed
        with pytest.raises(RuntimeError, match="Download failed"):
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

        mock_video_processor = Mock()
        processor = DownloadProcessor(
            temp_storage, mock_config, mock_camera, mock_video_processor
        )

        # Process the download -- exception is re-raised after updating state
        with pytest.raises(Exception, match="Download error"):
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
        mock_dir_state_instance.get_file_by_path = Mock(return_value=recording_file)
        mock_dir_state_instance.is_file_fully_downloaded = Mock(return_value=True)
        mock_dir_state_instance.get_incomplete_downloads = Mock(return_value=[])
        mock_dir_state_instance.update_file_state = AsyncMock()
        mock_directory_state.return_value = mock_dir_state_instance

        # Mock successful download
        mock_camera.download_file.return_value = True

        mock_video_processor = Mock()
        processor = DownloadProcessor(
            temp_storage, mock_config, mock_camera, mock_video_processor
        )
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

    @pytest.mark.asyncio
    async def test_truncated_download_is_requeued_not_combined(
        self, temp_storage, mock_config, real_filesystem
    ):
        """A short read that produces a truncated file must NOT be marked
        fully-downloaded: the file is left pending, re-queued for download,
        and the combine task is never handed off."""
        group_dir = os.path.join(temp_storage, "2023.01.01-10.00.00")
        os.makedirs(group_dir, exist_ok=True)
        file_path = os.path.join(group_dir, "RecM09_x.mp4")

        item = RecordingFile(
            start_time=datetime(2023, 1, 1, 10, 0, 0),
            end_time=datetime(2023, 1, 1, 10, 5, 0),
            file_path=file_path,
            metadata={"path": "/cam/RecM09_x.mp4", "size": 1_000_000},
        )

        # Pre-populate the on-disk state with a real DirectoryState (no mock).
        ds = DirectoryState(group_dir)
        await ds.add_file(item.file_path, item)

        async def _write_truncated(file_path, local_path):
            # Server served only a partial first segment — 270 bytes.
            with open(local_path, "wb") as f:
                f.write(b"\x00" * 270)
            return True

        camera = Mock()
        camera.name = "reolink"
        camera.check_availability = Mock(return_value=True)
        camera.download_file = AsyncMock(side_effect=_write_truncated)

        video_processor = Mock()
        video_processor.add_work = AsyncMock()

        processor = DownloadProcessor(
            temp_storage, mock_config, camera, video_processor
        )
        # Capture the re-queue without re-running the queue machinery.
        processor.add_work = AsyncMock()

        await processor.process_item(item)

        # download_file invoked the way process_item calls it.
        camera.download_file.assert_called_once_with(
            file_path=item.metadata["path"], local_path=file_path
        )

        # Status rolled back to pending so the truncated file is fetched again.
        reloaded = DirectoryState(group_dir)
        assert reloaded.get_file_by_path(item.file_path).status == "pending"

        # The item was re-queued for download exactly once.
        processor.add_work.assert_awaited_once_with(item)

        # A truncated file must never reach the combine step.
        video_processor.add_work.assert_not_called()

    @pytest.mark.asyncio
    async def test_full_download_is_downloaded_and_combined(
        self, temp_storage, mock_config, real_filesystem
    ):
        """A full-size download is marked 'downloaded' and the group is handed
        off to the video processor for combining."""
        group_dir = os.path.join(temp_storage, "2023.01.01-11.00.00")
        os.makedirs(group_dir, exist_ok=True)
        file_path = os.path.join(group_dir, "RecM09_x.mp4")

        item = RecordingFile(
            start_time=datetime(2023, 1, 1, 11, 0, 0),
            end_time=datetime(2023, 1, 1, 11, 5, 0),
            file_path=file_path,
            metadata={"path": "/cam/RecM09_x.mp4", "size": 1_000_000},
        )

        ds = DirectoryState(group_dir)
        await ds.add_file(item.file_path, item)

        async def _write_full(file_path, local_path):
            with open(local_path, "wb") as f:
                f.write(b"\x00" * 1_000_000)
            return True

        camera = Mock()
        camera.name = "reolink"
        camera.check_availability = Mock(return_value=True)
        camera.download_file = AsyncMock(side_effect=_write_full)

        video_processor = Mock()
        video_processor.add_work = AsyncMock()

        processor = DownloadProcessor(
            temp_storage, mock_config, camera, video_processor
        )
        processor.add_work = AsyncMock()

        await processor.process_item(item)

        camera.download_file.assert_called_once_with(
            file_path=item.metadata["path"], local_path=file_path
        )

        # Status is 'downloaded' once the full file is on disk.
        reloaded = DirectoryState(group_dir)
        assert reloaded.get_file_by_path(item.file_path).status == "downloaded"

        # A complete file is never re-queued for download.
        processor.add_work.assert_not_called()

        # The group is handed off to the video processor for combining.
        video_processor.add_work.assert_awaited_once()
        queued_task = video_processor.add_work.call_args[0][0]
        assert isinstance(queued_task, CombineTask)
        assert queued_task.group_dir == group_dir

    def test_get_item_key_recording_file(self, temp_storage, mock_config, mock_camera):
        """Test getting unique key for RecordingFile."""
        mock_video_processor = Mock()
        processor = DownloadProcessor(
            temp_storage, mock_config, mock_camera, mock_video_processor
        )

        recording_file = RecordingFile(
            start_time=datetime(2023, 1, 1, 10, 0, 0),
            end_time=datetime(2023, 1, 1, 10, 5, 0),
            file_path="/test/path/test.dav",
            metadata={"path": "/test.dav"},
        )

        key = processor.get_item_key(recording_file)
        assert key == "recording:/test/path/test.dav"
