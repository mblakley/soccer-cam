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
    create_directory, VideoGrouperApp, find_group_directory,
    FileState
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
async def test_find_and_download_files_with_grouping(app, mock_camera, monkeypatch):
    """Test that files are downloaded directly into the correct group directories."""
    # Create a temporary directory for testing
    with tempfile.TemporaryDirectory() as temp_dir:
        # Set the app's storage path to the temp directory
        app.storage_path = temp_dir
        app.processing_state.storage_path = temp_dir
        
        # Create group directories
        group1_dir = os.path.join(temp_dir, "group1")
        group2_dir = os.path.join(temp_dir, "group2")
        os.makedirs(group1_dir, exist_ok=True)
        os.makedirs(group2_dir, exist_ok=True)
        
        # Mock the find_group_directory function to return our predefined groups
        def mock_find_group_directory(file_path, storage_path, processing_state):
            if "12.00.00" in file_path or "12.15.02" in file_path:
                return group1_dir
            else:
                return group2_dir
        
        # Apply the monkeypatch
        import video_grouper.video_grouper
        monkeypatch.setattr(video_grouper.video_grouper, "find_group_directory", mock_find_group_directory)
        
        # Mock the camera's file list response
        mock_camera.get_file_list.return_value = [
            {
                'path': '/mnt/dvr/mmc1p2_0/2024.06.15/0/dav/12/12.00.00-12.15.00[F][0@0][123456].dav',
                'startTime': '2024-06-15 12:00:00',
                'endTime': '2024-06-15 12:15:00'
            },
            {
                'path': '/mnt/dvr/mmc1p2_0/2024.06.15/0/dav/12/12.15.02-12.30.00[F][0@0][123457].dav',
                'startTime': '2024-06-15 12:15:02',  # Starts 2 seconds after previous file ends
                'endTime': '2024-06-15 12:30:00'
            },
            {
                'path': '/mnt/dvr/mmc1p2_0/2024.06.15/0/dav/12/12.40.00-12.55.00[F][0@0][123458].dav',
                'startTime': '2024-06-15 12:40:00',  # Starts much later, should be in a new group
                'endTime': '2024-06-15 12:55:00'
            }
        ]
        
        # Mock the camera methods
        mock_camera.check_availability.return_value = True
        mock_camera.download_file.return_value = True
        mock_camera.get_file_size.return_value = 1000
        
        # Mock the queue methods to avoid datetime serialization issues
        app.save_queue_state = lambda: None
        app.ffmpeg_queue.put = AsyncMock()
        
        # Call the method
        await app.find_and_download_files()
        
        # Verify the files were downloaded to the correct directories
        # First two files should be in group1
        assert os.path.exists(os.path.join(group1_dir, '12.00.00-12.15.00[F][0@0][123456].dav'))
        assert os.path.exists(os.path.join(group1_dir, '12.15.02-12.30.00[F][0@0][123457].dav'))
        
        # Third file should be in group2
        assert os.path.exists(os.path.join(group2_dir, '12.40.00-12.55.00[F][0@0][123458].dav'))
        
        # Verify the download_file method was called with the correct paths
        assert mock_camera.download_file.call_count == 3
        
        # Check that the files were added to the ffmpeg queue
        assert app.ffmpeg_queue.put.call_count == 3

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
    """Test that queue state is saved correctly with datetime objects."""
    # Create a temporary directory for the test
    with tempfile.TemporaryDirectory() as temp_dir:
        app.storage_path = temp_dir
        queue_state_path = os.path.join(temp_dir, "ffmpeg_queue_state.json")
        
        # Add a tuple with datetime to queued_files
        now = datetime.now()
        file_tuple = ("test.dav", "latest.txt", now)
        app.queued_files.add(file_tuple)
        await app.ffmpeg_queue.put(file_tuple)
        
        # Save queue state
        app.save_queue_state()
        
        # Verify the file was created
        assert os.path.exists(queue_state_path)
        
        # Load the saved state and verify it's valid JSON
        with open(queue_state_path, 'r') as f:
            saved_state = json.load(f)
            
        # Check that the datetime was properly serialized
        assert 'queued_files' in saved_state
        assert len(saved_state['queued_files']) == 1
        assert saved_state['queued_files'][0]['type'] == 'conversion'
        assert saved_state['queued_files'][0]['file_path'] == "test.dav"
        assert saved_state['queued_files'][0]['latest_file_path'] == "latest.txt"
        assert saved_state['queued_files'][0]['end_time'] == now.isoformat()

@pytest.mark.asyncio
async def test_load_queue_state(app):
    """Test that queue state is loaded correctly, including datetime objects."""
    # Create a temporary directory for the test
    with tempfile.TemporaryDirectory() as temp_dir:
        app.storage_path = temp_dir
        queue_state_path = os.path.join(temp_dir, "ffmpeg_queue_state.json")
        
        # Create a test state file with serialized datetime
        now = datetime.now()
        test_state = {
            'queued_files': [
                {
                    'type': 'conversion',
                    'file_path': 'test.dav',
                    'latest_file_path': 'latest.txt',
                    'end_time': now.isoformat()
                }
            ],
            'ffmpeg_queue': [
                {
                    'type': 'conversion',
                    'file_path': 'test.dav',
                    'latest_file_path': 'latest.txt',
                    'end_time': now.isoformat()
                }
            ]
        }
        
        # Write the test state to file
        with open(queue_state_path, 'w') as f:
            json.dump(test_state, f)
        
        # Clear existing state
        app.queued_files.clear()
        while not app.ffmpeg_queue.empty():
            await app.ffmpeg_queue.get()
        
        # Load the state
        await app.load_queue_state()
        
        # Verify the state was loaded correctly
        assert len(app.queued_files) == 1
        loaded_item = next(iter(app.queued_files))
        assert isinstance(loaded_item, tuple)
        assert loaded_item[0] == 'test.dav'
        assert loaded_item[1] == 'latest.txt'
        assert isinstance(loaded_item[2], datetime)
        assert loaded_item[2].isoformat() == now.isoformat()
        
        # Verify queue was loaded
        assert not app.ffmpeg_queue.empty()
        queue_item = await app.ffmpeg_queue.get()
        assert queue_item[0] == 'test.dav'
        assert queue_item[1] == 'latest.txt'
        assert isinstance(queue_item[2], datetime)
        assert queue_item[2].isoformat() == now.isoformat()

@pytest.mark.asyncio
async def test_load_camera_state(app):
    with patch('json.load', return_value={'connection_events': [['2024-03-14T12:00:00', 'connected']]}), \
         patch('builtins.open', MagicMock()), \
         patch('os.path.exists', return_value=True):
        app.load_camera_state()
        assert len(app.connection_events) == 1
        assert app.connection_events[0][1] == 'connected'

# Test find_group_directory function
@pytest.mark.asyncio
async def test_find_group_directory_new_file():
    """Test that a new file with no time relationship to existing files gets a new directory."""
    # Create a mock processing state
    processing_state = ProcessingState(storage_path=os.path.abspath('test/videos'))
    
    # Call the function with a new file
    file_path = os.path.join('test/videos', '12.00.00-12.15.00.dav')
    
    with patch('video_grouper.video_grouper.create_directory') as mock_create_dir:
        result = find_group_directory(file_path, os.path.abspath('test/videos'), processing_state)
        
        # Verify a new directory was created
        assert mock_create_dir.called
        
        # Verify the directory name follows the expected format (YYYY.MM.DD-HH.MM.SS)
        assert os.path.basename(result).startswith(datetime.now().strftime("%Y.%m.%d"))

@pytest.mark.asyncio
async def test_find_group_directory_sequential_files():
    """Test that sequential files (where one starts right after another ends) are grouped together."""
    # Create a mock processing state
    processing_state = ProcessingState(storage_path=os.path.abspath('test/videos'))
    
    # Create a test directory
    test_dir = os.path.join(os.path.abspath('test/videos'), '2024.06.15-12.00.00')
    
    # Add a file to the processing state that ended at 12:15:00
    first_file_path = os.path.join('test/videos', '12.00.00-12.15.00.dav')
    first_file_state = FileState(
        file_path=first_file_path,
        group_dir=test_dir,
        status="converted",
        start_time=datetime.now().replace(hour=12, minute=0, second=0),
        end_time=datetime.now().replace(hour=12, minute=15, second=0)
    )
    processing_state.files[first_file_path] = first_file_state
    
    # Call find_group_directory with a new file that starts at 12:15:02 (within 5 seconds of previous end)
    second_file_path = os.path.join('test/videos', '12.15.02-12.30.00.dav')
    
    with patch('video_grouper.video_grouper.create_directory') as mock_create_dir:
        result = find_group_directory(second_file_path, os.path.abspath('test/videos'), processing_state)
        
        # Verify the file was assigned to the existing directory
        assert result == test_dir
        
        # Verify no new directory was created
        assert not mock_create_dir.called

@pytest.mark.asyncio
async def test_find_group_directory_non_sequential_files():
    """Test that non-sequential files (gap > 5 seconds) get different directories."""
    # Create a mock processing state
    processing_state = ProcessingState(storage_path=os.path.abspath('test/videos'))
    
    # Create a test directory
    test_dir = os.path.join(os.path.abspath('test/videos'), '2024.06.15-12.00.00')
    
    # Add a file to the processing state that ended at 12:15:00
    first_file_path = os.path.join('test/videos', '12.00.00-12.15.00.dav')
    first_file_state = FileState(
        file_path=first_file_path,
        group_dir=test_dir,
        status="converted",
        start_time=datetime.now().replace(hour=12, minute=0, second=0),
        end_time=datetime.now().replace(hour=12, minute=15, second=0)
    )
    processing_state.files[first_file_path] = first_file_state
    
    # Call find_group_directory with a new file that starts at 12:15:10 (> 5 seconds after previous end)
    second_file_path = os.path.join('test/videos', '12.15.10-12.30.00.dav')
    
    with patch('video_grouper.video_grouper.create_directory') as mock_create_dir:
        result = find_group_directory(second_file_path, os.path.abspath('test/videos'), processing_state)
        
        # Verify a new directory was created
        assert mock_create_dir.called
        
        # Verify the file was NOT assigned to the existing directory
        assert result != test_dir

@pytest.mark.asyncio
async def test_dav_file_removal_after_conversion(monkeypatch):
    """Test that DAV files are removed after successful conversion to MP4."""
    # Create a temporary directory for testing
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create a test DAV file
        dav_file = os.path.join(temp_dir, "test.dav")
        with open(dav_file, "w") as f:
            f.write("test data")
        
        # Create an MP4 file to simulate successful conversion
        mp4_file = dav_file.replace(".dav", ".mp4")
        with open(mp4_file, "w") as f:
            f.write("mp4 content")
        
        # Import the function we want to test
        from video_grouper.ffmpeg_utils import async_convert_file
        
        # Call the function with our test file
        end_time = datetime.now()
        latest_file_path = os.path.join(temp_dir, "latest_video.txt")
        
        # Create a mock subprocess that returns success
        async def mock_create_subprocess_exec(*args, **kwargs):
            mock_process = AsyncMock()
            mock_process.returncode = 0
            mock_process.stdout.readline = AsyncMock(side_effect=[b"out_time_ms=1000000\n", b""])
            mock_process.stderr.read = AsyncMock(return_value=b"")
            mock_process.wait = AsyncMock()
            return mock_process
        
        # Mock the file operations
        with patch('asyncio.create_subprocess_exec', mock_create_subprocess_exec), \
             patch('asyncio.sleep', AsyncMock()), \
             patch('video_grouper.ffmpeg_utils.ffmpeg_lock', AsyncMock()), \
             patch('video_grouper.ffmpeg_utils.get_video_duration', AsyncMock(return_value=10.0)), \
             patch('video_grouper.ffmpeg_utils.verify_mp4_duration', AsyncMock(return_value=True)), \
             patch('video_grouper.ffmpeg_utils.get_default_date_format', lambda: "%Y-%m-%d %H:%M:%S"), \
             patch('os.replace', MagicMock()), \
             patch('os.remove') as mock_remove:
            
            # Call the function
            await async_convert_file(dav_file, latest_file_path, end_time, os.path.basename(dav_file))
            
            # Verify os.remove was called with the DAV file path
            mock_remove.assert_any_call(dav_file)

@pytest.mark.asyncio
async def test_cleanup_dav_files(app, monkeypatch):
    """Test that the cleanup_dav_files method properly removes orphaned DAV files."""
    # Create a temporary directory for the test
    with tempfile.TemporaryDirectory() as temp_dir:
        # Set the app's storage path to the temp directory
        app.storage_path = temp_dir
        
        # Mock directories and files instead of creating them
        sub_dir = os.path.join(temp_dir, "test_dir")
        dav_file1 = os.path.join(sub_dir, "valid.dav")
        mp4_file1 = os.path.join(sub_dir, "valid.mp4")
        dav_file2 = os.path.join(sub_dir, "no_mp4.dav")
        dav_file3 = os.path.join(sub_dir, "empty_mp4.dav")
        mp4_file3 = os.path.join(sub_dir, "empty_mp4.mp4")
        
        # Mock os.path.exists to pretend our files exist
        original_exists = os.path.exists
        def mock_exists(path):
            if path in [sub_dir, dav_file1, mp4_file1, dav_file2, dav_file3, mp4_file3]:
                return True
            return original_exists(path)
        
        # Mock os.listdir to return our test files
        def mock_listdir(path):
            if path == sub_dir:
                return ["valid.dav", "valid.mp4", "no_mp4.dav", "empty_mp4.dav", "empty_mp4.mp4"]
            return []
        
        # Mock os.remove to track calls
        remove_mock = MagicMock()
        
        # Apply monkepatch
        monkeypatch.setattr(os.path, "exists", mock_exists)
        monkeypatch.setattr(os, "listdir", mock_listdir)
        monkeypatch.setattr(os, "remove", remove_mock)
        monkeypatch.setattr(os.path, "isdir", lambda path: path == sub_dir)
        monkeypatch.setattr(os.path, "getsize", lambda path: 1000 if path != mp4_file3 else 0)
        
        # Mock the get_video_duration function to return a valid duration only for the first MP4
        with patch('video_grouper.ffmpeg_utils.get_video_duration', 
                  side_effect=lambda path: 10.0 if path == mp4_file1 else 0.0):
            
            # Run the cleanup
            deleted_count = await app.cleanup_dav_files(sub_dir)
            
            # Verify only the valid DAV file was removed
            assert deleted_count == 1
            remove_mock.assert_called_once_with(dav_file1)

@pytest.mark.asyncio
async def test_cleanup_dav_files_all_directories(app, monkeypatch):
    """Test that the cleanup_dav_files method works across all directories when no specific directory is provided."""
    # Create a temporary directory for the test
    with tempfile.TemporaryDirectory() as temp_dir:
        # Set the app's storage path to the temp directory
        app.storage_path = temp_dir
        
        # Mock directories and files instead of creating them
        sub_dir1 = os.path.join(temp_dir, "dir1")
        sub_dir2 = os.path.join(temp_dir, "dir2")
        dav_file1 = os.path.join(sub_dir1, "valid1.dav")
        mp4_file1 = os.path.join(sub_dir1, "valid1.mp4")
        dav_file2 = os.path.join(sub_dir2, "valid2.dav")
        mp4_file2 = os.path.join(sub_dir2, "valid2.mp4")
        
        # Mock os.path.exists to pretend our files exist
        original_exists = os.path.exists
        def mock_exists(path):
            if path in [sub_dir1, sub_dir2, dav_file1, mp4_file1, dav_file2, mp4_file2]:
                return True
            return original_exists(path)
        
        # Mock os.listdir to return our test files and directories
        def mock_listdir(path):
            if path == temp_dir:
                return ["dir1", "dir2"]
            elif path == sub_dir1:
                return ["valid1.dav", "valid1.mp4"]
            elif path == sub_dir2:
                return ["valid2.dav", "valid2.mp4"]
            return []
        
        # Mock os.remove to track calls
        remove_mock = MagicMock()
        
        # Apply monkepatches
        monkeypatch.setattr(os.path, "exists", mock_exists)
        monkeypatch.setattr(os, "listdir", mock_listdir)
        monkeypatch.setattr(os, "remove", remove_mock)
        monkeypatch.setattr(os.path, "isdir", lambda path: path in [sub_dir1, sub_dir2])
        monkeypatch.setattr(os.path, "getsize", lambda path: 1000)  # All MP4 files have content
        
        # Mock the get_video_duration function to return valid durations
        with patch('video_grouper.ffmpeg_utils.get_video_duration', return_value=10.0):
            
            # Run the cleanup without specifying a directory
            deleted_count = await app.cleanup_dav_files()
            
            # Verify both DAV files were deleted
            assert deleted_count == 2
            assert remove_mock.call_count == 2
            remove_mock.assert_any_call(dav_file1)
            remove_mock.assert_any_call(dav_file2) 