import os
import pytest
import asyncio
import configparser
from unittest.mock import patch, AsyncMock, MagicMock
from video_grouper.api_integrations.ntfy import NtfyAPI
from video_grouper.video_grouper import VideoGrouperApp
import time

@pytest.fixture
def mock_config():
    """Create a mock configuration for testing."""
    config = configparser.ConfigParser()
    config.add_section('NTFY')
    config.set('NTFY', 'enabled', 'true')
    config.set('NTFY', 'server_url', 'https://ntfy.sh')
    config.set('NTFY', 'topic', 'test-soccer-cam')
    return config

@pytest.fixture
def ntfy_api(mock_config):
    """Create a NtfyAPI instance with mock configuration."""
    return NtfyAPI(mock_config)

@pytest.fixture
def mock_http_client():
    """Create a mock HTTP client for testing."""
    mock_client = AsyncMock()
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_client.post.return_value = mock_response
    mock_client.put.return_value = mock_response
    mock_client.get.return_value = mock_response
    
    # Mock the AsyncClient context manager
    mock_async_client = AsyncMock()
    mock_async_client.__aenter__.return_value = mock_client
    mock_async_client.__aexit__.return_value = None
    
    return mock_async_client, mock_client

@pytest.mark.asyncio
async def test_ntfy_initialization(ntfy_api):
    """Test that the NTFY API initializes correctly."""
    assert ntfy_api.enabled is True
    assert ntfy_api.base_url == 'https://ntfy.sh'
    assert ntfy_api.topic == 'test-soccer-cam'

@pytest.mark.asyncio
async def test_ntfy_disabled():
    """Test that the NTFY API is disabled when not configured."""
    config = configparser.ConfigParser()
    config.add_section('NTFY')
    config.set('NTFY', 'enabled', 'false')
    
    ntfy_api = NtfyAPI(config)
    assert ntfy_api.enabled is False

@pytest.mark.asyncio
async def test_ntfy_auto_topic():
    """Test that a random topic is generated when not provided."""
    config = configparser.ConfigParser()
    config.add_section('NTFY')
    config.set('NTFY', 'enabled', 'true')
    
    ntfy_api = NtfyAPI(config)
    assert ntfy_api.enabled is True
    assert ntfy_api.topic is not None
    assert ntfy_api.topic.startswith('soccer-cam-')

@pytest.mark.asyncio
async def test_send_notification(mock_http_client):
    """Test sending a notification."""
    mock_async_client, mock_client = mock_http_client
    
    config = configparser.ConfigParser()
    config.add_section('NTFY')
    config.set('NTFY', 'enabled', 'true')
    config.set('NTFY', 'topic', 'test-topic')
    
    ntfy_api = NtfyAPI(config)
    
    # Use patch to mock the httpx.AsyncClient
    with patch('httpx.AsyncClient', return_value=mock_async_client):
        # Test sending a text notification
        result = await ntfy_api.send_notification(
            message="Test message",
            title="Test Title",
            tags=["test", "notification"]
        )
        
        assert result is True
        mock_client.post.assert_called_once()
        args, kwargs = mock_client.post.call_args
        assert args[0] == 'https://ntfy.sh/test-topic'
        assert kwargs['data'] == 'Test message'
        assert kwargs['headers']['Title'] == 'Test Title'
        assert kwargs['headers']['Tags'] == 'test,notification'

@pytest.mark.asyncio
async def test_listen_for_responses():
    """Test the response queue functionality."""
    config = configparser.ConfigParser()
    config.add_section('NTFY')
    config.set('NTFY', 'enabled', 'true')
    config.set('NTFY', 'topic', 'test-topic')
    
    ntfy_api = NtfyAPI(config)
    
    # Directly add a test message to the queue
    await ntfy_api.response_queue.put({
        "id": "456",
        "time": 1642307389,
        "message": "yes",
        "is_affirmative": True
    })
    
    # Check that we can get the message from the queue
    assert ntfy_api.response_queue.qsize() == 1
    response = await ntfy_api.response_queue.get()
    assert response['id'] == '456'
    assert response['message'] == 'yes'
    assert response['is_affirmative'] is True

@pytest.mark.asyncio
async def test_ask_game_start_time(mock_http_client):
    """Test sending notification about game start time."""
    mock_async_client, mock_client = mock_http_client
    
    config = configparser.ConfigParser()
    config.add_section('NTFY')
    config.set('NTFY', 'enabled', 'true')
    config.set('NTFY', 'topic', 'test-topic')
    
    ntfy_api = NtfyAPI(config)
    
    # Create a proper async mock for get_video_duration
    async def mock_get_duration(*args, **kwargs):
        return '01:30:00'
    
    # Mock the necessary functions
    with patch.object(ntfy_api, 'send_notification', AsyncMock(return_value=True)), \
         patch('video_grouper.api_integrations.ntfy.get_video_duration', mock_get_duration), \
         patch('video_grouper.api_integrations.ntfy.create_screenshot', AsyncMock(return_value=True)), \
         patch('video_grouper.api_integrations.ntfy.compress_image', AsyncMock(return_value='test_screenshot.jpg')), \
         patch('video_grouper.api_integrations.ntfy.os.path.exists', return_value=True), \
         patch('video_grouper.api_integrations.ntfy.os.remove'):
        
        # Call the method
        result = await ntfy_api.ask_game_start_time(
            combined_video_path='test_video.mp4',
            group_dir='test_dir'
        )
        
        # Should return None since we're just sending a notification
        assert result is None
        
        # Verify the notification was sent
        ntfy_api.send_notification.assert_called_once()
        args, kwargs = ntfy_api.send_notification.call_args
        assert 'Game start time' in kwargs['message']
        assert kwargs['title'] == 'Set Game Start Time'
        assert kwargs['tags'] == ['warning', 'info']

@pytest.mark.asyncio
async def test_ask_game_end_time(mock_http_client):
    """Test sending notification about game end time."""
    mock_async_client, mock_client = mock_http_client
    
    config = configparser.ConfigParser()
    config.add_section('NTFY')
    config.set('NTFY', 'enabled', 'true')
    config.set('NTFY', 'topic', 'test-topic')
    
    ntfy_api = NtfyAPI(config)
    
    # Create a proper async mock for get_video_duration
    async def mock_get_duration(*args, **kwargs):
        return '02:00:00'
    
    # Mock the necessary functions
    with patch.object(ntfy_api, 'send_notification', AsyncMock(return_value=True)), \
         patch('video_grouper.api_integrations.ntfy.get_video_duration', mock_get_duration), \
         patch('video_grouper.api_integrations.ntfy.create_screenshot', AsyncMock(return_value=True)), \
         patch('video_grouper.api_integrations.ntfy.compress_image', AsyncMock(return_value='test_screenshot.jpg')), \
         patch('video_grouper.api_integrations.ntfy.os.path.exists', return_value=True), \
         patch('video_grouper.api_integrations.ntfy.os.remove'):
        
        # Call the method
        result = await ntfy_api.ask_game_end_time(
            combined_video_path='test_video.mp4',
            group_dir='test_dir',
            start_time_offset='00:10:00'
        )
        
        # Should return None since we're just sending a notification
        assert result is None
        
        # Verify the notification was sent
        ntfy_api.send_notification.assert_called_once()
        args, kwargs = ntfy_api.send_notification.call_args
        assert 'Game end time' in kwargs['message']
        assert kwargs['title'] == 'Set Game End Time'
        assert kwargs['tags'] == ['warning', 'info']

@pytest.mark.asyncio
async def test_video_grouper_ntfy_integration():
    """Test the NTFY integration in the VideoGrouperApp class."""
    # Create a mock config with NTFY enabled
    config = configparser.ConfigParser()
    config.add_section('NTFY')
    config.set('NTFY', 'enabled', 'true')
    config.set('NTFY', 'topic', 'test-topic')
    
    # Create a VideoGrouperApp instance with the mock config
    with patch('video_grouper.video_grouper.NtfyAPI') as mock_ntfy_class:
        # Set up the mock NTFY API
        mock_ntfy = AsyncMock()
        mock_ntfy.enabled = True
        mock_ntfy.configure.return_value = True
        mock_ntfy_class.return_value = mock_ntfy
        
        # Create the app with additional required mocks
        with patch('os.makedirs'), \
             patch('video_grouper.video_grouper.create_directory'):
            app = VideoGrouperApp(config, "./test_storage")
            
            # Call the initialization method directly
            app._initialize_ntfy()
            
            # Check that the NTFY API was initialized
            assert app.ntfy_api is not None
            assert app.ntfy_api == mock_ntfy
            mock_ntfy_class.assert_called_once_with(config)

@pytest.mark.asyncio
async def test_ask_team_info(mock_http_client):
    """Test sending notification about missing team information."""
    mock_async_client, mock_client = mock_http_client
    
    config = configparser.ConfigParser()
    config.add_section('NTFY')
    config.set('NTFY', 'enabled', 'true')
    config.set('NTFY', 'topic', 'test-topic')
    
    ntfy_api = NtfyAPI(config)
    
    # Create a proper async mock for get_video_duration
    async def mock_get_duration(*args, **kwargs):
        return '01:30:00'
    
    # Mock the necessary functions
    with patch.object(ntfy_api, 'send_notification', AsyncMock(return_value=True)), \
         patch('video_grouper.api_integrations.ntfy.get_video_duration', mock_get_duration), \
         patch('video_grouper.api_integrations.ntfy.create_screenshot', AsyncMock(return_value=True)), \
         patch('video_grouper.api_integrations.ntfy.compress_image', AsyncMock(return_value='test_screenshot.jpg')), \
         patch('video_grouper.api_integrations.ntfy.os.path.exists', return_value=True), \
         patch('video_grouper.api_integrations.ntfy.os.remove'):
        
        # Test 1: No existing info
        result = await ntfy_api.ask_team_info(combined_video_path='test_video.mp4')
        
        # Should return empty dict since we're just sending a notification
        assert result == {}
        
        # Verify the notification was sent
        ntfy_api.send_notification.assert_called_once()
        args, kwargs = ntfy_api.send_notification.call_args
        assert 'Missing match information:' in kwargs['message']
        
        # Reset the mock
        ntfy_api.send_notification.reset_mock()
        
        # Test 2: With some existing info
        existing_info = {
            'team_name': 'Existing Team',
            'location': 'Existing Stadium'
        }
        
        result = await ntfy_api.ask_team_info(
            combined_video_path='test_video.mp4',
            existing_info=existing_info
        )
        
        # Should return the existing info
        assert result == existing_info
        
        # Verify the notification was sent
        ntfy_api.send_notification.assert_called_once()
        args, kwargs = ntfy_api.send_notification.call_args
        assert 'Missing match information:' in kwargs['message']
        # Only check that opponent_name is mentioned, don't check for absence of other fields
        assert 'opponent team name' in kwargs['message']

@pytest.mark.asyncio
async def test_ask_resolve_game_conflict():
    """Test asking the user to resolve a game conflict via NTFY."""
    config = configparser.ConfigParser()
    config.add_section('NTFY')
    config.set('NTFY', 'enabled', 'true')
    config.set('NTFY', 'topic', 'test-topic')
    
    ntfy_api = NtfyAPI(config)
    
    # Create mock game options
    game_options = [
        {
            'source': 'TeamSnap',
            'team_name': 'Team A',
            'opponent_name': 'Opponent A',
            'location': 'Field A',
            'date': '2025-06-22'
        },
        {
            'source': 'PlayMetrics',
            'team_name': 'Team B',
            'opponent': 'Opponent B',
            'location': 'Field B',
            'date': '2025-06-22'
        }
    ]
    
    # Create a proper async mock for get_video_duration
    async def mock_get_duration(*args, **kwargs):
        return '01:30:00'
    
    # Create a mock future for simulating user response
    mock_future = asyncio.Future()
    mock_future.set_result({'action': 'http://localhost:8080/select_game/1'})
    
    # Mock the necessary methods
    with patch.object(ntfy_api, 'send_notification', AsyncMock(return_value=True)), \
         patch('video_grouper.api_integrations.ntfy.get_video_duration', mock_get_duration), \
         patch('video_grouper.api_integrations.ntfy.create_screenshot', AsyncMock(return_value=True)), \
         patch('video_grouper.api_integrations.ntfy.compress_image', AsyncMock(return_value='screenshot.jpg')), \
         patch('os.path.exists', return_value=True), \
         patch('os.remove'), \
         patch.object(asyncio, 'Future', return_value=mock_future), \
         patch('asyncio.wait_for', AsyncMock(return_value={'action': 'http://localhost:8080/select_game/1'})):
        
        # Call the method
        result = await ntfy_api.ask_resolve_game_conflict(
            combined_video_path='test_video.mp4',
            group_dir='test_dir',
            game_options=game_options
        )
        
        # Check the result
        assert result is not None
        assert result['source'] == 'PlayMetrics'
        assert result['team_name'] == 'Team B'
        assert result['opponent'] == 'Opponent B'
        
        # Check that send_notification was called with the correct parameters
        ntfy_api.send_notification.assert_called_once()
        call_args = ntfy_api.send_notification.call_args[1]
        assert 'Multiple games found' in call_args['message']
        assert 'Team A vs Opponent A' in call_args['message']
        assert 'Team B vs Opponent B' in call_args['message']
        assert call_args['title'] == 'Game Conflict - Select Correct Game'
        assert len(call_args['actions']) == 2
        assert call_args['actions'][0]['label'] == 'Opponent A (TeamSnap)'
        assert call_args['actions'][1]['label'] == 'Opponent B (PlayMetrics)' 