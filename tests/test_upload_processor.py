"""Tests for the UploadProcessor."""

import os
import json
import tempfile
import configparser
from unittest.mock import Mock, patch
import pytest

from video_grouper.task_processors.upload_processor import UploadProcessor
from video_grouper.models import VideoUploadTask


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
    config.add_section('YOUTUBE')
    config.set('YOUTUBE', 'enabled', 'true')
    config.add_section('youtube.playlist.processed')
    config.set('youtube.playlist.processed', 'name_format', '{my_team_name} 2023s')
    config.set('youtube.playlist.processed', 'description', 'Processed videos')
    config.set('youtube.playlist.processed', 'privacy_status', 'unlisted')
    config.add_section('youtube.playlist.raw')
    config.set('youtube.playlist.raw', 'name_format', '{my_team_name} 2023s - Full Field')
    config.set('youtube.playlist.raw', 'description', 'Raw videos')
    config.set('youtube.playlist.raw', 'privacy_status', 'unlisted')
    return config


class TestUploadProcessor:
    """Test the UploadProcessor."""
    
    @pytest.mark.asyncio
    async def test_upload_processor_initialization(self, temp_storage, mock_config):
        """Test UploadProcessor initialization."""
        processor = UploadProcessor(temp_storage, mock_config)
        
        assert processor.storage_path == temp_storage
        assert processor.config == mock_config
    
    @pytest.mark.asyncio
    async def test_upload_task_processing_no_credentials(self, temp_storage, mock_config):
        """Test upload task when credentials file doesn't exist."""
        group_dir = os.path.join(temp_storage, "2023.01.01-10.00.00")
        
        processor = UploadProcessor(temp_storage, mock_config)
        
        upload_task = VideoUploadTask(group_dir)
        await processor.process_item(upload_task)
        
        # Should complete without error (credentials check is logged but doesn't fail)
    
    @pytest.mark.asyncio
    @patch('video_grouper.task_processors.upload_processor.get_youtube_paths')
    @patch('os.path.exists')
    async def test_upload_task_processing_success(self, mock_exists, mock_get_paths, temp_storage, mock_config):
        """Test successful upload task processing."""
        group_dir = "/test/group"
        credentials_file = "/test/youtube/client_secret.json"
        token_file = "/test/youtube/token.json"
        
        # Mock the path functions
        mock_get_paths.return_value = (credentials_file, token_file)
        mock_exists.return_value = True  # Credentials file exists
        
        processor = UploadProcessor(temp_storage, mock_config)
        
        # Mock the upload function
        with patch('video_grouper.task_processors.upload_processor.upload_group_videos') as mock_upload:
            mock_upload.return_value = True
            
            upload_task = VideoUploadTask(group_dir)
            await processor.process_item(upload_task)
            
            mock_upload.assert_called_once()
            
            # Verify upload was called with correct arguments
            call_args = mock_upload.call_args
            assert call_args[0][0] == group_dir  # group_dir
            assert call_args[0][1] == credentials_file  # credentials_file
            assert call_args[0][3] is not None  # playlist_config
    
    @pytest.mark.asyncio
    @patch('video_grouper.task_processors.upload_processor.get_youtube_paths')
    @patch('os.path.exists')
    async def test_upload_task_processing_failure(self, mock_exists, mock_get_paths, temp_storage, mock_config):
        """Test failed upload task processing."""
        group_dir = "/test/group"
        credentials_file = "/test/youtube/client_secret.json"
        token_file = "/test/youtube/token.json"
        
        # Mock the path functions
        mock_get_paths.return_value = (credentials_file, token_file)
        mock_exists.return_value = True  # Credentials file exists
        
        processor = UploadProcessor(temp_storage, mock_config)
        
        # Mock the upload function to fail
        with patch('video_grouper.task_processors.upload_processor.upload_group_videos') as mock_upload:
            mock_upload.return_value = False
            
            upload_task = VideoUploadTask(group_dir)
            await processor.process_item(upload_task)
            
            mock_upload.assert_called_once()
    
    @pytest.mark.asyncio
    @patch('video_grouper.task_processors.upload_processor.get_youtube_paths')
    @patch('os.path.exists')
    async def test_upload_task_processing_exception(self, mock_exists, mock_get_paths, temp_storage, mock_config):
        """Test upload task processing with exception."""
        group_dir = "/test/group"
        credentials_file = "/test/youtube/client_secret.json"
        token_file = "/test/youtube/token.json"
        
        # Mock the path functions
        mock_get_paths.return_value = (credentials_file, token_file)
        mock_exists.return_value = True  # Credentials file exists
        
        processor = UploadProcessor(temp_storage, mock_config)
        
        # Mock the upload function to raise exception
        with patch('video_grouper.task_processors.upload_processor.upload_group_videos') as mock_upload:
            mock_upload.side_effect = Exception("Upload error")
            
            upload_task = VideoUploadTask(group_dir)
            await processor.process_item(upload_task)
            
            # Should complete without raising the exception
    
    @pytest.mark.asyncio
    @patch('video_grouper.task_processors.upload_processor.get_youtube_paths')
    @patch('os.path.exists')
    async def test_upload_task_no_playlist_config(self, mock_exists, mock_get_paths, temp_storage):
        """Test upload task when no playlist configuration is available."""
        # Create config without playlist sections
        config = configparser.ConfigParser()
        config.add_section('APP')
        config.set('APP', 'check_interval_seconds', '10')
        
        group_dir = "/test/group"
        credentials_file = "/test/youtube/client_secret.json"
        token_file = "/test/youtube/token.json"
        
        # Mock the path functions
        mock_get_paths.return_value = (credentials_file, token_file)
        mock_exists.return_value = True  # Credentials file exists
        
        processor = UploadProcessor(temp_storage, config)
        
        # Mock the upload function
        with patch('video_grouper.task_processors.upload_processor.upload_group_videos') as mock_upload:
            mock_upload.return_value = True
            
            upload_task = VideoUploadTask(group_dir)
            await processor.process_item(upload_task)
            
            mock_upload.assert_called_once()
            
            # Verify upload was called with None playlist_config
            call_args = mock_upload.call_args
            assert call_args[0][3] is None  # playlist_config should be None
    
    def test_deserialize_upload_task(self, temp_storage, mock_config):
        """Test deserializing VideoUploadTask from state data."""
        processor = UploadProcessor(temp_storage, mock_config)
        
        # Test valid task data
        task_data = {'item_path': '/test/path'}
        task = processor.deserialize_item(task_data)
        
        assert isinstance(task, VideoUploadTask)
        assert task.item_path == '/test/path'
        
        # Test invalid task data
        invalid_data = {'invalid_key': 'value'}
        task = processor.deserialize_item(invalid_data)
        
        assert task is None
    
    def test_get_item_key(self, temp_storage, mock_config):
        """Test getting unique key for VideoUploadTask."""
        processor = UploadProcessor(temp_storage, mock_config)
        
        upload_task = VideoUploadTask("/test/path/group")
        key = processor.get_item_key(upload_task)
        
        assert key == "/test/path/group"
    
    def test_get_state_file_name(self, temp_storage, mock_config):
        """Test getting state file name."""
        processor = UploadProcessor(temp_storage, mock_config)
        
        state_file_name = processor.get_state_file_name()
        
        assert state_file_name == "upload_queue_state.json"
    
 