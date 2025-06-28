import asyncio
import os
import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch, call

import pytest
import pytest_asyncio
from configparser import ConfigParser

from video_grouper.video_grouper import VideoGrouperApp
from video_grouper.models import ConvertTask, CombineTask, TrimTask, YouTubeUploadTask, MatchInfo, RecordingFile

@pytest.fixture
def temp_storage_path(tmp_path):
    """Fixture for a temporary test directory."""
    storage_path = tmp_path / "shared_data"
    storage_path.mkdir(exist_ok=True)
    yield str(storage_path)

@pytest.fixture
def mock_config(temp_storage_path):
    """Fixture for a mock config."""
    config = ConfigParser()
    config['STORAGE'] = {'path': temp_storage_path}
    config['CAMERA'] = {'type': 'dahua', 'device_ip': '1.1.1.1', 'username': 'admin', 'password': 'password'}
    config['APP'] = {'check_interval_seconds': '1'}
    config['YOUTUBE'] = {'enabled': 'true'}
    config['NTFY'] = {'enabled': 'true', 'server': 'http://ntfy.sh', 'topic': 'test'}
    return config

@pytest.fixture
def mock_camera():
    """Fixture for a mock camera."""
    camera = MagicMock()
    # Return connected timeframes that don't overlap with our test files
    # Our test files are from 2023-01-01, so use a different date range
    from datetime import datetime
    import pytz
    
    # Connected timeframe that doesn't overlap with test files (different day)
    connected_start = datetime(2023, 1, 2, 10, 0, 0, tzinfo=pytz.utc)
    connected_end = datetime(2023, 1, 2, 11, 0, 0, tzinfo=pytz.utc)
    camera.get_connected_timeframes = MagicMock(return_value=[(connected_start, connected_end)])
    return camera

@pytest_asyncio.fixture
async def app_instance(mock_config, temp_storage_path, mock_camera):
    """Fixture for a VideoGrouperApp instance."""
    with patch('video_grouper.video_grouper.TeamSnapAPI', MagicMock()), \
         patch('video_grouper.video_grouper.PlayMetricsAPI', MagicMock()), \
         patch('video_grouper.video_grouper.NtfyAPI') as mock_ntfy:
        
        mock_ntfy_instance = mock_ntfy.return_value
        mock_ntfy_instance.enabled = True
        
        # Ensure the storage path exists before creating the app
        os.makedirs(temp_storage_path, exist_ok=True)
        
        # We need to make sure the app's path is the temp path,
        # overriding the one set during initialization from the config.
        app = VideoGrouperApp(config=mock_config, camera=mock_camera)
        app.storage_path = temp_storage_path
        
        app.ntfy_api = mock_ntfy_instance
        app.poll_interval = 0.01 # speed up polling for tests

        # Mock queues to inspect them easily
        app.download_queue = asyncio.Queue()
        app.ffmpeg_queue = asyncio.Queue()
        app.queued_for_download = set()
        app.queued_for_ffmpeg = set()
        
        yield app
        
        # This shutdown logic might not be strictly necessary for these tests,
        # but it's good practice for more complex scenarios.
        if hasattr(app, '_shutdown_event') and not app._shutdown_event.is_set():
            # In a real scenario, you'd call app.shutdown(), but we mock dependencies
            # so a simple event set might suffice or be irrelevant.
            pass


def create_group_dir_with_state(storage_path, group_name, state_data, files_to_create=None):
    """Helper to create a group directory with a state.json file."""
    # Use pathlib for more robust path handling
    storage_path_obj = Path(storage_path)
    storage_path_obj.mkdir(parents=True, exist_ok=True)
    
    group_dir_obj = storage_path_obj / group_name
    group_dir_obj.mkdir(parents=True, exist_ok=True)
    
    state_file_obj = group_dir_obj / 'state.json'
    state_file_obj.write_text(json.dumps(state_data, indent=2))
    
    if files_to_create:
        for f in files_to_create:
            (group_dir_obj / f).touch()

    return str(group_dir_obj)

@pytest.mark.asyncio
async def test_poll_for_downloaded_file(app_instance: VideoGrouperApp):
    """Test that a 'downloaded' file gets a ConvertTask."""
    group_name = "2023.01.01-10.00.00"  # Use valid group directory format
    file_path = os.path.join(app_instance.storage_path, group_name, "video1.dav")
    state_data = {
        "status": "downloading",
        "files": {
            file_path: {
                "start_time": "2023-01-01 10:00:00", "end_time": "2023-01-01 10:05:00",
                "file_path": file_path, "metadata": {}, "status": "downloaded", "skip": False
            }
        }
    }
    create_group_dir_with_state(app_instance.storage_path, group_name, state_data)

    await app_instance._audit_storage_directory()

    task = await asyncio.wait_for(app_instance.ffmpeg_queue.get(), timeout=1)
    assert isinstance(task, ConvertTask)
    assert task.item_path == file_path

@pytest.mark.asyncio
async def test_poll_for_pending_file(app_instance: VideoGrouperApp):
    """Test that a 'pending' file gets re-added to download queue."""
    group_name = "2023.01.01-11.00.00"  # Use valid group directory format
    file_path = os.path.join(app_instance.storage_path, group_name, "video2.dav")
    state_data = {
        "status": "new",
        "files": {
            file_path: {
                "start_time": "2023-01-01 11:00:00", "end_time": "2023-01-01 11:05:00",
                "file_path": file_path, "metadata": {}, "status": "pending", "skip": False
            }
        }
    }
    create_group_dir_with_state(app_instance.storage_path, group_name, state_data)

    await app_instance._audit_storage_directory()

    rec_file = await asyncio.wait_for(app_instance.download_queue.get(), timeout=1)
    assert isinstance(rec_file, RecordingFile)
    assert rec_file.file_path == file_path

@pytest.mark.asyncio
async def test_poll_for_failed_conversion(app_instance: VideoGrouperApp):
    """Test that a 'conversion_failed' file gets a ConvertTask."""
    group_name = "2023.01.01-12.00.00"  # Use valid group directory format
    file_path = os.path.join(app_instance.storage_path, group_name, "video3.dav")
    state_data = {
        "status": "converting",
        "files": {
            file_path: {
                "start_time": "2023-01-01 12:00:00", "end_time": "2023-01-01 12:05:00",
                "file_path": file_path, "metadata": {}, "status": "conversion_failed", "skip": False
            }
        }
    }
    create_group_dir_with_state(app_instance.storage_path, group_name, state_data)

    await app_instance._audit_storage_directory()

    task = await asyncio.wait_for(app_instance.ffmpeg_queue.get(), timeout=1)
    assert isinstance(task, ConvertTask)
    assert task.item_path == file_path

@pytest.mark.asyncio
async def test_poll_for_combining(app_instance: VideoGrouperApp):
    """Test that a group with all files 'converted' gets a CombineTask."""
    group_name = "2023.01.01-13.00.00"  # Use valid group directory format
    group_dir = os.path.join(app_instance.storage_path, group_name)
    
    # Create a state file that shows all files are converted and ready for combining
    state_data = {
        "status": "converting",
        "files": {
            os.path.join(group_dir, "video1.dav"): {
                "start_time": "2023-01-01 13:00:00", 
                "end_time": "2023-01-01 13:05:00",
                "file_path": os.path.join(group_dir, "video1.dav"), 
                "metadata": {}, 
                "status": "converted", 
                "skip": False
            },
            os.path.join(group_dir, "video2.dav"): {
                "start_time": "2023-01-01 13:05:00", 
                "end_time": "2023-01-01 13:10:00",
                "file_path": os.path.join(group_dir, "video2.dav"), 
                "metadata": {}, 
                "status": "converted", 
                "skip": False
            }
        }
    }
    create_group_dir_with_state(app_instance.storage_path, group_name, state_data)
    
    # Override the global os.path.exists mock to handle different file existence states
    combined_path = os.path.join(group_dir, "combined.mp4")
    state_file_path = os.path.join(group_dir, "state.json")
    
    def mock_exists_side_effect(path):
        # State file exists (we created it)
        if path == state_file_path:
            return True
        # Storage directory exists
        if path == app_instance.storage_path:
            return True
        # Group directory exists
        if path == group_dir:
            return True
        # Combined file does NOT exist (this should trigger combining)
        if path == combined_path:
            return False
        # MP4 files exist (converted from DAV)
        if path.endswith('.mp4') and 'video' in path:
            return True
        # DAV files exist (original downloads)
        if path.endswith('.dav'):
            return True
        # Default to True for other paths
        return True
    
    with patch('os.path.exists', side_effect=mock_exists_side_effect):
        await app_instance._audit_storage_directory()

    task = await asyncio.wait_for(app_instance.ffmpeg_queue.get(), timeout=1)
    assert isinstance(task, CombineTask)
    assert task.item_path == group_dir
    
@pytest.mark.asyncio
async def test_poll_for_trimming(app_instance: VideoGrouperApp):
    """Test that a 'combined' group with populated match_info gets a TrimTask."""
    group_name = "2023.01.01-14.00.00"  # Use valid group directory format
    group_dir = os.path.join(app_instance.storage_path, group_name)
    state_data = {"status": "combined", "files": {}}
    create_group_dir_with_state(app_instance.storage_path, group_name, state_data, files_to_create=["combined.mp4"])

    # Create a populated match_info.ini
    match_info_path = os.path.join(group_dir, "match_info.ini")
    with open(match_info_path, 'w') as f:
        f.write("[MATCH]\nmy_team_name = My Team\nopponent_team_name = Opponent\nlocation = Field 1\nstart_time_offset = 00:05:00\ngame_duration_hh_mm = 01:30")

    # Override the global os.path.exists mock to handle different file existence states
    combined_path = os.path.join(group_dir, "combined.mp4")
    state_file_path = os.path.join(group_dir, "state.json")
    
    def mock_exists_side_effect(path):
        # State file exists (we created it)
        if path == state_file_path:
            return True
        # Storage directory exists
        if path == app_instance.storage_path:
            return True
        # Group directory exists
        if path == group_dir:
            return True
        # Combined file exists (for trimming test)
        if path == combined_path:
            return True
        # Match info file exists (we created it)
        if path == match_info_path:
            return True
        # Default to True for other paths
        return True
    
    with patch('os.path.exists', side_effect=mock_exists_side_effect):
        await app_instance._audit_storage_directory()

    task = await asyncio.wait_for(app_instance.ffmpeg_queue.get(), timeout=1)
    assert isinstance(task, TrimTask)
    assert task.item_path == group_dir
    assert task.match_info.my_team_name == "My Team"


@pytest.mark.asyncio
async def test_poll_for_youtube_upload(app_instance: VideoGrouperApp):
    """Test that an 'autocam_complete' group gets a YouTubeUploadTask."""
    group_name = "2023.01.01-15.00.00"  # Use valid group directory format
    group_dir = os.path.join(app_instance.storage_path, group_name)
    state_data = {"status": "autocam_complete", "files": {}}
    create_group_dir_with_state(app_instance.storage_path, group_name, state_data)

    await app_instance._audit_storage_directory()

    task = await asyncio.wait_for(app_instance.ffmpeg_queue.get(), timeout=1)
    assert isinstance(task, YouTubeUploadTask)
    assert task.item_path == group_dir


@pytest.mark.asyncio
async def test_poll_skip_file(app_instance: VideoGrouperApp):
    """Test that a file with skip=true is not queued."""
    group_name = "2023.01.01-16.00.00"  # Use valid group directory format
    file_path = os.path.join(app_instance.storage_path, group_name, "video7.dav")
    state_data = {
        "status": "downloading",
        "files": {
            file_path: {
                "start_time": "2023-01-01 14:00:00", "end_time": "2023-01-01 14:05:00",
                "file_path": file_path, "metadata": {}, "status": "downloaded", "skip": True
            }
        }
    }
    create_group_dir_with_state(app_instance.storage_path, group_name, state_data)

    await app_instance._audit_storage_directory()

    assert app_instance.ffmpeg_queue.empty()
    assert app_instance.download_queue.empty() 