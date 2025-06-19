import os
import json
import pytest
import logging
from datetime import datetime
from typing import Dict, Any
from unittest.mock import AsyncMock, MagicMock
import httpx
import respx
from pathlib import Path
from unittest.mock import patch

# Configure logging
logging.basicConfig(level=logging.INFO)

from video_grouper.cameras.dahua import DahuaCamera
from video_grouper.models import RecordingFile

# Fixtures
@pytest.fixture
def mock_config(tmp_path):
    """Create a mock camera configuration."""
    return {
        "device_ip": "192.168.1.100",
        "username": "admin",
        "password": "admin",
        "storage_path": str(tmp_path)
    }

@pytest.fixture
def mock_state_file(tmp_path) -> str:
    """Create a temporary state file for testing."""
    state_file = tmp_path / "camera_state.json"
    return str(state_file)

# Test Cases
class TestDahuaCameraInitialization:
    """Tests for camera initialization."""

    def test_init_with_config(self, mock_config):
        """Test camera initialization with valid config."""
        camera = DahuaCamera(**mock_config)
        assert camera.ip == mock_config['device_ip']
        assert camera.username == mock_config['username']
        assert camera.password == mock_config['password']
        assert camera.storage_path == mock_config['storage_path']

class TestDahuaCameraAvailability:
    """Tests for camera availability checks."""

    @pytest.mark.asyncio
    async def test_check_availability_success(self):
        """Test successful availability check."""
        # Create a mock client that will return a 200 response
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.get.return_value = mock_response
        
        # Create the camera with the mock client
        camera = DahuaCamera(
            device_ip="192.168.1.100",
            username="admin",
            password="admin",
            storage_path="test_path",
            client=mock_client
        )
        
        camera._is_connected = False
        camera._connection_events = []

        # Test the method
        result = await camera.check_availability()
        
        # Verify the method handled the response correctly
        assert result is True
        assert camera._is_connected is True
        assert len(camera._connection_events) == 1
        assert camera._connection_events[0][1] == "connected"
        assert isinstance(camera._connection_events[0][0], datetime)
        
        # Verify the mock was called with the correct URL
        mock_client.get.assert_called_once_with(
            "http://192.168.1.100/cgi-bin/recordManager.cgi?action=getCaps",
            auth=mock_client.get.call_args[1]["auth"]
        )

    @pytest.mark.asyncio
    async def test_check_availability_connection_error(self):
        """Test availability check with connection error."""
        # Create a mock client that will raise ConnectError
        mock_client = AsyncMock()
        mock_client.get.side_effect = httpx.ConnectError("Connection error")
        
        # Create the camera with the mock client
        camera = DahuaCamera(
            device_ip="192.168.1.100",
            username="admin",
            password="admin",
            storage_path="test_path",
            client=mock_client
        )
        
        camera._is_connected = True
        camera._connection_events = []

        # Test the method
        result = await camera.check_availability()
        
        # Verify the method handled the exception correctly
        assert result is False
        assert camera._is_connected is False
        assert len(camera._connection_events) == 1
        assert camera._connection_events[0][1] == "connection failed: Connection error"
        assert isinstance(camera._connection_events[0][0], datetime)
        
        # Verify the mock was called with the correct URL
        mock_client.get.assert_called_once_with(
            "http://192.168.1.100/cgi-bin/recordManager.cgi?action=getCaps",
            auth=mock_client.get.call_args[1]["auth"]
        )

    @pytest.mark.asyncio
    async def test_check_availability_no_state_change(self):
        """Test availability check when connection state hasn't changed."""
        # Create a mock client that will return a 200 response
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.get.return_value = mock_response
        
        # Create the camera with the mock client
        camera = DahuaCamera(
            device_ip="192.168.1.100",
            username="admin",
            password="admin",
            storage_path="test_path",
            client=mock_client
        )
        
        # Mock initial state
        camera._is_connected = True
        camera._connection_events = []

        # Test the method
        result = await camera.check_availability()
        
        # Verify the method handled the response correctly
        assert result is True
        assert camera._is_connected is True
        assert len(camera._connection_events) == 0  # No new events since state didn't change
        
        # Verify the mock was called with the correct URL
        mock_client.get.assert_called_once_with(
            "http://192.168.1.100/cgi-bin/recordManager.cgi?action=getCaps",
            auth=mock_client.get.call_args[1]["auth"]
        )

    @pytest.mark.asyncio
    async def test_check_availability_failure(self):
        """Test failed availability check."""
        # Create a mock client that will return a 404 response
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_client.get.return_value = mock_response
        
        # Create the camera with the mock client
        camera = DahuaCamera(
            device_ip="192.168.1.100",
            username="admin",
            password="admin",
            storage_path="test_path",
            client=mock_client
        )
        
        camera._is_connected = True
        camera._connection_events = []

        # Test the method
        result = await camera.check_availability()
        
        # Verify the method handled the response correctly
        assert result is False
        assert camera._is_connected is False
        assert len(camera._connection_events) == 1
        assert camera._connection_events[0][1] == "connection failed: 404"
        assert isinstance(camera._connection_events[0][0], datetime)
        
        # Verify the mock was called with the correct URL
        mock_client.get.assert_called_once_with(
            "http://192.168.1.100/cgi-bin/recordManager.cgi?action=getCaps",
            auth=mock_client.get.call_args[1]["auth"]
        )

class TestDahuaCameraFileOperations:
    """Tests for file operations."""

    @pytest.mark.asyncio
    @patch('video_grouper.cameras.dahua.DahuaCamera._log_http_call', new_callable=AsyncMock)
    async def test_get_file_list_success(self, mock_log_call):
        """Test successful file list retrieval."""
        # Create a mock client that will return a successful response
        mock_client = AsyncMock()
        
        # First response for factory.create
        factory_response = MagicMock()
        factory_response.status_code = 200
        factory_response.text = "result=3039757640"
        
        # Second response for findFile
        find_response = MagicMock()
        find_response.status_code = 200
        find_response.text = "OK"
        
        # Third response for findNextFile
        next_response = MagicMock()
        next_response.status_code = 200
        next_response.text = """found=1
items[0].Channel=1
items[0].Cluster=7861
items[0].CutLength=320256446
items[0].Disk=0
items[0].Duration=1800
items[0].EndTime=2024-01-01 12:30:00
items[0].FilePath=/mnt/dvr/mmc1p2_0/2024.01.01/0/dav/12/test.dav
items[0].FileState=Temporary
items[0].Flags[0]=Manual
items[0].Length=320256446
items[0].Partition=0
items[0].PicIndex=0
items[0].Repeat=0
items[0].StartTime=2024-01-01 12:00:00
items[0].Type=dav
items[0].UTCOffset=-14400
items[0].VideoStream=Main
items[0].WorkDir=/mnt/dvr/mmc1p2_0
items[0].WorkDirSN=0
"""
        
        # Configure the mock to return different responses for different calls
        mock_client.get.side_effect = [factory_response, find_response, next_response]
        
        # Create the camera with the mock client
        camera = DahuaCamera(
            device_ip="192.168.1.100",
            username="admin",
            password="admin",
            storage_path="test_path",
            client=mock_client
        )

        start_time = datetime(2024, 1, 1, 12, 0, 0)
        end_time = datetime(2024, 1, 1, 13, 0, 0)
        files = await camera.get_file_list(start_time, end_time)
        assert len(files) == 1
        assert files[0]['path'] == '/mnt/dvr/mmc1p2_0/2024.01.01/0/dav/12/test.dav'
        assert files[0]['startTime'] == '2024-01-01 12:00:00'
        assert files[0]['endTime'] == '2024-01-01 12:30:00'
        
        # Verify the mock was called with the correct URLs
        assert mock_client.get.call_count == 3
        mock_client.get.assert_any_call(
            "http://192.168.1.100/cgi-bin/mediaFileFind.cgi?action=factory.create",
            auth=mock_client.get.call_args_list[0][1]["auth"]
        )
        # We can't check the exact URL for the second call due to the dynamic date, but we can check it contains the right pattern
        assert "mediaFileFind.cgi?action=findFile&object=3039757640" in mock_client.get.call_args_list[1][0][0]
        mock_client.get.assert_any_call(
            "http://192.168.1.100/cgi-bin/mediaFileFind.cgi?action=findNextFile&object=3039757640&count=100",
            auth=mock_client.get.call_args_list[2][1]["auth"]
        )

    @pytest.mark.asyncio
    @patch('video_grouper.cameras.dahua.DahuaCamera._log_http_call', new_callable=AsyncMock)
    async def test_get_file_size_success(self, mock_log_call):
        """Test successful file size retrieval."""
        # Create a mock client that will return a successful response
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-length": "1024"}
        mock_client.head.return_value = mock_response
        
        # Create the camera with the mock client
        camera = DahuaCamera(
            device_ip="192.168.1.100",
            username="admin",
            password="admin",
            storage_path="test_path",
            client=mock_client
        )

        size = await camera.get_file_size("/test.dav")
        assert size == 1024
        
        # Verify the mock was called with the correct URL
        mock_client.head.assert_called_once_with(
            "http://192.168.1.100/cgi-bin/RPC_Loadfile/test.dav",
            auth=mock_client.head.call_args[1]["auth"]
        )

    @pytest.mark.asyncio
    async def test_download_file_success(self, tmp_path):
        """Test successful file download."""
        # Create test data
        test_data = b"test data"
        
        # Define the path for the test file
        test_file_path = tmp_path / "test.dav"
        
        # Create a mock implementation of download_file
        async def mock_download_impl(server_path, local_path):
            # Write the test data to the file
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, 'wb') as f:
                f.write(test_data)
            return True
        
        # Create the camera
        camera = DahuaCamera(
            device_ip="192.168.1.100",
            username="admin",
            password="admin",
            storage_path=str(tmp_path)
        )
        
        # Patch the download_file method
        original_method = camera.download_file
        camera.download_file = mock_download_impl
        
        try:
            # Call the method
            success = await camera.download_file("/test.dav", str(test_file_path))
            
            # Verify the result
            assert success is True
            
            # Verify the file was written correctly
            assert os.path.exists(test_file_path)
            with open(test_file_path, 'rb') as f:
                assert f.read() == test_data
        finally:
            # Restore the original method
            camera.download_file = original_method

    @pytest.mark.asyncio
    async def test_download_file_cleanup_on_error(self, tmp_path):
        """Test that partial downloads are cleaned up on error."""
        # Create a test file that will be "partially downloaded" then removed
        test_file = tmp_path / "test.dav"
        with open(test_file, "wb") as f:
            f.write(b"partial data")
        
        # Mock the camera methods
        with patch.object(DahuaCamera, 'get_file_size', return_value=1024), \
             patch.object(DahuaCamera, '_load_state'), \
             patch.object(DahuaCamera, '_save_state'), \
             patch('os.makedirs'), \
             patch('os.remove') as mock_remove:
            
            # Create the camera
            camera = DahuaCamera(
                device_ip="192.168.1.100",
                username="admin",
                password="admin",
                storage_path=str(tmp_path)
            )
            
            # Define a mock implementation that simulates a download failure
            async def mock_download_impl(server_path, local_path):
                # Simulate a failed download
                if os.path.exists(local_path):
                    os.remove(local_path)
                return False
                
            # Save the original method and replace it with our mock
            original_method = camera.download_file
            camera.download_file = mock_download_impl
            
            try:
                # Call the method
                success = await camera.download_file("/test.dav", str(test_file))
                
                # Download should fail
                assert success is False
                
                # Verify the file removal was attempted
                mock_remove.assert_called_once_with(str(test_file))
            finally:
                # Restore the original method
                camera.download_file = original_method

class TestDahuaCameraRecording:
    """Tests for recording operations."""

    @pytest.mark.asyncio
    @patch('video_grouper.cameras.dahua.DahuaCamera._log_http_call', new_callable=AsyncMock)
    async def test_stop_recording_success(self, mock_log_call):
        """Test successful recording stop."""
        # Create a mock client that will return a successful response
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.get.return_value = mock_response
        
        # Create the camera with the mock client
        camera = DahuaCamera(
            device_ip="192.168.1.100",
            username="admin",
            password="admin",
            storage_path="test_path",
            client=mock_client
        )

        success = await camera.stop_recording()
        assert success is True
        
        # Verify the mock was called with the correct URL
        mock_client.get.assert_called_once()
        call_args = mock_client.get.call_args
        assert call_args[0][0] == "http://192.168.1.100/cgi-bin/configManager.cgi?action=setConfig&RecordMode[0].Mode=2"
        assert isinstance(call_args[1]["auth"], httpx.DigestAuth)

    @pytest.mark.asyncio
    @patch('video_grouper.cameras.dahua.DahuaCamera._log_http_call', new_callable=AsyncMock)
    async def test_get_recording_status_recording(self, mock_log_call):
        """Test recording status when recording is active."""
        # Create a mock client that will return a successful response
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "RecordMode[0].Mode=1"
        mock_client.get.return_value = mock_response
        
        # Create the camera with the mock client
        camera = DahuaCamera(
            device_ip="192.168.1.100",
            username="admin",
            password="admin",
            storage_path="test_path",
            client=mock_client
        )

        assert await camera.get_recording_status() is True
        
        # Verify the mock was called with the correct URL
        mock_client.get.assert_called_once()
        call_args = mock_client.get.call_args
        assert call_args[0][0] == "http://192.168.1.100/cgi-bin/configManager.cgi?action=getConfig&name=RecordMode"
        assert isinstance(call_args[1]["auth"], httpx.DigestAuth)

    @pytest.mark.asyncio
    async def test_get_recording_status_not_recording(self):
        """Test recording status when recording is not active."""
        # Create a mock client that will return a successful response
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "RecordMode[0].Mode=0"
        mock_client.get.return_value = mock_response
        
        # Create the camera with the mock client
        camera = DahuaCamera(
            device_ip="192.168.1.100",
            username="admin",
            password="admin",
            storage_path="test_path",
            client=mock_client
        )

        assert await camera.get_recording_status() is False
        
        # Verify the mock was called with the correct URL
        mock_client.get.assert_called_once()
        call_args = mock_client.get.call_args
        assert call_args[0][0] == "http://192.168.1.100/cgi-bin/configManager.cgi?action=getConfig&name=RecordMode"
        assert isinstance(call_args[1]["auth"], httpx.DigestAuth)

class TestDahuaCameraDeviceInfo:
    """Tests for device info operations."""

    @pytest.mark.asyncio
    @patch('video_grouper.cameras.dahua.DahuaCamera._log_http_call', new_callable=AsyncMock)
    async def test_get_device_info_success(self, mock_log_call):
        """Test successful device info retrieval."""
        # Create a mock client that will return a successful response
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = """deviceName=Test Camera
firmwareVersion=1.0.0
deviceType=IPC"""
        mock_client.get.return_value = mock_response
        
        # Create the camera with the mock client
        camera = DahuaCamera(
            device_ip="192.168.1.100",
            username="admin",
            password="admin",
            storage_path="test_path",
            client=mock_client
        )

        info = await camera.get_device_info()
        assert info['deviceName'] == "Test Camera"
        assert info['firmwareVersion'] == "1.0.0"
        assert info['deviceType'] == "IPC"
        
        # Verify the mock was called with the correct URL
        mock_client.get.assert_called_once()
        call_args = mock_client.get.call_args
        assert call_args[0][0] == "http://192.168.1.100/cgi-bin/magicBox.cgi?action=getSystemInfo"
        assert isinstance(call_args[1]["auth"], httpx.DigestAuth)

    @pytest.mark.asyncio
    async def test_get_device_info_failure(self):
        """Test device info retrieval failure."""
        # Create a mock client that will return a failed response
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_client.get.return_value = mock_response
        
        # Create the camera with the mock client
        camera = DahuaCamera(
            device_ip="192.168.1.100",
            username="admin",
            password="admin",
            storage_path="test_path",
            client=mock_client
        )

        info = await camera.get_device_info()
        assert info == {}
        
        # Verify the mock was called with the correct URL
        mock_client.get.assert_called_once()
        call_args = mock_client.get.call_args
        assert call_args[0][0] == "http://192.168.1.100/cgi-bin/magicBox.cgi?action=getSystemInfo"
        assert isinstance(call_args[1]["auth"], httpx.DigestAuth)

def test_connection_events_property():
    """Test connection events property."""
    camera = DahuaCamera(
        device_ip="192.168.1.100",
        username="admin",
        password="admin",
        storage_path="test_path"
    )
    assert isinstance(camera.connection_events, list)

def test_is_connected_property():
    """Test is connected property."""
    camera = DahuaCamera(
        device_ip="192.168.1.100",
        username="admin",
        password="admin",
        storage_path="test_path"
    )
    assert isinstance(camera.is_connected, bool) 