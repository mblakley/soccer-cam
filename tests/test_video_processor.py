"""Tests for the VideoProcessor."""

import os
import tempfile
import configparser
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime
import pytest

from video_grouper.task_processors.video_processor import VideoProcessor
from video_grouper.models import ConvertTask, CombineTask, TrimTask, MatchInfo
from video_grouper.directory_state import DirectoryState


@pytest.fixture
def temp_storage():
    """Create a temporary storage directory for testing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def mock_config():
    """Create a mock configuration object."""
    config = configparser.ConfigParser()
    config.add_section('APP')
    config.set('APP', 'check_interval_seconds', '10')
    return config


class TestVideoProcessor:
    """Test the VideoProcessor."""
    
    @pytest.mark.asyncio
    async def test_video_processor_initialization(self, temp_storage, mock_config):
        """Test VideoProcessor initialization."""
        processor = VideoProcessor(temp_storage, mock_config)
        
        assert processor.storage_path == temp_storage
        assert processor.upload_processor is None
    
    @pytest.mark.asyncio
    async def test_set_upload_processor(self, temp_storage, mock_config):
        """Test setting upload processor reference."""
        processor = VideoProcessor(temp_storage, mock_config)
        mock_upload = Mock()
        
        processor.set_upload_processor(mock_upload)
        
        assert processor.upload_processor == mock_upload
    
    @pytest.mark.asyncio 
    @patch('video_grouper.task_processors.video_processor.DirectoryState')
    @patch('video_grouper.task_processors.video_processor.async_convert_file')
    @patch('video_grouper.task_processors.video_processor.create_screenshot')
    @patch('os.path.exists')
    async def test_convert_task_processing(self, mock_exists, mock_screenshot, mock_convert, mock_directory_state, temp_storage, mock_config):
        """Test processing a convert task."""
        test_file = "/test/group/test.dav"
        mp4_file = "/test/group/test.mp4"
        
        # Mock DirectoryState
        mock_dir_state_instance = Mock()
        mock_file_obj = Mock()
        mock_file_obj.skip = False
        mock_dir_state_instance.files = {test_file: mock_file_obj}
        mock_dir_state_instance.update_file_state = AsyncMock()
        mock_directory_state.return_value = mock_dir_state_instance
        
        # Mock file operations
        mock_exists.return_value = True
        mock_convert.return_value = mp4_file
        mock_screenshot.return_value = True
        
        processor = VideoProcessor(temp_storage, mock_config)
        
        # Mock the _ensure_match_info_exists and cleanup_dav_files methods to avoid file system operations
        processor._ensure_match_info_exists = AsyncMock()
        processor.cleanup_dav_files = AsyncMock()
        
        convert_task = ConvertTask(test_file)
        await processor.process_item(convert_task)
        
        mock_convert.assert_called_once_with(test_file)
        mock_screenshot.assert_called_once()
        processor._ensure_match_info_exists.assert_called_once()
        
        # Verify file status was updated
        expected_screenshot_path = mp4_file.replace('.mp4', '_screenshot.jpg')
        mock_dir_state_instance.update_file_state.assert_called_with(
            test_file, status="converted", screenshot_path=expected_screenshot_path
        )
    
    @pytest.mark.asyncio
    @patch('video_grouper.task_processors.video_processor.DirectoryState')
    async def test_convert_task_skipped_file(self, mock_directory_state, temp_storage, mock_config):
        """Test processing a convert task for a skipped file."""
        test_file = "/test/group/test.dav"
        
        # Mock DirectoryState with skipped file
        mock_dir_state_instance = Mock()
        mock_file_obj = Mock()
        mock_file_obj.skip = True
        mock_dir_state_instance.files = {test_file: mock_file_obj}
        mock_directory_state.return_value = mock_dir_state_instance
        
        processor = VideoProcessor(temp_storage, mock_config)
        
        convert_task = ConvertTask(test_file)
        await processor.process_item(convert_task)
        
        # Should not call any conversion methods when file is skipped
        # Just verify it handled the skip gracefully
    
    @pytest.mark.asyncio
    @patch('video_grouper.task_processors.video_processor.DirectoryState')
    @patch('asyncio.create_subprocess_exec')
    @patch('aiofiles.open')
    async def test_combine_task_processing(self, mock_aiofiles, mock_subprocess, mock_directory_state, temp_storage, mock_config):
        """Test processing a combine task."""
        group_dir = "/test/group"
        
        # Mock DirectoryState with converted files
        mock_dir_state_instance = Mock()
        mock_file_obj1 = Mock()
        mock_file_obj1.file_path = "/test/group/test0.dav"
        mock_file_obj1.status = "converted"
        mock_file_obj2 = Mock()
        mock_file_obj2.file_path = "/test/group/test1.dav"
        mock_file_obj2.status = "converted"
        
        mock_dir_state_instance.get_files_by_status.return_value = [mock_file_obj1, mock_file_obj2]
        mock_dir_state_instance.update_group_status = AsyncMock()
        mock_directory_state.return_value = mock_dir_state_instance
        
        # Mock file operations
        mock_file_handle = Mock()
        mock_file_handle.write = AsyncMock()
        mock_aiofiles.return_value.__aenter__.return_value = mock_file_handle
        
        # Mock subprocess
        mock_process = Mock()
        mock_process.wait = AsyncMock()
        mock_process.returncode = 0
        mock_subprocess.return_value = mock_process
        
        processor = VideoProcessor(temp_storage, mock_config)
        
        combine_task = CombineTask(group_dir)
        await processor.process_item(combine_task)
        
        mock_subprocess.assert_called_once()
        mock_dir_state_instance.update_group_status.assert_called_with("combined")
    
    @pytest.mark.asyncio
    @patch('video_grouper.task_processors.video_processor.DirectoryState')
    @patch('video_grouper.task_processors.video_processor.trim_video')
    @patch('os.path.exists')
    @patch('os.makedirs')
    async def test_trim_task_processing(self, mock_makedirs, mock_exists, mock_trim, mock_directory_state, temp_storage, mock_config):
        """Test processing a trim task."""
        group_dir = "/test/group"
        
        # Mock DirectoryState
        mock_dir_state_instance = Mock()
        mock_dir_state_instance.update_group_status = AsyncMock()
        mock_directory_state.return_value = mock_dir_state_instance
        
        # Mock file existence (combined.mp4 exists)
        mock_exists.return_value = True
        mock_trim.return_value = True
        
        # Create match info
        match_info = MatchInfo(
            my_team_name="Team A",
            opponent_team_name="Team B",
            location="Stadium",
            start_time_offset="00:05:00",
            total_duration="01:30:00"
        )
        
        processor = VideoProcessor(temp_storage, mock_config)
        
        trim_task = TrimTask(group_dir, match_info)
        await processor.process_item(trim_task)
        
        mock_trim.assert_called_once()
        mock_dir_state_instance.update_group_status.assert_called_with("trimmed")
    
    @pytest.mark.asyncio
    @patch('video_grouper.task_processors.video_processor.DirectoryState')
    @patch('os.path.exists')
    async def test_trim_task_missing_combined_file(self, mock_exists, mock_directory_state, temp_storage, mock_config):
        """Test trim task when combined file is missing."""
        group_dir = "/test/group"
        
        # Mock DirectoryState
        mock_dir_state_instance = Mock()
        mock_dir_state_instance.update_group_status = AsyncMock()
        mock_directory_state.return_value = mock_dir_state_instance
        
        # Mock file existence (combined.mp4 does NOT exist)
        mock_exists.return_value = False
        
        # Create match info
        match_info = MatchInfo(
            my_team_name="Team A",
            opponent_team_name="Team B",
            location="Stadium",
            start_time_offset="00:05:00",
            total_duration="01:30:00"
        )
        
        processor = VideoProcessor(temp_storage, mock_config)
        
        trim_task = TrimTask(group_dir, match_info)
        await processor.process_item(trim_task)
        
        # Verify directory status was updated to trim_failed
        mock_dir_state_instance.update_group_status.assert_called_with(
            "trim_failed", error_message="Combined video not found for trimming."
        )
    
    @pytest.mark.asyncio
    async def test_unknown_task_type(self, temp_storage, mock_config):
        """Test processing an unknown task type."""
        processor = VideoProcessor(temp_storage, mock_config)
        
        # Create a mock task with unknown type
        unknown_task = Mock()
        unknown_task.task_type = "unknown_type"
        
        success = await processor.process_item(unknown_task)
        
        assert not success
    
    def test_deserialize_item(self, temp_storage, mock_config):
        """Test deserializing FFmpegTask from state data."""
        processor = VideoProcessor(temp_storage, mock_config)
        
        # Test valid convert task data
        task_data = {
            'task_type': 'convert',
            'item_path': '/test/path/test.dav'
        }
        
        task = processor.deserialize_item(task_data)
        
        assert isinstance(task, ConvertTask)
        assert task.item_path == '/test/path/test.dav'
        
        # Test invalid data
        invalid_data = {'invalid_key': 'value'}
        task = processor.deserialize_item(invalid_data)
        
        assert task is None
    
    def test_get_item_key(self, temp_storage, mock_config):
        """Test getting unique key for FFmpegTask."""
        processor = VideoProcessor(temp_storage, mock_config)
        
        # Test convert task
        convert_task = ConvertTask("/test/path/test.dav")
        key = processor.get_item_key(convert_task)
        assert key == "convert:/test/path/test.dav"
        
        # Test trim task with match info
        match_info = MatchInfo(
            my_team_name="Team A",
            opponent_team_name="Team B",
            location="Stadium",
            start_time_offset="00:05:00",
            total_duration="01:30:00"
        )
        trim_task = TrimTask("/test/group", match_info)
        key = processor.get_item_key(trim_task)
        assert key.startswith("trim:/test/group:")
        assert "Team A" in key or str(hash(match_info)) in key 