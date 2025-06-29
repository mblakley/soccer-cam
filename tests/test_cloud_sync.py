import os
import json
import pytest
import tempfile
import configparser
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open

from video_grouper.api_integrations.cloud_sync import CloudSync, GoogleAuthProvider


@pytest.fixture
def sample_config():
    """Create a sample config file for testing."""
    # Mock the config file content
    config_content = """[CAMERA]
device_ip = 192.168.1.100
username = admin
password = password123

[STORAGE]
path = /path/to/storage

[APP]
timezone = UTC
"""
    
    # Mock file path and content
    mock_path = Path('/mock/config/path.ini')
    
    with patch('builtins.open', mock_open(read_data=config_content)):
        with patch('pathlib.Path.exists', return_value=True):
            yield mock_path


class TestCloudSync:
    """Tests for the CloudSync class."""
    
    def test_init(self):
        """Test initialization of CloudSync."""
        # Test with default endpoint
        cloud_sync = CloudSync()
        assert cloud_sync.endpoint_url == "https://example.com/api/sync"
        assert cloud_sync.username is None
        assert cloud_sync.password is None
        
        # Test with custom endpoint
        custom_endpoint = "https://custom.example.com/api/sync"
        cloud_sync = CloudSync(custom_endpoint)
        assert cloud_sync.endpoint_url == custom_endpoint
    
    def test_set_credentials(self):
        """Test setting credentials."""
        cloud_sync = CloudSync()
        cloud_sync.set_credentials("test_user", "test_password")
        assert cloud_sync.username == "test_user"
        assert cloud_sync.password == "test_password"
    
    def test_encrypt_config(self):
        """Test encryption of configuration data."""
        cloud_sync = CloudSync()
        
        # Sample config data
        config_data = {
            "CAMERA": {
                "device_ip": "192.168.1.100",
                "username": "admin",
                "password": "password123"
            }
        }
        
        # Encrypt the data
        encrypted_data = cloud_sync.encrypt_config(config_data)
        
        # Check that the encrypted data has the expected structure
        assert "encrypted_data" in encrypted_data
        assert "encrypted_key" in encrypted_data
        assert "iv" in encrypted_data
        assert "algorithm" in encrypted_data
        assert encrypted_data["algorithm"] == "AES-256-CBC+RSA-OAEP"
    
    @pytest.mark.asyncio
    async def test_upload_config_missing_credentials(self):
        """Test upload_config with missing credentials."""
        cloud_sync = CloudSync()
        result = await cloud_sync.upload_config(Path("dummy_path"))
        assert result is False
    
    @pytest.mark.asyncio
    async def test_upload_config_success(self, sample_config):
        """Test successful upload_config."""
        with patch.object(CloudSync, '_make_async_request') as mock_request:
            # Setup mock response
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_request.return_value = mock_response
            
            # Create CloudSync instance with credentials
            cloud_sync = CloudSync()
            cloud_sync.set_credentials("test_user", "test_password")
            
            # Call upload_config
            result = await cloud_sync.upload_config(sample_config)
            
            # Check result
            assert result is True
            
            # Verify _make_async_request was called with the right parameters
            mock_request.assert_called_once()
            call_args = mock_request.call_args[0][0]
            assert "username" in call_args
            assert "encrypted_data" in call_args
            assert call_args["username"] == "test_user"
    
    @pytest.mark.asyncio
    async def test_upload_config_failure(self, sample_config):
        """Test failed upload_config."""
        with patch.object(CloudSync, '_make_async_request') as mock_request:
            # Setup mock response
            mock_response = MagicMock()
            mock_response.status_code = 404
            mock_request.return_value = mock_response
            
            # Create CloudSync instance with credentials
            cloud_sync = CloudSync()
            cloud_sync.set_credentials("test_user", "test_password")
            
            # Call upload_config
            result = await cloud_sync.upload_config(sample_config)
            
            # Check result
            assert result is False


class TestGoogleAuthProvider:
    """Tests for the GoogleAuthProvider class."""
    
    @pytest.mark.asyncio
    async def test_authenticate(self):
        """Test Google authentication."""
        auth_result = await GoogleAuthProvider.authenticate()
        
        # Check that the auth result has the expected structure
        assert "access_token" in auth_result
        assert "email" in auth_result
        assert auth_result["access_token"] == "mock_token"
        assert auth_result["email"] == "user@example.com" 