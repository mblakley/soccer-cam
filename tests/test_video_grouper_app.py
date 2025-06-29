"""Integration tests for the refactored VideoGrouperApp."""

import os
import tempfile
import configparser
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime
import pytest

from video_grouper.video_grouper_app import VideoGrouperApp
from video_grouper.models import RecordingFile
from video_grouper.task_processors.tasks import ConvertTask, YoutubeUploadTask
from video_grouper.utils.directory_state import DirectoryState


@pytest.fixture
def temp_storage():
    """Create a temporary storage directory for testing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def mock_config(temp_storage):
    """Create a mock configuration object."""
    config = configparser.ConfigParser()
    config.add_section('STORAGE')
    config.set('STORAGE', 'path', temp_storage)
    config.add_section('APP')
    config.set('APP', 'check_interval_seconds', '1')  # Fast polling for tests
    config.add_section('CAMERA')
    config.set('CAMERA', 'type', 'dahua')
    config.set('CAMERA', 'device_ip', '192.168.1.100')
    config.set('CAMERA', 'username', 'admin')
    config.set('CAMERA', 'password', 'password')
    config.add_section('YOUTUBE')
    config.set('YOUTUBE', 'enabled', 'true')
    return config


@pytest.fixture
def mock_camera():
    """Create a mock camera object."""
    camera = Mock()
    camera.check_availability = AsyncMock(return_value=True)
    camera.get_file_list = AsyncMock(return_value=[])
    camera.get_connected_timeframes = Mock(return_value=[])
    camera.download_file = AsyncMock(return_value=True)
    camera.close = AsyncMock()
    return camera


class TestVideoGrouperAppRefactored:
    """Test the refactored VideoGrouperApp."""
    
    def test_initialization(self, mock_config, mock_camera):
        """Test VideoGrouperApp initialization."""
        app = VideoGrouperApp(mock_config, camera=mock_camera)
        
        assert app.config == mock_config
        assert app.camera == mock_camera
        assert app.poll_interval == 1
        
        # Verify all processors are initialized
        assert app.state_auditor is not None
        assert app.camera_poller is not None
        assert app.download_processor is not None
        assert app.video_processor is not None
        assert app.upload_processor is not None
        
        # Verify processors are wired correctly
        assert app.state_auditor.download_processor == app.download_processor
        assert app.state_auditor.video_processor == app.video_processor
        assert app.state_auditor.upload_processor == app.upload_processor
        assert app.camera_poller.download_processor == app.download_processor
        assert app.download_processor.video_processor == app.video_processor
        assert app.video_processor.upload_processor == app.upload_processor
    
    def test_initialization_with_camera_creation(self, mock_config):
        """Test VideoGrouperApp initialization with automatic camera creation."""
        with patch('video_grouper.cameras.dahua.DahuaCamera') as mock_dahua:
            mock_camera_instance = Mock()
            mock_dahua.return_value = mock_camera_instance
            
            app = VideoGrouperApp(mock_config)
            
            assert app.camera == mock_camera_instance
            mock_dahua.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_processor_lifecycle(self, mock_config, mock_camera):
        """Test processor lifecycle management."""
        app = VideoGrouperApp(mock_config, camera=mock_camera)
        
        # Initialize should start all processors
        await app.initialize()
        
        # All processors should be running
        for processor in app.processors:
            assert processor._processor_task is not None
            assert not processor._processor_task.done()
        
        # Shutdown should stop all processors
        await app.shutdown()
        
        # All processors should be stopped
        for processor in app.processors:
            assert processor._processor_task.done()
    
    @pytest.mark.asyncio
    async def test_add_download_task(self, mock_config, mock_camera):
        """Test adding download task through convenience method."""
        app = VideoGrouperApp(mock_config, camera=mock_camera)
        
        try:
            recording_file = RecordingFile(
                start_time=datetime(2023, 1, 1, 10, 0, 0),
                end_time=datetime(2023, 1, 1, 10, 5, 0),
                file_path="/test/path/test.dav",
                metadata={'path': '/test.dav'}
            )
            
            await app.add_download_task(recording_file)
            
            assert app.download_processor.get_queue_size() == 1
        finally:
            # Ensure proper cleanup to prevent asyncio warnings
            await app.shutdown()
    
    @pytest.mark.asyncio
    async def test_add_video_task(self, mock_config, mock_camera):
        """Test adding video task through convenience method."""
        app = VideoGrouperApp(mock_config, camera=mock_camera)
        
        try:
            convert_task = ConvertTask(file_path="/test/path/test.dav")
            
            await app.add_video_task(convert_task)
            
            assert app.video_processor.get_queue_size() == 1
        finally:
            # Ensure proper cleanup to prevent asyncio warnings
            await app.shutdown()
    
    @pytest.mark.asyncio
    async def test_add_youtube_task(self, mock_config, mock_camera):
        """Test adding YouTube task through convenience method."""
        app = VideoGrouperApp(mock_config, camera=mock_camera)

        try:
            upload_task = YoutubeUploadTask(group_dir="/test/path/group")

            await app.add_youtube_task(upload_task)

            assert app.upload_processor.get_queue_size() == 1
        finally:
            # Ensure proper cleanup to prevent asyncio warnings
            await app.shutdown()
    
    def test_get_queue_sizes(self, mock_config, mock_camera):
        """Test getting queue sizes."""
        app = VideoGrouperApp(mock_config, camera=mock_camera)
        
        queue_sizes = app.get_queue_sizes()
        
        assert 'download' in queue_sizes
        assert 'video' in queue_sizes
        assert 'youtube' in queue_sizes
        assert queue_sizes['download'] == 0
        assert queue_sizes['video'] == 0
        assert queue_sizes['youtube'] == 0
    
    def test_get_processor_status(self, mock_config, mock_camera):
        """Test getting processor status."""
        app = VideoGrouperApp(mock_config, camera=mock_camera)
        
        status = app.get_processor_status()
        
        assert 'state_auditor' in status
        assert 'camera_poller' in status
        assert 'download_processor' in status
        assert 'video_processor' in status
        assert 'upload_processor' in status
        
        # All should be stopped initially
        for processor_status in status.values():
            assert processor_status == 'stopped'
    
    @pytest.mark.asyncio
    async def test_integration_workflow(self, mock_config, mock_camera, temp_storage):
        """Test a complete workflow integration."""
        app = VideoGrouperApp(mock_config, camera=mock_camera)
        
        try:
            # Use mock paths instead of creating actual files
            group_dir = "/test/group"
            test_file = "/test/group/test.dav"
            
            # Create and add tasks
            recording_file = RecordingFile(
                start_time=datetime(2023, 1, 1, 10, 0, 0),
                end_time=datetime(2023, 1, 1, 10, 5, 0),
                file_path=test_file,
                metadata={'path': '/test.dav'}
            )
            
            convert_task = ConvertTask(file_path=test_file)
            upload_task = YoutubeUploadTask(group_dir=group_dir)
            
            # Add tasks to queues
            await app.add_download_task(recording_file)
            await app.add_video_task(convert_task)
            await app.add_youtube_task(upload_task)
            
            # Verify tasks were added
            queue_sizes = app.get_queue_sizes()
            assert queue_sizes['download'] == 1
            assert queue_sizes['video'] == 1
            assert queue_sizes['youtube'] == 1
        finally:
            # Ensure proper cleanup to prevent asyncio warnings
            await app.shutdown()
    
    @pytest.mark.asyncio
    async def test_error_handling_during_initialization(self, mock_config):
        """Test error handling during initialization."""
        # Test with invalid camera configuration
        mock_config.set('CAMERA', 'type', 'invalid_camera')
        
        with pytest.raises(ValueError, match="Unsupported camera type"):
            VideoGrouperApp(mock_config)
    
    @pytest.mark.asyncio
    async def test_camera_close_on_shutdown(self, mock_config, mock_camera):
        """Test that camera is properly closed on shutdown."""
        app = VideoGrouperApp(mock_config, camera=mock_camera)
        
        await app.initialize()
        await app.shutdown()
        
        mock_camera.close.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_storage_path_handling(self, mock_config, mock_camera):
        """Test storage path is properly handled."""
        app = VideoGrouperApp(mock_config, camera=mock_camera)
        
        try:
            # Storage path should be absolute
            assert os.path.isabs(app.storage_path)
            
            # All processors should have the same storage path
            for processor in app.processors:
                assert processor.storage_path == app.storage_path
        finally:
            # Ensure proper cleanup to prevent asyncio warnings
            await app.shutdown() 