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
        camera = DahuaCamera(mock_config)
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
        camera = DahuaCamera({
            "device_ip": "192.168.1.100",
            "username": "admin",
            "password": "admin",
            "storage_path": "test_path"
        }, client=mock_client)
        
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
        camera = DahuaCamera({
            "device_ip": "192.168.1.100",
            "username": "admin",
            "password": "admin",
            "storage_path": "test_path"
        }, client=mock_client)
        
        camera._is_connected = True
        camera._connection_events = []

        # Test the method
        result = await camera.check_availability()
        
        # Verify the method handled the exception correctly
        assert result is False
        assert camera._is_connected is False
        assert len(camera._connection_events) == 1
        assert camera._connection_events[0][1] == "connection error: Connection error"
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
        camera = DahuaCamera({
            "device_ip": "192.168.1.100",
            "username": "admin",
            "password": "admin",
            "storage_path": "test_path"
        }, client=mock_client)
        
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
        camera = DahuaCamera({
            "device_ip": "192.168.1.100",
            "username": "admin",
            "password": "admin",
            "storage_path": "test_path"
        }, client=mock_client)
        
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
    async def test_get_file_list_success(self):
        """Test successful file list retrieval."""
        # Create a mock client that will return a successful response
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = """object=1
path=test.dav&startTime=2024-01-01 12:00:00&endTime=2024-01-01 12:30:00"""
        mock_client.get.return_value = mock_response
        
        # Create the camera with the mock client
        camera = DahuaCamera({
            "device_ip": "192.168.1.100",
            "username": "admin",
            "password": "admin",
            "storage_path": "test_path"
        }, client=mock_client)

        files = await camera.get_file_list()
        assert len(files) == 1
        assert files[0]['path'] == 'test.dav'
        assert files[0]['startTime'] == '2024-01-01 12:00:00'
        assert files[0]['endTime'] == '2024-01-01 12:30:00'
        
        # Verify the mock was called with the correct URL
        mock_client.get.assert_called_once_with(
            "http://192.168.1.100/cgi-bin/loadfile.cgi?action=findFile&object=1",
            auth=mock_client.get.call_args[1]["auth"]
        )

    @pytest.mark.asyncio
    async def test_get_file_size_success(self):
        """Test successful file size retrieval."""
        # Create a mock client that will return a successful response
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "size=1024"
        mock_client.get.return_value = mock_response
        
        # Create the camera with the mock client
        camera = DahuaCamera({
            "device_ip": "192.168.1.100",
            "username": "admin",
            "password": "admin",
            "storage_path": "test_path"
        }, client=mock_client)

        size = await camera.get_file_size("test.dav")
        assert size == 1024
        
        # Verify the mock was called with the correct URL
        mock_client.get.assert_called_once_with(
            "http://192.168.1.100/cgi-bin/loadfile.cgi?action=getFileSize&object=test.dav",
            auth=mock_client.get.call_args[1]["auth"]
        )

    @pytest.mark.asyncio
    async def test_download_file_success(self, tmp_path):
        """Test successful file download."""
        # Create test data
        test_data = b"test data"
        
        # Create a mock response with an async generator for aiter_bytes
        async def mock_aiter_bytes():
            yield test_data
        
        # Create a mock client that will return a successful response
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = test_data
        mock_response.headers = {"content-length": str(len(test_data))}
        mock_response.aiter_bytes = mock_aiter_bytes
        mock_client.get.return_value = mock_response
        
        # Create the camera with the mock client
        camera = DahuaCamera({
            "device_ip": "192.168.1.100",
            "username": "admin",
            "password": "admin",
            "storage_path": "test_path"
        }, client=mock_client)

        local_path = tmp_path / "test.dav"
        success = await camera.download_file("test.dav", str(local_path))
        assert success is True
        assert local_path.read_bytes() == test_data
        
        # Verify the mock was called with the correct URL
        mock_client.get.assert_called_once_with(
            "http://192.168.1.100/cgi-bin/loadfile.cgi?action=downloadFile&object=test.dav",
            auth=mock_client.get.call_args[1]["auth"]
        )

class TestDahuaCameraRecording:
    """Tests for recording operations."""

    @pytest.mark.asyncio
    async def test_stop_recording_success(self):
        """Test successful recording stop."""
        # Create a mock client that will return a successful response
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.get.return_value = mock_response
        
        # Create the camera with the mock client
        camera = DahuaCamera({
            "device_ip": "192.168.1.100",
            "username": "admin",
            "password": "admin",
            "storage_path": "test_path"
        }, client=mock_client)

        success = await camera.stop_recording()
        assert success is True
        
        # Verify the mock was called with the correct URL
        mock_client.get.assert_called_once()
        call_args = mock_client.get.call_args
        assert call_args[0][0] == "http://192.168.1.100/cgi-bin/configManager.cgi?action=setConfig&RecordMode[0].Mode=0"
        assert isinstance(call_args[1]["auth"], httpx.DigestAuth)

    @pytest.mark.asyncio
    async def test_get_recording_status_recording(self):
        """Test recording status when recording is active."""
        # Create a mock client that will return a successful response
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "RecordMode[0].Mode=1"
        mock_client.get.return_value = mock_response
        
        # Create the camera with the mock client
        camera = DahuaCamera({
            "device_ip": "192.168.1.100",
            "username": "admin",
            "password": "admin",
            "storage_path": "test_path"
        }, client=mock_client)

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
        camera = DahuaCamera({
            "device_ip": "192.168.1.100",
            "username": "admin",
            "password": "admin",
            "storage_path": "test_path"
        }, client=mock_client)

        assert await camera.get_recording_status() is False
        
        # Verify the mock was called with the correct URL
        mock_client.get.assert_called_once()
        call_args = mock_client.get.call_args
        assert call_args[0][0] == "http://192.168.1.100/cgi-bin/configManager.cgi?action=getConfig&name=RecordMode"
        assert isinstance(call_args[1]["auth"], httpx.DigestAuth)

class TestDahuaCameraDeviceInfo:
    """Tests for device info operations."""

    @pytest.mark.asyncio
    async def test_get_device_info_success(self):
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
        camera = DahuaCamera({
            "device_ip": "192.168.1.100",
            "username": "admin",
            "password": "admin",
            "storage_path": "test_path"
        }, client=mock_client)

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
        camera = DahuaCamera({
            "device_ip": "192.168.1.100",
            "username": "admin",
            "password": "admin",
            "storage_path": "test_path"
        }, client=mock_client)

        info = await camera.get_device_info()
        assert info == {}
        
        # Verify the mock was called with the correct URL
        mock_client.get.assert_called_once()
        call_args = mock_client.get.call_args
        assert call_args[0][0] == "http://192.168.1.100/cgi-bin/magicBox.cgi?action=getSystemInfo"
        assert isinstance(call_args[1]["auth"], httpx.DigestAuth)

def test_connection_events_property():
    """Test connection events property."""
    camera = DahuaCamera({
        "device_ip": "192.168.1.100",
        "username": "admin",
        "password": "admin",
        "storage_path": "test_path"
    })
    assert isinstance(camera.connection_events, list)

def test_is_connected_property():
    """Test is connected property."""
    camera = DahuaCamera({
        "device_ip": "192.168.1.100",
        "username": "admin",
        "password": "admin",
        "storage_path": "test_path"
    })
    assert isinstance(camera.is_connected, bool) 