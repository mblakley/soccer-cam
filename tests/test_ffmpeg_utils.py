import os
import pytest
import asyncio
from datetime import datetime
from unittest.mock import patch, MagicMock, AsyncMock
from video_grouper.ffmpeg_utils import verify_mp4_duration, run_ffmpeg, async_convert_file, get_video_duration

@pytest.fixture
def mock_subprocess():
    with patch('asyncio.create_subprocess_exec') as mock:
        yield mock

@pytest.fixture
def mock_file():
    with patch('builtins.open', MagicMock()) as mock:
        yield mock

@pytest.mark.asyncio
async def test_get_video_duration_success(mock_subprocess):
    """Test successful video duration retrieval."""
    mock_process = AsyncMock()
    mock_process.communicate.return_value = (b"10.5", b"")
    mock_process.returncode = 0
    mock_subprocess.return_value = mock_process

    result = await get_video_duration("test.mp4")
    assert result == 10.5

@pytest.mark.asyncio
async def test_get_video_duration_failure(mock_subprocess):
    """Test video duration retrieval failure."""
    mock_process = AsyncMock()
    mock_process.communicate.return_value = (b"", b"Error")
    mock_process.returncode = 1
    mock_subprocess.return_value = mock_process

    result = await get_video_duration("test.mp4")
    assert result is None

@pytest.mark.asyncio
async def test_verify_mp4_duration_success(mock_subprocess):
    # Mock successful ffprobe response
    mock_process = AsyncMock()
    mock_process.communicate.side_effect = [
        (b"10.0", b""),  # First call for dav_file
        (b"10.0", b"")   # Second call for mp4_file
    ]
    mock_process.returncode = 0
    mock_subprocess.return_value = mock_process

    with patch('os.path.exists', return_value=True):
        result = await verify_mp4_duration("test.dav", "test.mp4")
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
    mock_process.communicate.side_effect = [
        (b"10.0", b""),  # First call for dav_file
        (b"10.4", b"")   # Second call for mp4_file
    ]
    mock_process.returncode = 0
    mock_subprocess.return_value = mock_process

    with patch('os.path.exists', return_value=True):
        result = await verify_mp4_duration("test.dav", "test.mp4", tolerance=0.1)
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

    # Mock file operations
    with patch('os.path.exists', return_value=True), \
         patch('os.access', return_value=True), \
         patch('os.remove') as mock_remove, \
         patch('os.replace') as mock_replace, \
         patch('asyncio.sleep', new_callable=AsyncMock), \
         patch('video_grouper.ffmpeg_utils.verify_mp4_duration', return_value=True), \
         patch('video_grouper.ffmpeg_utils.get_video_duration', return_value=10.0), \
         patch('video_grouper.ffmpeg_utils.get_default_date_format', return_value="%Y-%m-%d %H:%M:%S"):
        
        # Run the conversion
        await async_convert_file(
            "test.dav",
            "latest.txt",
            datetime.now(),
            "test.dav"
        )
        
        # Verify that os.remove was called for the DAV file
        mock_remove.assert_any_call("test.dav")

@pytest.mark.asyncio
async def test_async_convert_file_with_removal_retry(mock_subprocess, mock_file):
    """Test that file removal is retried when it fails initially."""
    mock_process = AsyncMock()
    mock_process.stdout.readline = AsyncMock(return_value=b"")
    mock_process.wait = AsyncMock()
    mock_process.returncode = 0
    mock_subprocess.return_value = mock_process

    # Track file paths for remove calls
    remove_calls = []
    
    # Mock file operations
    with patch('os.path.exists') as mock_exists, \
         patch('os.access', return_value=True), \
         patch('os.remove') as mock_remove, \
         patch('os.replace') as mock_replace, \
         patch('asyncio.sleep') as mock_sleep, \
         patch('video_grouper.ffmpeg_utils.verify_mp4_duration', return_value=True), \
         patch('video_grouper.ffmpeg_utils.get_video_duration', return_value=10.0), \
         patch('video_grouper.ffmpeg_utils.get_default_date_format', return_value="%Y-%m-%d %H:%M:%S"):
        
        # Make os.remove fail on first attempt for DAV file, succeed on second
        def side_effect_remove(path):
            remove_calls.append(path)
            if path == "test.dav" and len([p for p in remove_calls if p == "test.dav"]) == 1:
                raise PermissionError("File in use")
            return None
            
        mock_remove.side_effect = side_effect_remove
        
        # Make os.path.exists return True initially for DAV, then False after successful removal
        def side_effect_exists(path):
            if path == "test.dav":
                # Return True for first call, False after second call to remove
                return len([p for p in remove_calls if p == "test.dav"]) < 2
            return True
            
        mock_exists.side_effect = side_effect_exists
        
        # Run the conversion
        await async_convert_file(
            "test.dav",
            "latest.txt",
            datetime.now(),
            "test.dav"
        )
        
        # Verify os.remove was called at least twice for the DAV file
        dav_remove_calls = [call for call in remove_calls if call == "test.dav"]
        assert len(dav_remove_calls) >= 2, "File removal should be retried"
        
        # Verify sleep was called
        assert mock_sleep.called, "Sleep should be called between retries"

@pytest.mark.asyncio
async def test_async_convert_file_input_not_found():
    # Mock file not found
    with patch('video_grouper.ffmpeg_utils.ffmpeg_lock', AsyncMock()), \
         patch('os.path.exists', side_effect=lambda path: False):
        
        with pytest.raises(FileNotFoundError) as exc_info:
            await async_convert_file(
                "test.dav",
                "latest.txt",
                datetime.now(),
                "test.dav"
            )
        assert "Input file not found" in str(exc_info.value)

@pytest.mark.asyncio
async def test_async_convert_file_permission_error():
    # Mock permission error
    with patch('video_grouper.ffmpeg_utils.ffmpeg_lock', AsyncMock()), \
         patch('os.path.exists', side_effect=lambda path: True), \
         patch('os.access', side_effect=lambda path, mode: False):
        
        with pytest.raises(PermissionError) as exc_info:
            await async_convert_file(
                "test.dav",
                "latest.txt",
                datetime.now(),
                "test.dav"
            )
        assert "Cannot read input file" in str(exc_info.value)

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
         patch('os.access', return_value=True), \
         patch('os.replace') as mock_replace, \
         patch('video_grouper.ffmpeg_utils.get_default_date_format', return_value="%Y-%m-%d %H:%M:%S"):
        
        with pytest.raises(Exception) as exc_info:
            await async_convert_file(
                "test.dav",
                "latest.txt",
                datetime.now(),
                "test.dav"
            )
        assert "FFmpeg conversion failed" in str(exc_info.value)

@pytest.mark.asyncio
async def test_async_convert_file_cleanup_mp4_on_error(mock_subprocess, mock_file):
    """Test that MP4 file is cleaned up if conversion fails."""
    mock_process = AsyncMock()
    mock_process.stdout.readline = AsyncMock(return_value=b"")
    mock_process.stderr.read = AsyncMock(return_value=b"FFmpeg error")
    mock_process.wait = AsyncMock()
    mock_process.returncode = 1
    mock_subprocess.return_value = mock_process

    with patch('os.path.exists', side_effect=lambda path: True), \
         patch('os.access', return_value=True), \
         patch('os.remove') as mock_remove, \
         patch('os.replace') as mock_replace, \
         patch('video_grouper.ffmpeg_utils.get_default_date_format', return_value="%Y-%m-%d %H:%M:%S"):
        
        with pytest.raises(Exception):
            await async_convert_file(
                "test.dav",
                "latest.txt",
                datetime.now(),
                "test.dav"
            )
        
        # Verify that os.remove was called for the MP4 file
        mock_remove.assert_any_call("test.mp4") 