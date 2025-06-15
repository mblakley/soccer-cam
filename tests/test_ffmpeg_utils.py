import os
import pytest
import asyncio
from datetime import datetime
from unittest.mock import patch, MagicMock, AsyncMock
from video_grouper.ffmpeg_utils import verify_mp4_duration, run_ffmpeg, async_convert_file

@pytest.fixture
def mock_subprocess():
    with patch('asyncio.create_subprocess_exec') as mock:
        yield mock

@pytest.fixture
def mock_file():
    with patch('builtins.open', MagicMock()) as mock:
        yield mock

@pytest.mark.asyncio
async def test_verify_mp4_duration_success(mock_subprocess):
    # Mock successful ffprobe response
    mock_process = AsyncMock()
    mock_process.communicate.return_value = (b"10.5", b"")
    mock_process.returncode = 0
    mock_subprocess.return_value = mock_process

    result = await verify_mp4_duration("test.mp4", 10.5)
    assert result is True

@pytest.mark.asyncio
async def test_verify_mp4_duration_failure(mock_subprocess):
    # Mock ffprobe failure
    mock_process = AsyncMock()
    mock_process.communicate.return_value = (b"", b"Error: Invalid file")
    mock_process.returncode = 1
    mock_subprocess.return_value = mock_process

    with patch('os.path.exists', return_value=True):
        result = await verify_mp4_duration("test.dav", "test.mp4")
        assert result is False

@pytest.mark.asyncio
async def test_verify_mp4_duration_within_tolerance(mock_subprocess):
    # Mock ffprobe response with duration within tolerance
    mock_process = AsyncMock()
    mock_process.communicate.return_value = (b"10.4", b"")
    mock_process.returncode = 0
    mock_subprocess.return_value = mock_process

    result = await verify_mp4_duration("test.mp4", 10.5, tolerance=0.2)
    assert result is True

@pytest.mark.asyncio
async def test_run_ffmpeg_success(mock_subprocess):
    # Mock successful ffmpeg execution
    mock_process = AsyncMock()
    mock_process.wait = AsyncMock()
    mock_subprocess.return_value = mock_process

    command = ["ffmpeg", "-i", "input.mp4", "output.mp4"]
    await run_ffmpeg(command)
    
    mock_subprocess.assert_called_once_with(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL
    )

@pytest.mark.asyncio
async def test_run_ffmpeg_failure(mock_subprocess):
    # Mock ffmpeg execution failure
    mock_process = AsyncMock()
    mock_process.wait.side_effect = Exception("FFmpeg failed")
    mock_subprocess.return_value = mock_process

    command = ["ffmpeg", "-i", "input.mp4", "output.mp4"]
    await run_ffmpeg(command)  # Should not raise exception

@pytest.mark.asyncio
async def test_async_convert_file_success(mock_subprocess, mock_file):
    # Mock successful file conversion
    mock_process = AsyncMock()
    mock_process.stdout.readline = AsyncMock()
    mock_process.stdout.readline.side_effect = [
        b"out_time_ms=5000000\n",
        b"out_time_ms=10000000\n",
        b""  # End of output
    ]
    mock_process.wait = AsyncMock()
    mock_process.returncode = 0
    mock_subprocess.return_value = mock_process

    # Mock file existence and permissions
    with patch('os.path.exists', return_value=True), \
         patch('os.access', return_value=True):
        
        await async_convert_file(
            "test.dav",
            "latest.txt",
            datetime.now(),
            "test.dav"
        )

@pytest.mark.asyncio
async def test_async_convert_file_input_not_found(mock_subprocess):
    # Mock file not found
    with patch('os.path.exists', return_value=False):
        with pytest.raises(FileNotFoundError):
            await async_convert_file(
                "test.dav",
                "latest.txt",
                datetime.now(),
                "test.dav"
            )

@pytest.mark.asyncio
async def test_async_convert_file_permission_error(mock_subprocess):
    # Mock permission error
    with patch('os.path.exists', return_value=True), \
         patch('os.access', return_value=False):
        
        with pytest.raises(PermissionError):
            await async_convert_file(
                "test.dav",
                "latest.txt",
                datetime.now(),
                "test.dav"
            )

@pytest.mark.asyncio
async def test_async_convert_file_ffmpeg_failure(mock_subprocess, mock_file):
    # Mock ffmpeg conversion failure
    mock_process = AsyncMock()
    mock_process.stdout.readline = AsyncMock(return_value=b"")
    mock_process.stderr.read = AsyncMock(return_value=b"FFmpeg error")
    mock_process.wait = AsyncMock()
    mock_process.returncode = 1
    mock_subprocess.return_value = mock_process

    with patch('os.path.exists', return_value=True), \
         patch('os.access', return_value=True):
        
        with pytest.raises(Exception) as exc_info:
            await async_convert_file(
                "test.dav",
                "latest.txt",
                datetime.now(),
                "test.dav"
            )
        assert "FFmpeg conversion failed" in str(exc_info.value) 