"""Tests for the VideoProcessor."""

import os
import tempfile
import configparser
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime
import pytest

from video_grouper.task_processors.video_processor import VideoProcessor
from video_grouper.task_processors.tasks import ConvertTask, CombineTask, TrimTask
from video_grouper.models import MatchInfo
from video_grouper.directory_state import DirectoryState


@pytest.fixture
def temp_storage():
    """Create a temporary storage directory for testing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def mock_config():
    """Create a mock configuration object."""
    config = Mock()
    config.get.return_value = "10"
    config.has_section.return_value = False
    return config


class TestVideoProcessor:
    """Test the VideoProcessor."""
    
    @pytest.mark.asyncio
    async def test_video_processor_initialization(self, temp_storage, mock_config):
        """Test VideoProcessor initialization."""
        processor = VideoProcessor(temp_storage, mock_config)
        
        assert processor.storage_path == temp_storage
        assert processor.config == mock_config
    
    @pytest.mark.asyncio
    async def test_set_upload_processor(self, temp_storage, mock_config):
        """Test setting upload processor reference."""
        processor = VideoProcessor(temp_storage, mock_config)
        mock_upload = Mock()
        
        processor.set_upload_processor(mock_upload)
        
        assert processor.upload_processor == mock_upload
    
    @pytest.mark.asyncio 
    async def test_convert_task_processing(self, temp_storage, mock_config):
        """Test processing a convert task."""
        test_file = "/test/group/test.dav"
        
        processor = VideoProcessor(temp_storage, mock_config)
        
        # Create convert task and mock its execute method
        convert_task = ConvertTask(file_path=test_file)
        
        # Mock the task's execute method
        with patch.object(convert_task, 'execute', new_callable=AsyncMock) as mock_execute:
            mock_execute.return_value = True
            
            await processor.process_item(convert_task)
            
            mock_execute.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_convert_task_skipped_file(self, temp_storage, mock_config):
        """Test processing a convert task for a skipped file."""
        test_file = "/test/group/test.dav"
        
        processor = VideoProcessor(temp_storage, mock_config)
        
        convert_task = ConvertTask(file_path=test_file)
        
        # Mock the task's execute method to return False (failed)
        with patch.object(convert_task, 'execute', new_callable=AsyncMock) as mock_execute:
            mock_execute.return_value = False
            
            await processor.process_item(convert_task)
            
            mock_execute.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_combine_task_processing(self, temp_storage, mock_config):
        """Test processing a combine task."""
        group_dir = "/test/group"
        
        processor = VideoProcessor(temp_storage, mock_config)
        
        # Create combine task and mock its execute method
        combine_task = CombineTask(group_dir=group_dir)
        
        # Mock the task's execute method
        with patch.object(combine_task, 'execute', new_callable=AsyncMock) as mock_execute:
            mock_execute.return_value = True
            
            await processor.process_item(combine_task)
            
            mock_execute.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_trim_task_processing(self, temp_storage, mock_config):
        """Test processing a trim task."""
        group_dir = "/test/group"
        
        processor = VideoProcessor(temp_storage, mock_config)
        
        # Create trim task using the new interface
        trim_task = TrimTask(
            group_dir=group_dir,
            start_time="00:05:00",
            end_time="01:35:00"
        )
        
        # Mock the execute method to return success
        with patch.object(trim_task, 'execute', return_value=True) as mock_execute:
            await processor.process_item(trim_task)
            mock_execute.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_trim_task_missing_combined_file(self, temp_storage, mock_config):
        """Test trim task when combined file is missing."""
        group_dir = "/test/group"
        
        processor = VideoProcessor(temp_storage, mock_config)
        
        try:
            # Create trim task using the new interface
            trim_task = TrimTask(
                group_dir=group_dir,
                start_time="00:05:00",
                end_time="01:35:00"
            )
            
            # Mock the execute method to return failure
            with patch.object(trim_task, 'execute', return_value=False) as mock_execute:
                await processor.process_item(trim_task)
                mock_execute.assert_called_once()
        finally:
            # Ensure processor is properly stopped
            await processor.stop()
    
    @pytest.mark.asyncio
    async def test_unknown_task_type(self, temp_storage, mock_config):
        """Test processing an unknown task type."""
        processor = VideoProcessor(temp_storage, mock_config)
        
        # Create a mock task with unknown type
        unknown_task = Mock()
        unknown_task.execute = AsyncMock(return_value=False)
        
        await processor.process_item(unknown_task)
        
        unknown_task.execute.assert_called_once()
    
    def test_get_item_key(self, temp_storage, mock_config):
        """Test getting unique key for FFmpegTask."""
        processor = VideoProcessor(temp_storage, mock_config)
        
        # Test convert task
        convert_task = ConvertTask(file_path="/test/path/test.dav")
        key = processor.get_item_key(convert_task)
        assert key.startswith("convert:/test/path/test.dav:")
        
        # Test trim task with new constructor
        trim_task = TrimTask(
            group_dir="/test/group",
            start_time="00:05:00",
            end_time="01:35:00"
        )
        key = processor.get_item_key(trim_task)
        assert key.startswith("trim:/test/group:") 