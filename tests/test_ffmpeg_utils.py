import os
import asyncio
import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock, AsyncMock, mock_open
from video_grouper.ffmpeg_utils import (
    async_convert_file, 
    get_video_duration, 
    verify_ffmpeg_install, 
    create_screenshot
)
import tempfile

@pytest.fixture
def mock_logger():
    """Mocks the logger used in ffmpeg_utils to prevent console output during tests."""
    with patch('video_grouper.ffmpeg_utils.logger', MagicMock()) as mock:
        yield mock

@pytest.fixture
def mock_ffmpeg_subprocess():
    """Mocks asyncio.create_subprocess_exec to avoid actual FFmpeg/FFprobe calls."""
    with patch('asyncio.create_subprocess_exec', new_callable=AsyncMock) as mock:
        process = mock.return_value
        # Configure the mock process to simulate a successful command execution by default
        process.returncode = 0
        process.communicate = AsyncMock(return_value=(b'', b''))
        process.wait = AsyncMock(return_value=None)
        # Mock stdout for progress parsing if needed
        process.stdout.readline = AsyncMock(return_value=b'')
        yield mock

@pytest.fixture
def mock_file_ops():
    """Mocks file system operations to isolate tests from the actual filesystem."""
    with patch('os.path.exists', return_value=True) as mock_exists, \
         patch('os.remove') as mock_remove, \
         patch('os.path.getsize', return_value=1024) as mock_getsize:
        yield {
            "exists": mock_exists,
            "remove": mock_remove,
            "getsize": mock_getsize
        }

@pytest.mark.asyncio
async def test_get_video_duration_success(mock_ffmpeg_subprocess):
    """Verifies that get_video_duration correctly parses ffprobe's output."""
    mock_process = mock_ffmpeg_subprocess.return_value
    # Simulate ffprobe outputting a duration
    mock_process.communicate.return_value = (b"123.45\n", b"")
    
    duration = await get_video_duration("dummy.mp4")

    assert duration == 123.45
    mock_ffmpeg_subprocess.assert_called_once()

@pytest.mark.asyncio
async def test_get_video_duration_failure(mock_ffmpeg_subprocess):
    """Ensures get_video_duration returns None when ffprobe fails."""
    mock_process = mock_ffmpeg_subprocess.return_value
    mock_process.returncode = 1
    mock_process.communicate.return_value = (b"", b"ffprobe error")

    duration = await get_video_duration("dummy.mp4")

    assert duration is None

@pytest.mark.asyncio
async def test_async_convert_file_success(mock_ffmpeg_subprocess, mock_file_ops, mock_logger):
    """Tests the successful conversion of a .dav file to .mp4."""
    dav_path = "test.dav"
    mock_file_ops['exists'].return_value = True

    result_path = await async_convert_file(dav_path)

    # Verify that FFmpeg was called to perform the conversion
    assert mock_ffmpeg_subprocess.called
    # Verify the function returns the correct path on success
    assert result_path == "test.mp4"

@pytest.mark.asyncio
async def test_async_convert_file_ffmpeg_failure(mock_ffmpeg_subprocess, mock_file_ops, mock_logger):
    """Tests that the function returns None if FFmpeg fails."""
    mock_process = mock_ffmpeg_subprocess.return_value
    mock_process.returncode = 1
    mock_process.communicate.return_value = (b"", b"ffmpeg error")

    dav_path = "test.dav"
    mock_file_ops['exists'].return_value = True

    result_path = await async_convert_file(dav_path)
        
    # Verify that the function returns None on failure
    assert result_path is None

@pytest.mark.asyncio
async def test_verify_ffmpeg_install_success(mock_ffmpeg_subprocess):
    """Tests that ffmpeg installation is correctly verified when the command succeeds."""
    assert await verify_ffmpeg_install() is True
    mock_ffmpeg_subprocess.assert_called_once()

@pytest.mark.asyncio
async def test_verify_ffmpeg_install_failure(mock_ffmpeg_subprocess):
    """Tests that ffmpeg installation verification fails when the command fails."""
    mock_process = mock_ffmpeg_subprocess.return_value
    mock_process.returncode = 1
    mock_process.communicate.return_value = (b"", b"ffmpeg not found")
    
    assert await verify_ffmpeg_install() is False

@pytest.mark.asyncio
async def test_create_screenshot_success(mock_ffmpeg_subprocess, mock_file_ops):
    """Tests the successful creation of a video screenshot."""
    video_path = "test.mp4"
    screenshot_path = "test.jpg"
    
    success = await create_screenshot(video_path, screenshot_path)
    
    assert success is True
    mock_ffmpeg_subprocess.assert_called_once()
    args, _ = mock_ffmpeg_subprocess.call_args
    # Check that the correct ffmpeg command was constructed
    assert 'ffmpeg' in args[0]
    assert '-i' in args
    assert video_path in args
    assert screenshot_path in args

@pytest.mark.asyncio
async def test_create_screenshot_failure(mock_ffmpeg_subprocess, mock_file_ops):
    """Tests that screenshot creation returns False when FFmpeg fails."""
    mock_process = mock_ffmpeg_subprocess.return_value
    mock_process.returncode = 1
    mock_process.communicate.return_value = (b"", b"ffmpeg error")
    
    success = await create_screenshot("test.mp4", "test.jpg")
        
    assert success is False
    mock_ffmpeg_subprocess.assert_called_once() 