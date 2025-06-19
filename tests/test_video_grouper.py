import asyncio
import configparser
import json
import os
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch, mock_open

import pytest
from unittest.mock import call

from video_grouper.models import RecordingFile
from video_grouper.video_grouper import VideoGrouperApp, DOWNLOAD_QUEUE_STATE_FILE, FFMPEG_QUEUE_STATE_FILE

# Constants
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
STORAGE_PATH = "/fake/storage"

@pytest.fixture
def mock_config():
    """Fixture for a mock configparser object with a fake storage path."""
    config = configparser.ConfigParser()
    config['CAMERA'] = {
        'type': 'dahua',
        'device_ip': '127.0.0.1',
        'username': 'admin',
        'password': 'password'
    }
    config['STORAGE'] = {'path': STORAGE_PATH}
    config['APP'] = {'check_interval_seconds': '1'}
    return config

@pytest.fixture
def mock_camera():
    """Fixture for a mock DahuaCamera."""
    camera = MagicMock()
    camera.check_availability = AsyncMock(return_value=True)
    camera.get_file_list = AsyncMock(return_value=[])
    camera.download_file = AsyncMock(return_value=True)
    camera.connection_events = [('mock_time', 'connected')]
    return camera

@pytest.fixture
def async_context_manager_mock():
    """Fixture to create a mock for an async context manager."""
    async def async_magic_mock(*args, **kwargs):
        pass
    
    mock_file = MagicMock()
    mock_file.write = AsyncMock()
    
    manager = AsyncMock()
    manager.__aenter__.return_value = mock_file
    manager.__aexit__.return_value = False
    return manager


def setup_app(mock_config, mock_camera):
    """Helper to create and setup a VideoGrouperApp instance."""
    app = VideoGrouperApp(config=mock_config, camera=mock_camera)
    # We manually set storage_path because the config points to a fake path
    app.storage_path = STORAGE_PATH
    app.camera.storage_path = app.storage_path
    # We don't mock the load/save methods anymore, so they can call the patched functions
    return app

@pytest.mark.asyncio
@patch('video_grouper.video_grouper.aiofiles.open')
@patch('os.remove')
@patch('os.listdir')
@patch('os.path.exists')
@patch('os.makedirs')
@patch('builtins.open', new_callable=mock_open)
class TestVideoGrouperAppWithMocks:

    async def test_initialization(self, mock_builtin_open, mock_makedirs, mock_exists, mock_listdir, mock_remove, mock_aio_open, mock_config, mock_camera):
        """Test app initializes correctly with mocked filesystem."""
        mock_exists.return_value = False # No state files exist
        mock_listdir.return_value = [] # No existing directories
        
        app = setup_app(mock_config, mock_camera)
        await app.initialize()
        
        # Assert that it tried to load queues (which would check for files)
        mock_exists.assert_any_call(os.path.join(STORAGE_PATH, DOWNLOAD_QUEUE_STATE_FILE))
        mock_exists.assert_any_call(os.path.join(STORAGE_PATH, FFMPEG_QUEUE_STATE_FILE))
        
        # Assert that it tries to create the storage directory
        mock_makedirs.assert_called_once_with(STORAGE_PATH, exist_ok=True)
        # Assert it checks the contents of the storage directory
        mock_listdir.assert_called_once_with(STORAGE_PATH)


    async def test_sync_creates_group_and_state(self, mock_builtin_open, mock_makedirs, mock_exists, mock_listdir, mock_remove, mock_aio_open, mock_config, mock_camera):
        """Test that syncing a new file creates a group directory and a state.json file."""
        mock_exists.return_value = False # No directories or files exist initially
        mock_listdir.return_value = [] # No existing directories

        app = setup_app(mock_config, mock_camera)
        
        start_time = datetime(2025, 1, 1, 12, 0, 0)
        end_time = datetime(2025, 1, 1, 12, 5, 0)
        
        # This should be a list of dicts, as returned by the camera API
        mock_file_list = [{
            'path': '/remote/file1.dav',
            'startTime': start_time.strftime(DEFAULT_DATE_FORMAT),
            'endTime': end_time.strftime(DEFAULT_DATE_FORMAT)
        }]
        
        app.camera.get_file_list.return_value = mock_file_list
        app._get_latest_processed_time = AsyncMock(return_value=None)
        app._update_latest_processed_time = AsyncMock()

        with patch('video_grouper.video_grouper.DirectoryState') as mock_dir_state_class:
            mock_state_instance = MagicMock()
            mock_state_instance.add_file = AsyncMock() # This is an async method
            mock_state_instance.save_state = AsyncMock()
            mock_state_instance.is_file_in_state.return_value = False # Ensure it tries to add the file
            mock_state_instance.get_file_by_path.return_value = None # Ensure it doesn't think the file is skipped
            mock_dir_state_class.return_value = mock_state_instance
            
            await app.sync_files_from_camera()

        # Check that listdir was called to find existing directories
        mock_listdir.assert_called_once_with(STORAGE_PATH)
        
        # Check that a new group directory was created
        group_dir_name = start_time.strftime('%Y.%m.%d-%H.%M.%S')
        expected_group_path = os.path.join(STORAGE_PATH, group_dir_name)
        mock_makedirs.assert_any_call(expected_group_path, exist_ok=True)
        
        # Check that a file was added to the download queue
        assert app.download_queue.qsize() == 1


    async def test_handle_combine_task(self, mock_builtin_open, mock_makedirs, mock_exists, mock_listdir, mock_remove, mock_aio_open, mock_config, mock_camera, async_context_manager_mock):
        """Test combine task generates correct ffmpeg command and updates state."""
        app = setup_app(mock_config, mock_camera)
        group_dir = os.path.join(STORAGE_PATH, "2025.01.01-12.00.00")
        
        # Configure the aio_open mock to work as an async context manager
        mock_aio_open.return_value = async_context_manager_mock

        # Simulate existing converted files and state
        file1_path = os.path.join(group_dir, "file1.dav.mp4")
        file2_path = os.path.join(group_dir, "file2.dav.mp4")
        
        with patch('video_grouper.video_grouper.DirectoryState') as mock_dir_state_class:
            mock_state_instance = MagicMock()
            mock_state_instance.get_files_by_status.return_value = [
                RecordingFile.from_dict({'file_path': file1_path.replace('.mp4', ''), 'status': 'converted', 'start_time': '2025-01-01T12:00:00', 'end_time': '2025-01-01T12:05:00', "metadata": {}}),
                RecordingFile.from_dict({'file_path': file2_path.replace('.mp4', ''), 'status': 'converted', 'start_time': '2025-01-01T12:05:00', 'end_time': '2025-01-01T12:10:00', "metadata": {}})
            ]
            mock_state_instance.update_group_status = AsyncMock()
            mock_dir_state_class.return_value = mock_state_instance

            # Mock the subprocess call
            with patch('asyncio.create_subprocess_exec', new_callable=AsyncMock) as mock_exec:
                proc_mock = AsyncMock()
                proc_mock.wait = AsyncMock()
                proc_mock.returncode = 0
                mock_exec.return_value = proc_mock
                
                # mock exists for the file list
                mock_exists.return_value = True

                await app._handle_combine_task(group_dir)

            # Verify ffmpeg was called
            mock_exec.assert_called_once()
            
            # Verify the file list for combining was created and then removed
            concat_list_path = os.path.join(group_dir, "filelist.txt")
            mock_aio_open.assert_called_once_with(concat_list_path, 'w')
            
            # Since the file list is created and removed within the same function,
            # we need to ensure our mock for os.path.exists returns True for it
            # before we assert that os.remove is called.
            original_exists = mock_exists.side_effect
            def side_effect(path):
                if path == concat_list_path:
                    return True
                if original_exists:
                    return original_exists(path)
                return True
            mock_exists.side_effect = side_effect

            mock_remove.assert_called_once_with(concat_list_path)

            # Verify state was updated to "combined"
            mock_state_instance.update_group_status.assert_called_once_with("combined")


    async def test_handle_trim_task(self, mock_aio_open, mock_remove, mock_listdir, mock_exists, mock_makedirs, mock_builtin_open, mock_config, mock_camera):
        """Test trim task uses a mock config parser and calls the trim utility."""
        app = setup_app(mock_config, mock_camera)
        group_dir = os.path.join(STORAGE_PATH, "group_for_trim")
        combined_path = os.path.join(group_dir, "combined.mp4")

        # Mock that the combined file exists
        mock_exists.return_value = True

        # Create a mock config parser for match_info
        mock_match_config = configparser.ConfigParser()
        mock_match_config['MATCH'] = {
            'my_team_name': 'Test Team',
            'opponent_team_name': 'Rivals',
            'location': 'Home',
            'start_time_offset': '00:01:30',
            'total_duration': '00:10:00'
        }

        with patch('video_grouper.video_grouper.DirectoryState') as mock_dir_state_class:
            mock_state_instance = MagicMock()
            mock_state_instance.update_group_status = AsyncMock()
            mock_dir_state_class.return_value = mock_state_instance

            with patch('video_grouper.video_grouper.trim_video', new_callable=AsyncMock) as mock_trim:
                mock_trim.return_value = True
                
                await app._handle_trim_task(group_dir, match_info_config=mock_match_config)

                # Verify trim was called with correct parameters
                mock_trim.assert_called_once()
                kwargs = mock_trim.call_args.kwargs
                assert kwargs['input_path'] == combined_path
                assert 'testteam-rivals-home' in kwargs['output_path']
                assert kwargs['start_offset'] == '00:01:30'
                assert kwargs['duration'] == str(10 * 60)

            # Verify group status was updated
            mock_state_instance.update_group_status.assert_called_once_with("trimmed") 