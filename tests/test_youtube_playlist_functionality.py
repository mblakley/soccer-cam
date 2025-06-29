"""
Tests for YouTube playlist functionality with full mocking.

These tests focus on the core playlist logic without complex file system dependencies.
"""

import os
import pytest
import configparser
import json
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock, mock_open
from datetime import datetime

from video_grouper.task_processors.tasks.upload.youtube_upload_task import YoutubeUploadTask
from video_grouper.task_processors.services.ntfy_service import NtfyService
from video_grouper.models import MatchInfo
from video_grouper.utils.directory_state import DirectoryState
from video_grouper.task_processors.state_auditor import StateAuditor


# Correct patch targets for dependencies used within YoutubeUploadTask
YT_UPLOAD_TASK_PATH = 'video_grouper.task_processors.tasks.upload.youtube_upload_task'

@pytest.mark.asyncio
@patch(f'{YT_UPLOAD_TASK_PATH}.DirectoryState')
@patch('video_grouper.task_processors.services.ntfy_service.NtfyService')
@patch('video_grouper.utils.youtube_upload.YouTubeUploader')
@patch('video_grouper.utils.youtube_upload.get_youtube_paths', return_value=('cred.json', 'token.json'))
@patch('os.path.exists', return_value=True)
@patch('builtins.open', new_callable=mock_open)
@patch('video_grouper.task_processors.tasks.upload.youtube_upload_task.load_config')
@patch('video_grouper.models.MatchInfo.from_file')
async def test_youtube_upload_task_coordination_with_state_playlist(
    mock_match_info_from_file, mock_load_config, mock_open, mock_path_exists,
    mock_get_yt_paths, mock_yt_uploader, mock_ntfy_service, mock_dir_state):
    """Test that the upload task properly coordinates with state-based playlist names."""
    group_dir = "/fake/group_dir"
    
    # Mock MatchInfo
    mock_match_info = MagicMock()
    mock_match_info.my_team_name = "Test Team"
    mock_match_info.get_youtube_title.return_value = "Test Title"
    mock_match_info.get_youtube_description.return_value = "Test Description"
    mock_match_info_from_file.return_value = mock_match_info
    
    # Mock DirectoryState to return playlist name from state
    mock_dir_state_instance = mock_dir_state.return_value
    mock_dir_state_instance.get_youtube_playlist_name.return_value = "State-Playlist"
    
    # Mock load_config to return a valid config object
    from video_grouper.utils.config import Config, YouTubeConfig, NtfyConfig
    mock_config = MagicMock(spec=Config)
    mock_config.youtube = MagicMock(spec=YouTubeConfig)
    mock_config.youtube.enabled = True
    mock_config.youtube.privacy_status = 'private'
    mock_config.youtube.playlist_mapping = {}
    mock_config.ntfy = MagicMock(spec=NtfyConfig)
    mock_config.ntfy.enabled = True
    mock_load_config.return_value = mock_config
    
    # Mock NtfyService
    mock_ntfy_instance = mock_ntfy_service.return_value
    mock_ntfy_instance.is_waiting_for_input.return_value = False
    
    # Mock YouTubeUploader
    mock_uploader_instance = mock_yt_uploader.return_value
    mock_uploader_instance.upload_video.return_value = "video_id_123"
    mock_uploader_instance.get_or_create_playlist.return_value = "playlist_id_123"
    
    # Execute the task
    task = YoutubeUploadTask(group_dir=group_dir)
    result = await task.execute()
    
    # Verify success
    assert result is True
    
    # Verify playlist creation calls
    mock_uploader_instance.get_or_create_playlist.assert_called()
    playlist_calls = mock_uploader_instance.get_or_create_playlist.call_args_list
    playlist_names = [call[0][0] for call in playlist_calls]
    
    # Should create playlist for raw videos with " - Full Field" suffix
    assert "State-Playlist - Full Field" in playlist_names


@pytest.mark.asyncio 
@patch(f'{YT_UPLOAD_TASK_PATH}.DirectoryState')
@patch('video_grouper.task_processors.services.ntfy_service.NtfyService')
@patch('video_grouper.utils.youtube_upload.YouTubeUploader')
@patch('video_grouper.utils.youtube_upload.get_youtube_paths', return_value=('cred.json', 'token.json'))
@patch('os.path.exists', return_value=True)
@patch('builtins.open', new_callable=mock_open)
@patch('video_grouper.task_processors.tasks.upload.youtube_upload_task.load_config')
@patch('video_grouper.models.MatchInfo.from_file')
async def test_youtube_upload_task_coordination_with_config_mapping(
    mock_match_info_from_file, mock_load_config, mock_open, mock_path_exists,
    mock_get_yt_paths, mock_yt_uploader, mock_ntfy_service, mock_dir_state):
    """Test that the upload task properly uses config-based playlist mappings."""
    group_dir = "/fake/group_dir"
        
    # Mock MatchInfo
    mock_match_info = MagicMock()
    mock_match_info.my_team_name = "Test Team"
    mock_match_info.get_youtube_title.return_value = "Test Title"
    mock_match_info.get_youtube_description.return_value = "Test Description"
    mock_match_info_from_file.return_value = mock_match_info
    
    # Mock DirectoryState to return no playlist name (so it uses config)
    mock_dir_state_instance = mock_dir_state.return_value
    mock_dir_state_instance.get_youtube_playlist_name.return_value = None
    
    # Mock config to have playlist mapping
    from video_grouper.utils.config import Config, YouTubeConfig, NtfyConfig
    mock_config = MagicMock(spec=Config)
    mock_config.youtube = MagicMock(spec=YouTubeConfig)
    mock_config.youtube.enabled = True
    mock_config.youtube.privacy_status = 'private'
    mock_config.youtube.playlist_mapping = {'Test Team': 'Config-Playlist'}
    mock_config.ntfy = MagicMock(spec=NtfyConfig)
    mock_config.ntfy.enabled = True
    mock_load_config.return_value = mock_config
    
    # Mock NtfyService
    mock_ntfy_instance = mock_ntfy_service.return_value
    mock_ntfy_instance.is_waiting_for_input.return_value = False
    
    # Mock YouTubeUploader
    mock_uploader_instance = mock_yt_uploader.return_value
    mock_uploader_instance.upload_video.return_value = "video_id_123"
    mock_uploader_instance.get_or_create_playlist.return_value = "playlist_id_123"
    
    # Execute the task
    task = YoutubeUploadTask(group_dir=group_dir)
    result = await task.execute()
    
    # Verify success
    assert result is True
    
    # Verify playlist creation calls
    mock_uploader_instance.get_or_create_playlist.assert_called()
    playlist_calls = mock_uploader_instance.get_or_create_playlist.call_args_list
    playlist_names = [call[0][0] for call in playlist_calls]
    
    # Should use config mapping and create raw video playlist
    assert "Config-Playlist - Full Field" in playlist_names


@pytest.mark.asyncio
@patch(f'{YT_UPLOAD_TASK_PATH}.DirectoryState')
@patch('video_grouper.task_processors.services.ntfy_service.NtfyService')
@patch('video_grouper.utils.youtube_upload.YouTubeUploader')
@patch('video_grouper.utils.youtube_upload.get_youtube_paths', return_value=('cred.json', 'token.json'))
@patch('os.path.exists', return_value=True)
@patch('builtins.open', new_callable=mock_open)
@patch('video_grouper.task_processors.tasks.upload.youtube_upload_task.load_config')
@patch('video_grouper.models.MatchInfo.from_file')
async def test_youtube_upload_task_requests_playlist_when_not_found(
    mock_match_info_from_file, mock_load_config, mock_open, mock_path_exists,
    mock_get_yt_paths, mock_yt_uploader, mock_ntfy_service, mock_dir_state):
    """Test that the upload task requests playlist name when no mapping exists."""
    group_dir = "/fake/group_dir"
    
    # Mock MatchInfo
    mock_match_info = MagicMock()
    mock_match_info.my_team_name = "Test Team"
    mock_match_info_from_file.return_value = mock_match_info
    
    # Mock DirectoryState to return no playlist name
    mock_dir_state_instance = mock_dir_state.return_value
    mock_dir_state_instance.get_youtube_playlist_name.return_value = None
    
    # Mock config to have no playlist mapping
    from video_grouper.utils.config import Config, YouTubeConfig, NtfyConfig
    mock_config = MagicMock(spec=Config)
    mock_config.youtube = MagicMock(spec=YouTubeConfig)
    mock_config.youtube.enabled = True
    mock_config.youtube.privacy_status = 'private'
    mock_config.youtube.playlist_mapping = {}
    mock_config.ntfy = MagicMock(spec=NtfyConfig)
    mock_config.ntfy.enabled = True
    mock_load_config.return_value = mock_config
    
    # Mock NtfyService
    mock_ntfy_instance = mock_ntfy_service.return_value
    mock_ntfy_instance.is_waiting_for_input.return_value = False
    mock_ntfy_instance.request_playlist_name = AsyncMock(return_value=True)
    
    # Execute the task
    task = YoutubeUploadTask(group_dir=group_dir)
    result = await task.execute()
    
    # Should return False because no playlist name is available
    assert result is False
    
    # Verify playlist request was made
    mock_ntfy_instance.request_playlist_name.assert_called_once_with(group_dir, "Test Team")


@pytest.mark.asyncio
@patch(f'{YT_UPLOAD_TASK_PATH}.DirectoryState')
@patch('video_grouper.task_processors.services.ntfy_service.NtfyService')
@patch('video_grouper.utils.youtube_upload.YouTubeUploader')
@patch('video_grouper.utils.youtube_upload.get_youtube_paths', return_value=('cred.json', 'token.json'))
@patch('os.path.exists', return_value=True)
@patch('builtins.open', new_callable=mock_open)
@patch('video_grouper.task_processors.tasks.upload.youtube_upload_task.load_config')
@patch('video_grouper.models.MatchInfo.from_file')
async def test_youtube_upload_task_skips_request_if_already_waiting(
    mock_match_info_from_file, mock_load_config, mock_open, mock_path_exists,
    mock_get_yt_paths, mock_yt_uploader, mock_ntfy_service, mock_dir_state):
    """Test that the upload task skips requesting playlist if already waiting for response."""
    group_dir = "/fake/group_dir"
    
    # Mock MatchInfo
    mock_match_info = MagicMock()
    mock_match_info.my_team_name = "Test Team"
    mock_match_info_from_file.return_value = mock_match_info
    
    # Mock DirectoryState to return no playlist name
    mock_dir_state_instance = mock_dir_state.return_value
    mock_dir_state_instance.get_youtube_playlist_name.return_value = None
    
    # Mock config to have no playlist mapping
    from video_grouper.utils.config import Config, YouTubeConfig, NtfyConfig
    mock_config = MagicMock(spec=Config)
    mock_config.youtube = MagicMock(spec=YouTubeConfig)
    mock_config.youtube.enabled = True
    mock_config.youtube.privacy_status = 'private'
    mock_config.youtube.playlist_mapping = {}
    mock_config.ntfy = MagicMock(spec=NtfyConfig)
    mock_config.ntfy.enabled = True
    mock_load_config.return_value = mock_config
    
    # Mock NtfyService to indicate already waiting for input
    mock_ntfy_instance = mock_ntfy_service.return_value
    mock_ntfy_instance.is_waiting_for_input.return_value = True
    mock_ntfy_instance.request_playlist_name = AsyncMock()
    
    # Execute the task
    task = YoutubeUploadTask(group_dir=group_dir)
    result = await task.execute()
    
    # Should return False because waiting for input
    assert result is False
    
    # Verify no new playlist request was made
    mock_ntfy_instance.request_playlist_name.assert_not_called()


@pytest.mark.asyncio
async def test_ntfy_service_playlist_request():
    """Test that NtfyService can request playlist names."""
    config = configparser.ConfigParser()
    config.add_section('NTFY')
    config.set('NTFY', 'enabled', 'true')
    config.set('NTFY', 'topic', 'test_topic')
    config.set('NTFY', 'server_url', 'https://ntfy.sh')
    
    with patch('video_grouper.task_processors.services.ntfy_service.NtfyAPI') as mock_api_class:
        # Mock the API instance
        mock_api = mock_api_class.return_value
        mock_api.enabled = True
        mock_api.initialize = AsyncMock()
        mock_api.send_notification = AsyncMock(return_value=True)
        
        # Create service
        service = NtfyService(config, '/fake/storage/path')
        service.ntfy_api = mock_api
        
        # Request playlist name
        result = await service.request_playlist_name('/fake/group1', "Test Team")
        
        # Verify the request was made
        assert result is True
        assert service.is_waiting_for_input('/fake/group1')


def test_youtube_uploader_class_functionality():
    """Test that YouTubeUploader class has the expected interface."""
    from video_grouper.utils.youtube_upload import YouTubeUploader
    
    # Test that the class can be instantiated
    uploader = YouTubeUploader('fake_creds.json', 'fake_token.json')
    
    # Test that it has the expected methods
    assert hasattr(uploader, 'upload_video')
    assert hasattr(uploader, 'get_or_create_playlist')
    assert hasattr(uploader, 'find_playlist_by_name')
    assert hasattr(uploader, 'create_playlist')
    assert hasattr(uploader, 'add_video_to_playlist')
    assert hasattr(uploader, 'authenticate')


def test_match_info_youtube_methods():
    """Test that MatchInfo has YouTube-specific methods."""
    # Create a temporary match info file
    import tempfile
    import os
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.ini', delete=False) as f:
        f.write("""[MATCH]
my_team_name = Test Team
opponent_team_name = Rival Team
location = Field 5
date = 2024-01-15
time = 14:30
""")
        temp_path = f.name
    
    try:
        # Load match info
        match_info = MatchInfo.from_file(temp_path)
        assert match_info is not None
        
        # Test YouTube-specific methods exist and work
        assert hasattr(match_info, 'get_youtube_title')
        assert hasattr(match_info, 'get_youtube_description')
        
        # Test the methods return reasonable values
        title = match_info.get_youtube_title('processed')
        assert isinstance(title, str)
        assert len(title) > 0
        
        description = match_info.get_youtube_description('processed')
        assert isinstance(description, str)
        assert len(description) > 0
        
    finally:
        os.unlink(temp_path)


def test_directory_state_playlist_methods_interface():
    """Test that DirectoryState has playlist-related methods."""
    # Test that the class has the expected methods
    assert hasattr(DirectoryState, 'get_youtube_playlist_name')
    assert hasattr(DirectoryState, 'set_youtube_playlist_name')
    
    # The actual functionality is tested via integration tests
    # since it involves complex file operations 