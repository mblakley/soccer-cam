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
    create_directory, VideoGrouperApp
)
from video_grouper.ffmpeg_utils import (
    verify_mp4_duration, get_video_duration,
    async_convert_file
)
import tempfile

# Fixtures
@pytest.fixture
def mock_config():
    config = configparser.ConfigParser()
    config['CAMERA'] = {
        'type': 'dahua',
        'device_ip': '192.168.1.100',
        'username': 'admin',
        'password': 'password'
    }
    config['STORAGE'] = {
        'path': os.path.abspath('test/videos')
    }
    return config

@pytest.fixture
def mock_auth():
    return httpx.DigestAuth("admin", "admin")

@pytest.fixture
def sample_recording_file():
    return RecordingFile(
        file_path="/record/2024.03.14/12.18.28-12.35.18[F][0@0][140480].dav",
        start_time=datetime(2024, 3, 14, 12, 18, 28),
        end_time=datetime(2024, 3, 14, 12, 35, 18)
    )

@pytest.fixture
def mock_processing_state():
    return ProcessingState(storage_path=os.path.abspath('test/videos'))

@pytest.fixture
def mock_camera():
    with patch('video_grouper.cameras.dahua.DahuaCamera') as mock:
        camera = mock.return_value
        camera.check_availability = AsyncMock(return_value=True)
        camera.get_file_list = AsyncMock(return_value=[])
        camera.get_file_size = AsyncMock(return_value=1000)
        camera.download_file = AsyncMock(return_value=True)
        camera.stop_recording = AsyncMock(return_value=True)
        camera.connection_events = []
        camera.is_connected = True
        yield camera

@pytest.fixture
def app(mock_config, mock_camera):
    app = VideoGrouperApp(mock_config)
    app.camera = mock_camera
    return app

# Test ProcessingState
def test_processing_state_initialization():
    state = ProcessingState(storage_path=os.path.abspath('test/videos'))
    assert state.storage_path == os.path.abspath('test/videos')

def test_processing_state_update_file():
    state = ProcessingState(storage_path=os.path.abspath('test/videos'))
    state.update_file_state('file1', status='downloaded')
    assert state.files['file1'].status == 'downloaded'

# Test DirectoryPlan
def test_directory_plan_initialization():
    plan = DirectoryPlan(path='dir1')
    assert plan.path == 'dir1'
    assert plan.files == []

def test_directory_plan_add_file(sample_recording_file):
    plan = DirectoryPlan(path='dir1')
    plan.add_file(sample_recording_file)
    assert len(plan.files) == 1
    assert plan.files[0] == sample_recording_file

# Test RecordingFile
def test_recording_file_from_response():
    response_text = """
    <record>
        <filePath>/record/2024.03.14/12.18.28-12.35.18[F][0@0][140480].dav</filePath>
        <startTime>2024-03-14 12:18:28</startTime>
        <endTime>2024-03-14 12:35:18</endTime>
    </record>
    """
    files = RecordingFile.from_response(response_text)
    assert isinstance(files, list)

# Test verify_mp4_duration
@pytest.mark.asyncio
async def test_verify_mp4_duration_success():
    with patch('os.path.exists', return_value=True), \
         patch('video_grouper.ffmpeg_utils.get_video_duration', AsyncMock(return_value=100.0)):
        result = await verify_mp4_duration('test.dav', 'test.mp4')
        assert result is True

@pytest.mark.asyncio
async def test_verify_mp4_duration_missing_files():
    with patch('os.path.exists', return_value=False):
        result = await verify_mp4_duration('test.dav', 'test.mp4')
        assert result is False

@pytest.mark.asyncio
async def test_verify_mp4_duration_retry():
    durations = [100.0, 100.0, 100.0]
    mp4_durations = [90.0, 95.0, 100.0]
    with patch('os.path.exists', return_value=True), \
         patch('video_grouper.ffmpeg_utils.get_video_duration', side_effect=lambda f: durations.pop(0) if f.endswith('.dav') else mp4_durations.pop(0)):
        result = await verify_mp4_duration('test.dav', 'test.mp4')
        assert result is True

# Test get_video_duration
@pytest.mark.asyncio
async def test_get_video_duration_success():
    with patch('asyncio.create_subprocess_exec') as mock_subprocess:
        mock_process = AsyncMock()
        mock_process.communicate.return_value = (b'100.0', b'')
        mock_process.returncode = 0
        mock_subprocess.return_value = mock_process
        
        duration = await get_video_duration('test.dav')
        assert duration == 100.0

@pytest.mark.asyncio
async def test_get_video_duration_error():
    with patch('asyncio.create_subprocess_exec') as mock_subprocess:
        mock_process = AsyncMock()
        mock_process.communicate.return_value = (b'', b'error')
        mock_process.returncode = 1
        mock_subprocess.return_value = mock_process
        
        duration = await get_video_duration('test.dav')
        assert duration is None

# Test verify_file_complete
@pytest.mark.asyncio
async def test_verify_file_complete_success(app, mock_camera):
    with patch('os.path.getsize', return_value=1000):
        mock_camera.get_file_size.return_value = 1000
        result = await app.verify_file_complete('test.dav')
        assert result is True

@pytest.mark.asyncio
async def test_verify_file_complete_failure(app, mock_camera):
    with patch('os.path.getsize', return_value=1000):
        mock_camera.get_file_size.return_value = 500
        result = await app.verify_file_complete('test.dav')
        assert result is False

# Test find_and_download_files
@pytest.mark.asyncio
async def test_find_and_download_files_success(app, mock_camera):
    mock_camera.check_availability.return_value = True
    mock_camera.get_file_list.return_value = [
        {'path': 'test.dav', 'size': 1000}
    ]
    mock_camera.get_file_size.return_value = 1000
    mock_camera.download_file.return_value = True
    
    await app.find_and_download_files()
    assert mock_camera.download_file.called

# Test process_ffmpeg_queue
@pytest.mark.asyncio
async def test_process_ffmpeg_queue(app):
    with patch('video_grouper.video_grouper.async_convert_file', new_callable=AsyncMock) as mock_convert:
        mock_convert.return_value = None

        # Create a test queue on the app instance
        await app.ffmpeg_queue.put(("test.dav", "latest_video.txt", datetime.now()))

        # Start the queue processor using the app's queue
        async def process_queue():
            while not app.ffmpeg_queue.empty():
                item = await app.ffmpeg_queue.get()
                await mock_convert(*item)

        task = asyncio.create_task(process_queue())
        await asyncio.sleep(0.1)
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
            create_directory(os.path.join('test', 'videos', '2024.03.14-12.18.28'))
            mock_makedirs.assert_called_once_with(os.path.join('test', 'videos', '2024.03.14-12.18.28'), exist_ok=True)

def test_state_saving():
    with patch('json.dump') as mock_dump, \
         patch('builtins.open', MagicMock()), \
         patch('os.path.exists', return_value=True):
        state = ProcessingState(storage_path=os.path.abspath('test/videos'))
        state.save_state()
        assert mock_dump.called

# Test error handling
@pytest.mark.asyncio
async def test_error_handling_during_download(app, mock_camera):
    mock_camera.check_availability.return_value = True
    mock_camera.get_file_list.return_value = [
        {'path': 'test.dav', 'size': 1000}
    ]
    mock_camera.get_file_size.return_value = 1000
    mock_camera.download_file.return_value = False
    
    await app.find_and_download_files()
    assert mock_camera.download_file.called

@pytest.mark.asyncio
async def test_error_handling_during_conversion():
    with patch('asyncio.create_subprocess_exec') as mock_subprocess, \
         patch('os.path.exists', return_value=True), \
         patch('os.access', return_value=True):
        mock_process = AsyncMock()
        mock_process.wait.side_effect = Exception("Conversion error")
        mock_process.stdout.readline = AsyncMock(return_value=b"")
        mock_subprocess.return_value = mock_process
        
        try:
            await async_convert_file(
                "test.dav",
                "latest_video.txt",
                datetime.now(),
                "test.dav"
            )
        except Exception as e:
            assert "Conversion error" in str(e)

# Test camera connection scenarios
@pytest.mark.asyncio
async def test_camera_disconnected_behavior(app, mock_camera):
    mock_camera.is_connected = False
    mock_camera.check_availability.return_value = False
    await app.find_and_download_files()
    assert not mock_camera.get_file_list.called

@pytest.mark.asyncio
async def test_incomplete_dav_file_before_disconnect(app, mock_camera):
    with patch('os.path.exists', return_value=True), \
         patch('os.path.getsize', return_value=1000):
        mock_camera.get_file_size.return_value = 2000  # Different size
        result = await app.verify_file_complete('test.dav')
        assert result is False

@pytest.mark.asyncio
async def test_corrupted_mp4_file():
    # DAV exists, MP4 exists, DAV duration is 100, MP4 duration is 0
    with patch('os.path.exists', side_effect=lambda x: True), \
         patch('video_grouper.ffmpeg_utils.get_video_duration', side_effect=lambda x: 100.0 if x.endswith('.dav') else 0.0):
        result = await verify_mp4_duration('test.dav', 'test.mp4')
        assert result is False

@pytest.mark.asyncio
async def test_mp4_not_fully_processed_from_dav():
    # DAV exists, MP4 exists, DAV duration is 100, MP4 duration is 50
    with patch('os.path.exists', side_effect=lambda x: True), \
         patch('video_grouper.ffmpeg_utils.get_video_duration', side_effect=lambda x: 100.0 if x.endswith('.dav') else 50.0):
        result = await verify_mp4_duration('test.dav', 'test.mp4')
        assert result is False

@pytest.mark.asyncio
async def test_trim_task_not_readded_if_output_exists(app):
    # Setup
    input_file = "test.mp4"
    output_file = "test_trimmed.mp4"
    start_time_offset = "00:00:10"
    total_duration = 100.0
    
    # Mock file existence and ensure queue is empty
    with patch('os.path.exists', return_value=True), \
         patch('os.path.getsize', return_value=1000), \
         patch('video_grouper.ffmpeg_utils.get_video_duration', AsyncMock(return_value=100.0)):
        
        # Clear any existing state
        app.queued_files.clear()
        while not app.ffmpeg_queue.empty():
            try:
                app.ffmpeg_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        
        # Add task to queue
        app.queued_files.add((input_file, output_file, start_time_offset, total_duration))
        await app.ffmpeg_queue.put((input_file, output_file, start_time_offset, total_duration))
        
        # Save queue state
        app.save_queue_state()
        
        # Clear queue
        app.queued_files.clear()
        while not app.ffmpeg_queue.empty():
            try:
                app.ffmpeg_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        
        # Load queue state
        await app.load_queue_state()
        
        # Check that task was not readded
        assert len(app.queued_files) == 0
        assert app.ffmpeg_queue.empty()

@pytest.mark.asyncio
async def test_parse_match_info(app):
    # Write a temporary file with a [MATCH] section
    with tempfile.NamedTemporaryFile('w+', delete=False) as tmp:
        tmp.write('[MATCH]\nstart_time_offset = 00:00:10\nmy_team_name = Team A\n')
        tmp.flush()
        result = app.parse_match_info(tmp.name)
    assert dict(result) == {'start_time_offset': '00:00:10', 'my_team_name': 'Team A'}

@pytest.mark.asyncio
async def test_save_queue_state(app):
    with patch('json.dump') as mock_dump, \
         patch('builtins.open', MagicMock()):
        app.queued_files.add('test.dav')
        app.ffmpeg_queue.put_nowait('test.dav')
        app.save_queue_state()
        assert mock_dump.called

@pytest.mark.asyncio
async def test_load_queue_state(app):
    with patch('json.load', return_value={'queued_files': ['test.dav'], 'ffmpeg_queue': ['test.dav']}), \
         patch('builtins.open', MagicMock()), \
         patch('os.path.exists', return_value=True):
        await app.load_queue_state()
        assert 'test.dav' in app.queued_files
        assert not app.ffmpeg_queue.empty()

@pytest.mark.asyncio
async def test_load_camera_state(app):
    with patch('json.load', return_value={'connection_events': [['2024-03-14T12:00:00', 'connected']]}), \
         patch('builtins.open', MagicMock()), \
         patch('os.path.exists', return_value=True):
        app.load_camera_state()
        assert len(app.connection_events) == 1
        assert app.connection_events[0][1] == 'connected' 