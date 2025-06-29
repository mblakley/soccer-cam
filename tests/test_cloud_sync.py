import pytest
import asyncio
from unittest.mock import patch, MagicMock, mock_open
from pathlib import Path
import configparser
import json
import base64

from video_grouper.api_integrations.cloud_sync import CloudSync, GoogleAuthProvider
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

@pytest.fixture
def cloud_sync():
    """Fixture for CloudSync."""
    return CloudSync(endpoint_url="https://fake-endpoint.com/sync")

@pytest.fixture
def mock_config_file():
    """Fixture for a mock config file path."""
    return Path("/fake/path/config.ini")

def test_cloud_sync_initialization(cloud_sync):
    """Test that CloudSync initializes with the correct endpoint."""
    assert cloud_sync.endpoint_url == "https://fake-endpoint.com/sync"

def test_set_credentials(cloud_sync):
    """Test setting credentials."""
    cloud_sync.set_credentials("testuser", "testpass")
    assert cloud_sync.username == "testuser"
    assert cloud_sync.password == "testpass"

def test_encrypt_config():
    """Test the encryption of a configuration dictionary."""
    # This test is more complex as it involves cryptography. We'll test the structure
    # and decryptability of the output.
    
    # 1. Generate a real key pair for this test
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend()
    )
    public_key = private_key.public_key()
    
    # 2. Patch the key generation inside encrypt_config to use our public key
    with patch('cryptography.hazmat.primitives.asymmetric.rsa.generate_private_key') as mock_gen_key:
        mock_gen_key.return_value.public_key.return_value = public_key

        cs = CloudSync()
        config_data = {"section1": {"key1": "value1"}}
        
        # 3. Encrypt the data
        encrypted_package = cs.encrypt_config(config_data)

        # 4. Assert the structure of the output
        assert "encrypted_data" in encrypted_package
        assert "encrypted_key" in encrypted_package
        assert "iv" in encrypted_package
        assert encrypted_package["algorithm"] == "AES-256-CBC+RSA-OAEP"

        # 5. Decrypt and verify the content
        # Decode from base64
        encrypted_key_bytes = base64.b64decode(encrypted_package["encrypted_key"])
        iv_bytes = base64.b64decode(encrypted_package["iv"])
        encrypted_data_bytes = base64.b64decode(encrypted_package["encrypted_data"])

        # Decrypt the AES key with our private key
        aes_key = private_key.decrypt(
            encrypted_key_bytes,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None
            )
        )
        
        # Decrypt the data with the AES key
        cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv_bytes), backend=default_backend())
        decryptor = cipher.decryptor()
        decrypted_padded_data = decryptor.update(encrypted_data_bytes) + decryptor.finalize()
        
        # Unpad the data (PKCS7)
        pad_len = decrypted_padded_data[-1]
        decrypted_data_bytes = decrypted_padded_data[:-pad_len]
        
        decrypted_config = json.loads(decrypted_data_bytes.decode('utf-8'))
        
        assert decrypted_config == config_data

@pytest.mark.asyncio
async def test_upload_config_no_credentials(cloud_sync, mock_config_file):
    """Test upload fails if credentials are not set."""
    result = await cloud_sync.upload_config(mock_config_file)
    assert result is False

@pytest.mark.asyncio
@patch('video_grouper.api_integrations.cloud_sync.CloudSync._make_async_request')
async def test_upload_config_success(mock_request, cloud_sync, mock_config_file):
    """Test a successful config upload."""
    # Prepare mock config
    config = configparser.ConfigParser()
    config['DEFAULT'] = {'Server': 'localhost'}
    
    # Mock file reading
    m_open = mock_open(read_data="[DEFAULT]\nServer = localhost")
    with patch('builtins.open', m_open):
        # Set credentials
        cloud_sync.set_credentials("testuser", "testpass")
    
        # Mock successful response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_request.return_value = mock_response
    
        result = await cloud_sync.upload_config(mock_config_file)
    
        assert result is True
        mock_request.assert_called_once()
        # You could add more assertions here to inspect the payload sent

@pytest.mark.asyncio
@patch('video_grouper.api_integrations.cloud_sync.CloudSync._make_async_request')
async def test_upload_config_failure(mock_request, cloud_sync, mock_config_file):
    """Test a failed config upload."""
    m_open = mock_open(read_data="[DEFAULT]\nServer = localhost")
    with patch('builtins.open', m_open):
        cloud_sync.set_credentials("testuser", "testpass")
    
        # Mock failed response
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_request.return_value = mock_response
    
        result = await cloud_sync.upload_config(mock_config_file)
    
        assert result is False

@pytest.mark.asyncio
@patch('requests.post')
async def test_make_async_request(mock_post, cloud_sync):
    """Test the helper for making async requests."""
    cloud_sync.set_credentials("user", "pass")
    
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_post.return_value = mock_response
    
    payload = {"data": "test"}
    response = await cloud_sync._make_async_request(payload)
    
    assert response.status_code == 200
    mock_post.assert_called_with(
        cloud_sync.endpoint_url,
        json=payload,
        auth=("user", "pass"),
        timeout=10
    )

# --- GoogleAuthProvider Tests ---

@pytest.mark.asyncio
async def test_google_auth_provider():
    """Test the placeholder GoogleAuthProvider."""
    result = await GoogleAuthProvider.authenticate()
    assert result is not None
    assert result["access_token"] == "mock_token"
    assert result["email"] == "user@example.com"
