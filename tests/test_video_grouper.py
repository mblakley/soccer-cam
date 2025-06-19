import pytest
import asyncio
import os
import json
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, AsyncMock, MagicMock
import configparser

from video_grouper.video_grouper import (
    DirectoryState, 
    create_directory, VideoGrouperApp, find_group_directory
)
from video_grouper.models import RecordingFile

# Fixtures
@pytest.fixture
def mock_config(tmp_path):
    """Provides a mock configparser object and a temporary storage path."""
    config = configparser.ConfigParser()
    config['STORAGE'] = {'path': str(tmp_path)}
    config['CAMERA'] = {
        'type': 'dahua',
        'device_ip': '127.0.0.1',
        'username': 'admin',
        'password': 'password'
    }
    config['APP'] = {'check_interval_seconds': '1'}
    return config

@pytest.fixture
def sample_recording_file():
    return RecordingFile(
        start_time=datetime(2024, 3, 14, 12, 18, 28),
        end_time=datetime(2024, 3, 14, 12, 35, 18),
        file_path="/record/2024.03.14/12.18.28-12.35.18[F][0@0][140480].dav"
    )

@pytest.fixture
def mock_camera():
    """Mocks the DahuaCamera object."""
    camera = MagicMock()
    camera.check_availability = AsyncMock(return_value=True)
    camera.get_file_list = AsyncMock(return_value=[])
    camera.download_file = AsyncMock(return_value=True)
    return camera

@pytest.fixture
def mock_camera_class(mock_camera):
    """Mocks the DahuaCamera class."""
    with patch('video_grouper.video_grouper.DahuaCamera', return_value=mock_camera) as mock_class:
        yield mock_class

@pytest.fixture
def video_grouper_app(mock_config, mock_camera):
    """Initializes VideoGrouperApp with mocked dependencies."""
    app = VideoGrouperApp(mock_config, camera=mock_camera)
    yield app

# Test DirectoryState
def test_directory_state_initialization(tmp_path):
    state = DirectoryState(str(tmp_path))
    assert state.path == str(tmp_path)
    assert isinstance(state.files, dict)
    assert len(state.files) == 0

def test_directory_state_add_recording_file(tmp_path, sample_recording_file):
    state = DirectoryState(str(tmp_path))
    state.add_file(sample_recording_file)
    assert len(state.files) == 1
    assert state.files[sample_recording_file.file_path] == sample_recording_file
    assert sample_recording_file.group_dir == str(tmp_path)

# Test RecordingFile
def test_recording_file_initialization():
    file = RecordingFile(
        start_time=datetime(2024, 3, 14, 12, 18, 28),
        end_time=datetime(2024, 3, 14, 12, 35, 18),
        file_path="/record/2024.03.14/12.18.28-12.35.18[F][0@0][140480].dav"
    )
    assert file.start_time == datetime(2024, 3, 14, 12, 18, 28)
    assert file.end_time == datetime(2024, 3, 14, 12, 35, 18)
    assert file.file_path == "/record/2024.03.14/12.18.28-12.35.18[F][0@0][140480].dav"
    assert file.status == "pending"
    assert file.skip == False
    assert file.group_dir is None

def test_recording_file_to_dict():
    file = RecordingFile(
        start_time=datetime(2024, 3, 14, 12, 18, 28),
        end_time=datetime(2024, 3, 14, 12, 35, 18),
        file_path="/record/2024.03.14/12.18.28-12.35.18[F][0@0][140480].dav"
    )
    data = file.to_dict()
    assert data['file_path'] == "/record/2024.03.14/12.18.28-12.35.18[F][0@0][140480].dav"
    assert data['start_time'] == datetime(2024, 3, 14, 12, 18, 28).isoformat()
    assert data['end_time'] == datetime(2024, 3, 14, 12, 35, 18).isoformat()
    assert data['status'] == "pending"
    assert data['skip'] == False

def test_recording_file_from_dict():
    data = {
        'file_path': "/record/2024.03.14/12.18.28-12.35.18[F][0@0][140480].dav",
        'start_time': datetime(2024, 3, 14, 12, 18, 28).isoformat(),
        'end_time': datetime(2024, 3, 14, 12, 35, 18).isoformat(),
        'status': "downloaded",
        'skip': False,
        'group_dir': "/record/2024.03.14",
        'metadata': {'channel': '1'}
    }
    file = RecordingFile.from_dict(data)
    assert file.file_path == "/record/2024.03.14/12.18.28-12.35.18[F][0@0][140480].dav"
    assert file.start_time == datetime(2024, 3, 14, 12, 18, 28)
    assert file.end_time == datetime(2024, 3, 14, 12, 35, 18)
    assert file.status == "downloaded"
    assert file.skip == False
    assert file.group_dir == "/record/2024.03.14"
    assert file.metadata == {'channel': '1'}

def test_recording_file_from_response():
    response_text = "path=/record/2024.03.14/12.18.28-12.35.18[F][0@0][140480].dav&startTime=2024-03-14 12:18:28&endTime=2024-03-14 12:35:18&channel=1"
    files = RecordingFile.from_response(response_text)
    assert len(files) == 1
    assert files[0].file_path == "/record/2024.03.14/12.18.28-12.35.18[F][0@0][140480].dav"
    assert files[0].start_time == datetime(2024, 3, 14, 12, 18, 28)
    assert files[0].end_time == datetime(2024, 3, 14, 12, 35, 18)
    assert files[0].metadata == {'channel': '1'}

# Test find_group_directory function
def test_find_group_directory_creation(tmp_path):
    """Test that a new file with no time relationship to existing files gets a new directory."""
    storage_path = tmp_path
    file_start_time = datetime(2024, 1, 1, 12, 0, 0)
    existing_dirs = []
    
    with patch('video_grouper.video_grouper.create_directory') as mock_create_dir:
        mock_create_dir.side_effect = lambda p: os.makedirs(p, exist_ok=True)
        result = find_group_directory(file_start_time, str(storage_path), existing_dirs)
        
        assert mock_create_dir.called
        
        expected_dir_name = file_start_time.strftime("%Y.%m.%d-%H.%M.%S")
        assert os.path.basename(result) == expected_dir_name

def test_find_group_directory_sequential_files(tmp_path):
    """Test that sequential files are grouped together."""
    storage_path = tmp_path
    
    first_file_start_time = datetime(2024, 1, 1, 12, 0, 0)
    last_file_end_time = datetime(2024, 1, 1, 12, 15, 0)
    group_dir_path = storage_path / first_file_start_time.strftime("%Y.%m.%d-%H.%M.%S")
    group_dir_path.mkdir()

    state_file_path = group_dir_path / "state.json"
    state_content = {
        "path": str(group_dir_path),
        "files": {
            "dummy.dav": {
                "file_path": "dummy.dav",
                "start_time": first_file_start_time.isoformat(),
                "end_time": last_file_end_time.isoformat(),
                "status": "converted", "skip": False, "group_dir": str(group_dir_path),
                "screenshot_path": None, "metadata": {}
            }
        }
    }
    with open(state_file_path, "w") as f:
        json.dump(state_content, f)

    new_file_start_time = last_file_end_time + timedelta(seconds=2)
    
    with patch('video_grouper.video_grouper.create_directory') as mock_create_dir:
        result = find_group_directory(new_file_start_time, str(storage_path), [str(group_dir_path)])
        
        assert result == str(group_dir_path)
        assert not mock_create_dir.called

def test_find_group_directory_non_sequential_files(tmp_path):
    storage_path = tmp_path
    
    last_file_end_time = datetime(2024, 1, 1, 12, 15, 0)
    group_dir_name = (last_file_end_time - timedelta(minutes=15)).strftime("%Y.%m.%d-%H.%M.%S")
    group_dir_path = storage_path.joinpath(group_dir_name)
    group_dir_path.mkdir()

    state_file_path = group_dir_path.joinpath("state.json")
    state_content = {
        "path": str(group_dir_path),
        "files": {
            "dummy.dav": {
                "file_path": "dummy.dav",
                "start_time": (last_file_end_time - timedelta(minutes=15)).isoformat(),
                "end_time": last_file_end_time.isoformat(),
                "status": "converted",
                "skip": False,
                "group_dir": str(group_dir_path),
                "screenshot_path": None,
                "metadata": {}
            }
        }
    }
    with open(state_file_path, "w") as f:
        json.dump(state_content, f)

    existing_dirs = [str(group_dir_path)]
    
    new_file_start_time = last_file_end_time + timedelta(seconds=20)
    
    with patch('video_grouper.video_grouper.create_directory') as mock_create_dir:
        mock_create_dir.side_effect = lambda p: os.makedirs(p, exist_ok=True)
        result = find_group_directory(new_file_start_time, str(storage_path), existing_dirs)
        
        assert mock_create_dir.called
        assert result != str(group_dir_path)
        expected_dir_name = new_file_start_time.strftime("%Y.%m.%d-%H.%M.%S")
        assert os.path.basename(result) == expected_dir_name

# Test file system operations
def test_directory_creation():
    with patch('os.makedirs') as mock_makedirs:
        with patch('os.path.exists', return_value=False):
            create_directory(os.path.join('test', 'videos', '2024.03.14-12.18.28'))
            mock_makedirs.assert_called_once_with(os.path.join('test', 'videos', '2024.03.14-12.18.28'), exist_ok=True)

def test_state_saving(tmp_path):
    with patch('json.dump') as mock_dump, \
         patch('builtins.open', MagicMock()), \
         patch('os.path.exists', return_value=True):
        state = DirectoryState(str(tmp_path))
        state.save_state()
        assert mock_dump.called

def create_mock_group_dir(tmp_path, name="group1", files_data=None):
    if files_data is None:
        files_data = []
    group_dir = tmp_path / name
    group_dir.mkdir()
    
    files = {}
    file_objects = []
    for file_data in files_data:
        file_obj = RecordingFile.from_dict(file_data)
        files[file_data['file_path']] = file_obj
        file_objects.append(file_obj)
    
    state = DirectoryState(str(group_dir))
    state.files = files
    state.save_state()
    
    return group_dir, state, file_objects

@pytest.mark.asyncio
async def test_initialize_audit(video_grouper_app, tmp_path):
    """Tests that the initialize method correctly audits existing directories."""
    group_dir, _, file_objects = create_mock_group_dir(tmp_path, files_data=[
        {
            "file_path": str(tmp_path / "group1" / "video1.dav"), 
            "status": "downloaded", 
            "start_time": "2024-01-01T12:00:00", "end_time": "2024-01-01T12:05:00",
            "metadata": {"path": "server/video1.dav"}
        }
    ])
    file_path = file_objects[0].file_path
    
    await video_grouper_app.initialize()
    
    assert ('convert', file_path) in [await video_grouper_app.ffmpeg_queue.get()]

@pytest.mark.asyncio
async def test_poll_camera_on_reconnect(video_grouper_app, mock_camera, tmp_path):
    video_grouper_app.camera_was_connected = False
    mock_camera.check_availability.return_value = True
    
    file_list = [
        {
            'path': 'server/video1.dav',
            'startTime': '2024-01-01 12:00:00',
            'endTime': '2024-01-01 12:05:00',
        }
    ]
    mock_camera.get_file_list.return_value = file_list
    
    with patch('video_grouper.video_grouper.find_group_directory', return_value=str(tmp_path)):
        await video_grouper_app.sync_files_from_camera()

    mock_camera.get_file_list.assert_called_once()
    assert not video_grouper_app.download_queue.empty()
    download_item = await video_grouper_app.download_queue.get()
    assert download_item.metadata['path'] == 'server/video1.dav'

@pytest.mark.asyncio
async def test_process_download_queue(video_grouper_app, mock_camera, tmp_path):
    group_dir = tmp_path / "group1"
    group_dir.mkdir()
    file_path = str(group_dir / "video1.dav")
    rf = RecordingFile(
        file_path=file_path,
        start_time=datetime.now(),
        end_time=datetime.now(),
        metadata={'path': 'server/video1.dav'},
    )
    dir_state = DirectoryState(str(group_dir))
    dir_state.add_file(rf)
    dir_state.save_state()
    await video_grouper_app.add_to_download_queue(rf)
    
    with patch('os.path.getsize', return_value=12345):
        recording_file = await video_grouper_app.download_queue.get()
        await video_grouper_app.handle_download_task(recording_file)
        
    mock_camera.download_file.assert_called_once_with(file_path='server/video1.dav', local_path=file_path)
    assert not video_grouper_app.ffmpeg_queue.empty()
    ffmpeg_task = await video_grouper_app.ffmpeg_queue.get()
    assert ffmpeg_task == ('convert', file_path)
    
    updated_dir_state = DirectoryState(str(group_dir))
    assert updated_dir_state.files[file_path].status == "downloaded"

@pytest.mark.asyncio
@patch('video_grouper.video_grouper.async_convert_file', new_callable=AsyncMock)
@patch('video_grouper.video_grouper.create_screenshot', new_callable=AsyncMock)
async def test_ffmpeg_handle_conversion_task(mock_create_screenshot, mock_async_convert, video_grouper_app, tmp_path):
    group_dir_name = "2024.01.01-12.00.00"
    group_dir, dir_state, file_objects = create_mock_group_dir(tmp_path, name=group_dir_name, files_data=[
        {
            "file_path": str(tmp_path / group_dir_name / "video1.dav"), 
            "status": "downloaded",
            "start_time": "2024-01-01T12:00:00", "end_time": "2024-01-01T12:05:00",
            "metadata": {"path": "server/video1.dav"}
        }
    ])
    file_path = file_objects[0].file_path
    mp4_path = file_path.replace('.dav', '.mp4')
    mock_async_convert.return_value = mp4_path
    mock_create_screenshot.return_value = True

    await video_grouper_app._handle_conversion_task(file_path)

    mock_async_convert.assert_called_once_with(file_path)
    mock_create_screenshot.assert_called_once_with(mp4_path, mp4_path.replace('.mp4', '_screenshot.jpg'))
    
    updated_dir_state = DirectoryState(str(group_dir))
    file_obj = updated_dir_state.files[file_path]
    assert file_obj.status == "converted"
    assert file_obj.screenshot_path is not None
    
    assert not video_grouper_app.ffmpeg_queue.empty()
    assert await video_grouper_app.ffmpeg_queue.get() == ('combine', str(group_dir))

@pytest.mark.asyncio
async def test_ffmpeg_handle_combine_task(video_grouper_app, tmp_path):
    group_dir, dir_state, _ = create_mock_group_dir(tmp_path, name="group_to_combine", files_data=[
        {
            "file_path": str(tmp_path / "group_to_combine" / "video1.dav"), 
            "status": "converted",
            "start_time": "2024-01-01T12:00:00", "end_time": "2024-01-01T12:05:00",
            "metadata": {"path": "server/video1.dav"}
        }
    ])
    (group_dir / "match_info.ini").touch()
    mp4_path = list(dir_state.files.keys())[0].replace('.dav', '.mp4')
    with open(mp4_path, 'w') as f:
        f.write("dummy")

    with patch('asyncio.create_subprocess_exec') as mock_exec:
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b'', b''))
        mock_exec.return_value = mock_proc
        
        await video_grouper_app._handle_combine_task(str(group_dir))
    
    mock_exec.assert_called_once()
    args, _ = mock_exec.call_args
    assert 'ffmpeg' in args[0]
    assert '-f' in args
    assert 'concat' in args
    
    assert not video_grouper_app.ffmpeg_queue.empty()
    assert await video_grouper_app.ffmpeg_queue.get() == ('trim', str(group_dir))

@pytest.mark.asyncio
async def test_ffmpeg_handle_trim_task(video_grouper_app, tmp_path):
    group_dir = tmp_path / "group_to_trim"
    group_dir.mkdir()
    (group_dir / "combined.mp4").touch()
    
    match_info_path = group_dir / "match_info.ini"
    config = configparser.ConfigParser()
    config['MATCH'] = {
        'home_team': 'Team A',
        'away_team': 'Team B',
        'start_time_offset': '10',
        'total_duration': '90'
    }
    with open(match_info_path, 'w') as f:
        config.write(f)

    with patch('asyncio.create_subprocess_exec') as mock_exec:
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b'', b''))
        mock_exec.return_value = mock_proc
        
        await video_grouper_app._handle_trim_task(str(group_dir))

    mock_exec.assert_called_once()
    args, _ = mock_exec.call_args
    assert 'ffmpeg' in args[0]
    assert '-ss' in args
    assert '10' in args
    assert '-t' in args
    assert str(90*60) in args
    assert str(tmp_path / "Team A vs Team B" / "Team A vs Team B.mp4") in args 

@pytest.mark.asyncio
async def test_initialization_with_existing_state(mock_config, mock_camera_class, tmp_path):
    """
    Test that the app correctly initializes and audits existing directories.
    """
    group_dir = tmp_path / '2025.01.01-12.00.00'
    group_dir.mkdir()
    state_file = group_dir / 'state.json'
    
    downloaded_file_path = str(group_dir / 'video1.dav')
    pending_file_path = str(group_dir / 'video2.dav')

    state_data = {
        "path": str(group_dir),
        "files": {
            downloaded_file_path: {
                "file_path": downloaded_file_path, "status": "downloaded", 
                "start_time": "2025-01-01T12:00:00", "end_time": "2025-01-01T12:05:00",
                "metadata": {"path": "server/video1.dav"}
            },
            pending_file_path: {
                "file_path": pending_file_path, "status": "pending",
                "start_time": "2025-01-01T12:05:00", "end_time": "2025-01-01T12:10:00",
                "metadata": {"path": "server/video2.dav"}
            }
        }
    }
    with open(state_file, 'w') as f:
        json.dump(state_data, f)
    
    app = VideoGrouperApp(mock_config)
    await app.initialize()

    assert app.download_queue.qsize() == 1
    assert app.ffmpeg_queue.qsize() == 1
    
    download_item = await app.download_queue.get()
    assert download_item.file_path == pending_file_path
    
    ffmpeg_item = await app.ffmpeg_queue.get()
    assert ffmpeg_item == ('convert', downloaded_file_path)
