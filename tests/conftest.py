import pytest
import asyncio
import os
from unittest.mock import Mock, patch, AsyncMock

@pytest.fixture(autouse=True)
def mock_ffmpeg():
    """Mock ffmpeg command for all tests."""
    with patch('asyncio.create_subprocess_exec') as mock:
        process = Mock()
        process.communicate = Mock(return_value=(b"100.0", b""))
        process.returncode = 0
        mock.return_value = process
        yield mock

@pytest.fixture(autouse=True)
def mock_file_system():
    """Mock file system operations for all tests."""
    with patch('os.path.exists') as mock_exists, \
         patch('os.path.getsize') as mock_getsize, \
         patch('os.makedirs') as mock_makedirs, \
         patch('os.access') as mock_access:
        
        mock_exists.return_value = True
        mock_getsize.return_value = 1024 * 1024  # 1MB
        mock_makedirs.return_value = None
        mock_access.return_value = True
        
        yield {
            'exists': mock_exists,
            'getsize': mock_getsize,
            'makedirs': mock_makedirs,
            'access': mock_access
        }

@pytest.fixture(autouse=True)
def mock_httpx():
    """Mock httpx client for all tests."""
    with patch('httpx.AsyncClient') as mock:
        client = AsyncMock()
        response = Mock()
        response.status_code = 200
        response.text = "object=123"
        response.headers = {'content-length': '1048576'}
        client.__aenter__.return_value.get.return_value = response
        mock.return_value = client
        yield mock 