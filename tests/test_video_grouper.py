import pytest
import asyncio
import os
import json
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, AsyncMock, MagicMock
import httpx
import configparser
from video_grouper.video_grouper import (
    ProcessingState, DirectoryPlan, RecordingFile,
    verify_mp4_duration, get_video_duration,
    verify_file_complete, async_convert_file,
    find_and_download_files, process_ffmpeg_queue,
    create_directory, check_device_availability,
    download_with_progress, ffmpeg_queue, queued_files,
    save_queue_state, update_status,
    trim_video
)

# Fixtures
@pytest.fixture
def mock_config():
    config = configparser.ConfigParser()
    config.read('tests/test_config.ini')
    return config

@pytest.fixture
def mock_auth():
    return httpx.DigestAuth("admin", "admin")

@pytest.fixture
def sample_recording_file():
    return RecordingFile(
        file_path="/record/2024.03.14/12.18.28-12.35.18[F][0@0][140480].dav",
        start_time=datetime(2024, 3, 14, 12, 18, 28),
        end_time=datetime(2024, 3, 14, 12, 35, 18),
        size=1024 * 1024  # 1MB
    )

@pytest.fixture
def mock_processing_state():
    state = ProcessingState()
    state.files = {}
    return state

@pytest.fixture(autouse=True)
def setup_teardown():
    # Setup
    ffmpeg_queue = asyncio.Queue()
    queued_files.clear()
    yield
    # Teardown
    ffmpeg_queue = asyncio.Queue()
    queued_files.clear()

# Test ProcessingState
def test_processing_state_initialization():
    state = ProcessingState()
    assert state.files == {}
    assert state.last_updated is not None

def test_processing_state_update_file():
    state = ProcessingState()
    state.update_file_state(
        "test.dav",
        group_dir="/test/videos/2024.03.14-12.18.28",
        status="downloaded"
    )
    assert "test.dav" in state.files
    assert state.files["test.dav"].status == "downloaded"

# Test DirectoryPlan
def test_directory_plan_initialization():
    plan = DirectoryPlan("/test/videos/2024.03.14-12.18.28")
    assert plan.directory_path == "/test/videos/2024.03.14-12.18.28"
    assert plan.expected_files == []
    assert plan.status == "pending"

def test_directory_plan_add_file(sample_recording_file):
    plan = DirectoryPlan("/test/videos/2024.03.14-12.18.28")
    plan.add_file(sample_recording_file)
    assert len(plan.expected_files) == 1
    assert plan.expected_files[0] == sample_recording_file

# Test RecordingFile
def test_recording_file_from_response():
    response_text = """
    <record>
        <filePath>/record/2024.03.14/12.18.28-12.35.18[F][0@0][140480].dav</filePath>
        <startTime>2024-03-14 12:18:28</startTime>
        <endTime>2024-03-14 12:35:18</endTime>
        <size>1048576</size>
    </record>
    """
    files = RecordingFile.from_response(response_text)
    assert len(files) == 1
    assert files[0].file_path == "/record/2024.03.14/12.18.28-12.35.18[F][0@0][140480].dav"
    assert files[0].start_time == datetime(2024, 3, 14, 12, 18, 28)
    assert files[0].end_time == datetime(2024, 3, 14, 12, 35, 18)
    assert files[0].size == 1048576

# Test verify_mp4_duration
@pytest.mark.asyncio
async def test_verify_mp4_duration_success():
    with patch('video_grouper.video_grouper.get_video_duration') as mock_get_duration:
        mock_get_duration.side_effect = [100.0, 100.0]  # DAV and MP4 durations
        with patch('os.path.exists') as mock_exists:
            mock_exists.return_value = True
            result = await verify_mp4_duration("test.dav", "test.mp4")
            assert result is True

@pytest.mark.asyncio
async def test_verify_mp4_duration_missing_files():
    with patch('os.path.exists') as mock_exists:
        mock_exists.return_value = False
        result = await verify_mp4_duration("test.dav", "test.mp4")
        assert result is False

@pytest.mark.asyncio
async def test_verify_mp4_duration_retry():
    with patch('video_grouper.video_grouper.get_video_duration') as mock_get_duration:
        mock_get_duration.side_effect = [100.0, 0.0, 100.0, 100.0]  # Fail first MP4 check
        with patch('os.path.exists') as mock_exists:
            mock_exists.return_value = True
            result = await verify_mp4_duration("test.dav", "test.mp4")
            assert result is True
            assert mock_get_duration.call_count == 4

# Test get_video_duration
@pytest.mark.asyncio
async def test_get_video_duration_success():
    with patch('asyncio.create_subprocess_exec') as mock_subprocess:
        mock_process = AsyncMock()
        mock_process.communicate.return_value = (b"100.0", b"")
        mock_process.returncode = 0
        mock_subprocess.return_value = mock_process
        
        duration = await get_video_duration("test.mp4")
        assert duration == 100.0

@pytest.mark.asyncio
async def test_get_video_duration_error():
    with patch('asyncio.create_subprocess_exec') as mock_subprocess:
        mock_process = AsyncMock()
        mock_process.communicate.return_value = (b"", b"Error")
        mock_process.returncode = 1
        mock_subprocess.return_value = mock_process
        
        duration = await get_video_duration("test.mp4")
        assert duration == 0

# Test verify_file_complete
@pytest.mark.asyncio
async def test_verify_file_complete_success():
    with patch('httpx.AsyncClient') as mock_client:
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.headers = {'content-length': '1048576'}
        mock_client.return_value.__aenter__.return_value.head.return_value = mock_response
        
        with patch('os.path.exists') as mock_exists:
            mock_exists.return_value = True
            with patch('os.path.getsize') as mock_getsize:
                mock_getsize.return_value = 1048576
                
                result = await verify_file_complete("test.dav", "/record/test.dav")
                assert result is True

# Test async_convert_file
@pytest.mark.asyncio
async def test_async_convert_file_success():
    with patch('asyncio.create_subprocess_exec') as mock_subprocess:
        mock_process = AsyncMock()
        mock_process.stdout.readline = AsyncMock(return_value=b"")
        mock_process.wait = AsyncMock()
        mock_process.returncode = 0
        mock_subprocess.return_value = mock_process
        
        with patch('os.path.exists') as mock_exists:
            mock_exists.return_value = True
            with patch('os.access') as mock_access:
                mock_access.return_value = True
                with patch('video_grouper.video_grouper.verify_mp4_duration') as mock_verify:
                    mock_verify.return_value = True
                    
                    await async_convert_file(
                        "test.dav",
                        "latest_video.txt",
                        datetime.now(),
                        "test.dav"
                    )
                    
                    assert mock_subprocess.called

# Test find_and_download_files
@pytest.mark.asyncio
async def test_find_and_download_files_success(mock_auth, mock_processing_state):
    with patch('video_grouper.video_grouper.check_device_availability', AsyncMock(return_value=True)):
        with patch('httpx.AsyncClient') as mock_client:
            # Mock the factory creation
            mock_response = AsyncMock()
            mock_response.status_code = 200
            mock_response.text = "object=123"
            mock_client.return_value.__aenter__.return_value.get.return_value = mock_response
            
            # Mock the file list response
            mock_response.text = """
            <record>
                <filePath>/record/2024.03.14/12.18.28-12.35.18[F][0@0][140480].dav</filePath>
                <startTime>2024-03-14 12:18:28</startTime>
                <endTime>2024-03-14 12:35:18</endTime>
                <size>1048576</size>
            </record>
            """
            
            with patch('os.path.exists') as mock_exists:
                mock_exists.return_value = True
                with patch('os.makedirs') as mock_makedirs:
                    with patch('video_grouper.video_grouper.download_with_progress') as mock_download:
                        mock_download.return_value = None
                        
                        await find_and_download_files(mock_auth, mock_processing_state)
                        
                        assert mock_makedirs.called
                        assert mock_download.called

# Test process_ffmpeg_queue
@pytest.mark.asyncio
async def test_process_ffmpeg_queue():
    with patch('video_grouper.video_grouper.async_convert_file') as mock_convert:
        mock_convert.return_value = None
        
        # Create a test queue
        await ffmpeg_queue.put(("test.dav", "latest_video.txt", datetime.now()))
        
        # Start the queue processor
        task = asyncio.create_task(process_ffmpeg_queue())
        
        # Wait a bit for processing
        await asyncio.sleep(0.1)
        
        # Cancel the task
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        
        assert mock_convert.called

# Test file system operations
def test_directory_creation():
    with patch('os.makedirs') as mock_makedirs:
        with patch('os.path.exists') as mock_exists:
            mock_exists.return_value = False
            
            # Test directory creation
            create_directory("/test/videos/2024.03.14-12.18.28")
            mock_makedirs.assert_called_once_with("/test/videos/2024.03.14-12.18.28", exist_ok=True)

def test_state_saving():
    with patch('json.dump') as mock_dump:
        with patch('builtins.open', MagicMock()) as mock_open:
            state = ProcessingState()
            state.save_state()
            assert mock_dump.called

# Test error handling
@pytest.mark.asyncio
async def test_error_handling_during_download(mock_auth, mock_processing_state):
    with patch('video_grouper.video_grouper.check_device_availability', AsyncMock(return_value=True)):
        with patch('httpx.AsyncClient') as mock_client:
            mock_client.return_value.__aenter__.return_value.get.side_effect = Exception("Network error")
            
            with patch('os.path.exists') as mock_exists:
                mock_exists.return_value = True
                
                result = await find_and_download_files(mock_auth, mock_processing_state)
                assert result is None

@pytest.mark.asyncio
async def test_error_handling_during_conversion():
    with patch('asyncio.create_subprocess_exec') as mock_subprocess:
        mock_process = AsyncMock()
        mock_process.wait.side_effect = Exception("Conversion error")
        mock_subprocess.return_value = mock_process
        
        with patch('os.path.exists') as mock_exists:
            mock_exists.return_value = True
            
            await async_convert_file(
                "test.dav",
                "latest_video.txt",
                datetime.now(),
                "test.dav"
            )
            
            # Verify error was handled
            assert mock_process.wait.called

# Test camera connection scenarios
@pytest.mark.asyncio
async def test_camera_disconnected_behavior(mock_auth, mock_processing_state):
    # Simulate camera disconnected (check_device_availability returns False)
    with patch('video_grouper.video_grouper.check_device_availability', AsyncMock(return_value=False)):
        with patch('httpx.AsyncClient') as mock_client:
            mock_response = AsyncMock()
            mock_response.status_code = 200
            mock_response.text = "object=123"
            mock_client.return_value.__aenter__.return_value.get.return_value = mock_response
            
            with patch('os.path.exists') as mock_exists:
                mock_exists.return_value = True
                with patch('os.makedirs') as mock_makedirs:
                    with patch('video_grouper.video_grouper.download_with_progress') as mock_download:
                        mock_download.return_value = None
                        # Should exit early due to disconnection
                        result = await find_and_download_files(mock_auth, mock_processing_state)
                        assert result is None

@pytest.mark.asyncio
async def test_camera_connected_behavior(mock_auth, mock_processing_state):
    # Simulate camera connected (check_device_availability returns True)
    with patch('video_grouper.video_grouper.check_device_availability', AsyncMock(return_value=True)):
        with patch('httpx.AsyncClient') as mock_client:
            mock_response = AsyncMock()
            mock_response.status_code = 200
            mock_response.text = "object=123"
            mock_client.return_value.__aenter__.return_value.get.return_value = mock_response
            
            with patch('os.path.exists') as mock_exists:
                mock_exists.return_value = True
                with patch('os.makedirs') as mock_makedirs:
                    with patch('video_grouper.video_grouper.download_with_progress') as mock_download:
                        mock_download.return_value = None
                        # Should proceed as normal
                        await find_and_download_files(mock_auth, mock_processing_state)
                        assert mock_download.called

@pytest.mark.asyncio
async def test_incomplete_dav_file_before_disconnect(mock_auth, mock_processing_state):
    # Simulate incomplete DAV file and camera disconnects
    with patch('video_grouper.video_grouper.check_device_availability', AsyncMock(side_effect=[True, False])):
        with patch('httpx.AsyncClient') as mock_client:
            mock_response = AsyncMock()
            mock_response.status_code = 200
            mock_response.text = "object=123"
            mock_client.return_value.__aenter__.return_value.get.return_value = mock_response
            
            with patch('os.path.exists') as mock_exists:
                # DAV file exists but is incomplete
                def exists_side_effect(path):
                    if path.endswith('.dav'):
                        return True
                    return False
                mock_exists.side_effect = exists_side_effect
                with patch('os.path.getsize', return_value=512):  # Incomplete size
                    with patch('video_grouper.video_grouper.download_with_progress', side_effect=Exception("Camera disconnected")) as mock_download:
                        with pytest.raises(Exception):
                            await find_and_download_files(mock_auth, mock_processing_state)
                        assert mock_download.called

@pytest.mark.asyncio
async def test_corrupted_mp4_file():
    # Simulate MP4 file exists but is corrupted (duration check fails)
    with patch('os.path.exists') as mock_exists:
        mock_exists.side_effect = lambda path: path.endswith('.mp4') or path.endswith('.dav')
        with patch('video_grouper.video_grouper.get_video_duration', AsyncMock(side_effect=[100.0, 0.0, 0.0, 0.0])):
            # First DAV duration is fine, MP4 duration is 0 (corrupted)
            result = await verify_mp4_duration('test.dav', 'test.mp4')
            assert result is False

@pytest.mark.asyncio
async def test_mp4_not_fully_processed_from_dav():
    # Simulate MP4 conversion fails or produces incomplete file
    with patch('asyncio.create_subprocess_exec') as mock_subprocess:
        mock_process = AsyncMock()
        mock_process.stdout.readline = AsyncMock(return_value=b"")
        mock_process.wait = AsyncMock()
        mock_process.returncode = 0
        mock_subprocess.return_value = mock_process
        
        with patch('os.path.exists') as mock_exists:
            # DAV exists, MP4 exists but is empty
            def exists_side_effect(path):
                if path.endswith('.dav') or path.endswith('.mp4'):
                    return True
                return False
            mock_exists.side_effect = exists_side_effect
            with patch('os.path.getsize', side_effect=lambda path: 0 if path.endswith('.mp4') else 1024):
                with patch('os.access', return_value=True):
                    with patch('video_grouper.video_grouper.verify_mp4_duration', AsyncMock(return_value=False)):
                        with pytest.raises(ValueError):
                            await async_convert_file(
                                "test.dav",
                                "latest_video.txt",
                                datetime.now(),
                                "test.dav"
                            )

@pytest.mark.asyncio
def test_trim_task_not_readded_if_output_exists(monkeypatch):
    # Setup
    directory = "/test/videos/2025.06.14-10.37.25"
    match_info = {
        'my_team_name': 'WNY Flash',
        'opponent_team_name': 'Vestal',
        'location': 'home',
        'start_time_offset': '20:45'
    }
    combined_file = os.path.join(directory, "combined.mp4")
    output_dir = os.path.join(directory, "2025.06.14 - WNY Flash vs Vestal (home)")
    output_file = os.path.join(output_dir, "wnyflash-vestal-home-06-14-2025-raw.mp4")

    # Mock os.path.exists and os.path.getsize
    monkeypatch.setattr(os.path, "exists", lambda path: path == combined_file or path == output_file)
    monkeypatch.setattr(os.path, "getsize", lambda path: 100 if path == output_file else 1000)
    # Mock get_video_duration to return valid duration for output_file
    async def mock_get_video_duration(path):
        if path == output_file:
            return 120.0
        return 300.0
    monkeypatch.setattr("video_grouper.video_grouper.get_video_duration", mock_get_video_duration)
    # Mock create_directory to do nothing
    monkeypatch.setattr("video_grouper.video_grouper.create_directory", lambda path: None)
    # Clear queued_files
    from video_grouper.video_grouper import queued_files
    queued_files.clear()
    # Run trim_video
    import asyncio
    asyncio.run(trim_video(directory, match_info))
    # Assert that no trim task was added
    assert len(queued_files) == 0 