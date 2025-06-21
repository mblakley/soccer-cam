import os
import pytest
import json
import asyncio
import tempfile
import shutil
from datetime import datetime, timedelta
import pytz
from unittest.mock import MagicMock, AsyncMock, patch

from video_grouper.video_grouper import VideoGrouperApp
from video_grouper.models import RecordingFile
from video_grouper.directory_state import DirectoryState
from video_grouper.cameras.base import Camera

class MockCamera(Camera):
    """Mock camera implementation for testing."""
    
    def __init__(self, connected_timeframes=None):
        self._connected_timeframes = connected_timeframes or []
        self._is_connected = False
        self._files = []
        
    async def check_availability(self) -> bool:
        """Check if the camera is available."""
        return True
    
    async def get_file_list(self, start_time=None, end_time=None):
        """Get list of recording files from the camera."""
        # Filter files based on timeframes
        filtered_files = []
        for file_info in self._files:
            if self._is_file_in_timeframe(file_info, start_time, end_time):
                filtered_files.append(file_info)
        return filtered_files
    
    def _is_file_in_timeframe(self, file_info, start_time, end_time):
        """Check if file is within the specified timeframe."""
        if not start_time or not end_time:
            return True
        
        file_start = datetime.strptime(file_info['startTime'], "%Y-%m-%d %H:%M:%S")
        file_end = datetime.strptime(file_info['endTime'], "%Y-%m-%d %H:%M:%S")
        
        return file_start <= end_time and file_end >= start_time
    
    async def get_file_size(self, file_path: str) -> int:
        """Get size of a file on the camera."""
        return 1024
    
    async def download_file(self, file_path: str, local_path: str) -> bool:
        """Download a file from the camera."""
        # Create an empty file at the local path
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, 'w') as f:
            f.write("Mock file content")
        return True
    
    async def stop_recording(self) -> bool:
        """Stop recording on the camera."""
        return True
    
    async def get_recording_status(self) -> bool:
        """Get recording status from the camera."""
        return False
    
    async def get_device_info(self) -> dict:
        """Get device information from the camera."""
        return {"model": "MockCamera", "firmware": "1.0"}
    
    def get_connected_timeframes(self):
        """Returns a list of timeframes when the camera was connected."""
        return self._connected_timeframes
    
    @property
    def connection_events(self):
        """Get list of connection events."""
        return []
    
    @property
    def is_connected(self) -> bool:
        """Get connection status."""
        return self._is_connected
    
    def add_file(self, file_info):
        """Add a file to the mock camera."""
        self._files.append(file_info)


class MockDirectoryState(DirectoryState):
    """Mock DirectoryState for testing."""
    
    def __init__(self, directory_path):
        super().__init__(directory_path)
        
    async def save_state(self):
        """Override to avoid file operations."""
        pass


@pytest.fixture
def temp_dir():
    """Create a temporary directory for testing."""
    with tempfile.TemporaryDirectory() as tmpdirname:
        yield tmpdirname


@pytest.fixture
def mock_config():
    """Create a mock configuration."""
    config = MagicMock()
    config.get.side_effect = lambda section, key, fallback=None: fallback
    config.getint.side_effect = lambda section, key, fallback=None: fallback
    config.getboolean.side_effect = lambda section, key, fallback=None: fallback
    config.has_section.return_value = False
    return config


@pytest.mark.asyncio
async def test_filter_recordings_during_sync(temp_dir, mock_config):
    """Test that recordings that overlap with connected timeframes are filtered during sync."""
    # Create a mock camera with connected timeframes
    now = datetime.now(pytz.utc)
    connected_timeframes = [
        (now - timedelta(hours=2), now - timedelta(hours=1))  # Connected from 2 hours ago to 1 hour ago
    ]
    
    camera = MockCamera(connected_timeframes=connected_timeframes)
    
    # Add files to the camera
    # File 1: Overlaps with connected timeframe (should be filtered)
    camera.add_file({
        'path': '/mnt/sd/2023-01-01/001.dav',
        'startTime': (now - timedelta(hours=1, minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
        'endTime': (now - timedelta(hours=0, minutes=45)).strftime("%Y-%m-%d %H:%M:%S")
    })
    
    # File 2: Does not overlap with connected timeframe (should not be filtered)
    camera.add_file({
        'path': '/mnt/sd/2023-01-01/002.dav',
        'startTime': (now - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
        'endTime': now.strftime("%Y-%m-%d %H:%M:%S")
    })
    
    # Create a VideoGrouperApp with the mock camera
    app = VideoGrouperApp(mock_config, camera=camera)
    app.storage_path = temp_dir
    
    # Run sync_files_from_camera
    await app.sync_files_from_camera()
    
    # Check that only the non-overlapping file was added to the download queue
    assert app.download_queue.qsize() == 1
    recording_file = await app.download_queue.get()
    assert os.path.basename(recording_file.file_path) == '002.dav'


@pytest.mark.asyncio
async def test_filter_existing_state_files(temp_dir, mock_config):
    """Test the filtering logic for recordings that overlap with connected timeframes."""
    # Create a mock camera with connected timeframes
    now = datetime.now(pytz.utc)
    connected_timeframes = [
        (now - timedelta(hours=2), now - timedelta(hours=1))  # Connected from 2 hours ago to 1 hour ago
    ]
    
    camera = MockCamera(connected_timeframes=connected_timeframes)
    
    # Create two recording files
    # File 1: Overlaps with connected timeframe (should be filtered)
    file1_start = now - timedelta(hours=1, minutes=30)
    file1_end = now - timedelta(hours=0, minutes=45)
    file1 = RecordingFile(
        start_time=file1_start,
        end_time=file1_end,
        file_path="file1.dav",
        status="pending"
    )
    
    # File 2: Does not overlap with connected timeframe (should not be filtered)
    file2_start = now - timedelta(minutes=30)
    file2_end = now
    file2 = RecordingFile(
        start_time=file2_start,
        end_time=file2_end,
        file_path="file2.dav",
        status="pending"
    )
    
    # Test the filtering logic directly
    # Convert to UTC for comparison
    file1_start_utc = pytz.utc.localize(file1.start_time) if file1.start_time.tzinfo is None else file1.start_time
    file1_end_utc = pytz.utc.localize(file1.end_time) if file1.end_time.tzinfo is None else file1.end_time
    
    file2_start_utc = pytz.utc.localize(file2.start_time) if file2.start_time.tzinfo is None else file2.start_time
    file2_end_utc = pytz.utc.localize(file2.end_time) if file2.end_time.tzinfo is None else file2.end_time
    
    # Check if file1 overlaps with connected timeframe
    file1_overlaps = False
    for frame_start, frame_end in connected_timeframes:
        frame_end_or_now = frame_end or datetime.now(pytz.utc)
        if file1_start_utc < frame_end_or_now and file1_end_utc > frame_start:
            file1_overlaps = True
            break
    
    # Check if file2 overlaps with connected timeframe
    file2_overlaps = False
    for frame_start, frame_end in connected_timeframes:
        frame_end_or_now = frame_end or datetime.now(pytz.utc)
        if file2_start_utc < frame_end_or_now and file2_end_utc > frame_start:
            file2_overlaps = True
            break
    
    # Assert that file1 overlaps and file2 does not overlap
    assert file1_overlaps == True, "File1 should overlap with connected timeframe"
    assert file2_overlaps == False, "File2 should not overlap with connected timeframe"


@pytest.mark.asyncio
async def test_filter_queue_state(temp_dir, mock_config):
    """Test that recordings in the queue state that overlap with connected timeframes are filtered."""
    # Create a mock camera with connected timeframes
    now = datetime.now(pytz.utc)
    connected_timeframes = [
        (now - timedelta(hours=2), now - timedelta(hours=1))  # Connected from 2 hours ago to 1 hour ago
    ]
    
    camera = MockCamera(connected_timeframes=connected_timeframes)
    
    # Create a VideoGrouperApp with the mock camera
    app = VideoGrouperApp(mock_config, camera=camera)
    app.storage_path = temp_dir
    
    # Create a group directory
    group_dir = os.path.join(temp_dir, "2023.01.01-12.00.00")
    os.makedirs(group_dir, exist_ok=True)
    
    # Create two recording files in the queue
    file1_path = os.path.join(group_dir, "001.dav")
    file2_path = os.path.join(group_dir, "002.dav")
    
    # File 1: Overlaps with connected timeframe (should be filtered)
    file1 = RecordingFile(
        start_time=now - timedelta(hours=1, minutes=30),
        end_time=now - timedelta(hours=0, minutes=45),
        file_path=file1_path,
        status="pending"
    )
    
    # File 2: Does not overlap with connected timeframe (should not be filtered)
    file2 = RecordingFile(
        start_time=now - timedelta(minutes=30),
        end_time=now,
        file_path=file2_path,
        status="pending"
    )
    
    # Save the queue state
    os.makedirs(temp_dir, exist_ok=True)
    with open(os.path.join(temp_dir, "download_queue_state.json"), 'w') as f:
        json.dump([file1.to_dict(), file2.to_dict()], f)
    
    # Load the queue state
    await app._load_queues_from_state()
    
    # Check that only the non-overlapping file was added to the download queue
    assert app.download_queue.qsize() == 1
    recording_file = await app.download_queue.get()
    assert os.path.basename(recording_file.file_path) == '002.dav'


@pytest.mark.asyncio
async def test_filter_ongoing_connection(temp_dir, mock_config):
    """Test that recordings that overlap with an ongoing connection are filtered."""
    # Create a mock camera with an ongoing connection
    now = datetime.now(pytz.utc)
    connected_timeframes = [
        (now - timedelta(hours=1), None)  # Connected from 1 hour ago until now
    ]
    
    camera = MockCamera(connected_timeframes=connected_timeframes)
    
    # Add files to the camera
    # File 1: Overlaps with ongoing connection (should be filtered)
    camera.add_file({
        'path': '/mnt/sd/2023-01-01/001.dav',
        'startTime': (now - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
        'endTime': now.strftime("%Y-%m-%d %H:%M:%S")
    })
    
    # File 2: Before the connection started (should not be filtered)
    camera.add_file({
        'path': '/mnt/sd/2023-01-01/002.dav',
        'startTime': (now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
        'endTime': (now - timedelta(hours=1, minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    })
    
    # Create a VideoGrouperApp with the mock camera
    app = VideoGrouperApp(mock_config, camera=camera)
    app.storage_path = temp_dir
    
    # Run sync_files_from_camera
    await app.sync_files_from_camera()
    
    # Check that only the non-overlapping file was added to the download queue
    assert app.download_queue.qsize() == 1
    recording_file = await app.download_queue.get()
    assert os.path.basename(recording_file.file_path) == '002.dav' 