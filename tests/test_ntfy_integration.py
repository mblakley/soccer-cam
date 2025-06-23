import os
import pytest
import asyncio
import configparser
from unittest.mock import patch, AsyncMock, MagicMock
from video_grouper.api_integrations.ntfy import NtfyAPI
from video_grouper.video_grouper import VideoGrouperApp

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
async def test_send_notification():
    """Test sending a notification."""
    config = configparser.ConfigParser()
    config.add_section('NTFY')
    config.set('NTFY', 'enabled', 'true')
    config.set('NTFY', 'topic', 'test-topic')
    
    ntfy_api = NtfyAPI(config)
    
    # Mock the httpx client
    mock_response = AsyncMock()
    mock_response.status_code = 200
    
    # Use patch to mock the httpx.AsyncClient context manager
    with patch('httpx.AsyncClient') as mock_client_class:
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.put.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client
        
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
async def test_ask_game_start_time():
    """Test asking for game start time."""
    config = configparser.ConfigParser()
    config.add_section('NTFY')
    config.set('NTFY', 'enabled', 'true')
    config.set('NTFY', 'topic', 'test-topic')
    
    ntfy_api = NtfyAPI(config)
    
    # Create a proper async mock for get_video_duration
    async def mock_get_duration(*args, **kwargs):
        return '01:30:00'
    
    # Create a mock for send_notification that captures the message_id
    message_ids = []
    
    async def mock_send_notification(*args, **kwargs):
        # Extract message_id from the message
        message = kwargs.get('message', '')
        if 'ID:' in message:
            message_id = message.split('ID:')[-1].strip()
            message_ids.append(message_id)
        return True
    
    # Mock the necessary functions
    with patch('video_grouper.api_integrations.ntfy.get_video_duration', mock_get_duration), \
         patch('video_grouper.api_integrations.ntfy.create_screenshot', return_value=True), \
         patch('video_grouper.api_integrations.ntfy.os.path.exists', return_value=True), \
         patch.object(ntfy_api, 'send_notification', mock_send_notification), \
         patch('video_grouper.api_integrations.ntfy.os.remove'):
        
        # Start the ask_game_start_time task but don't await it yet
        task = asyncio.create_task(ntfy_api.ask_game_start_time(
            combined_video_path='test_video.mp4',
            group_dir='test_dir'
        ))
        
        # Give it a moment to send the notification
        await asyncio.sleep(0.1)
        
        # Simulate a user response by completing the future
        if message_ids:
            message_id = message_ids[0]
            if message_id in ntfy_api.pending_messages:
                # Complete the future with a positive response
                ntfy_api.pending_messages[message_id].set_result("Yes, game started at 00:05:00")
        
        # Now await the task
        result = await task
        
        # Should return a start time offset
        assert result is not None
        assert ':' in result  # Should be in HH:MM:SS format

@pytest.mark.asyncio
async def test_ask_game_end_time():
    """Test asking for game end time."""
    config = configparser.ConfigParser()
    config.add_section('NTFY')
    config.set('NTFY', 'enabled', 'true')
    config.set('NTFY', 'topic', 'test-topic')
    
    ntfy_api = NtfyAPI(config)
    
    # Create a proper async mock for get_video_duration
    async def mock_get_duration(*args, **kwargs):
        return '02:00:00'
    
    # Create a mock for send_notification that captures the message_id
    message_ids = []
    
    async def mock_send_notification(*args, **kwargs):
        # Extract message_id from the message
        message = kwargs.get('message', '')
        if 'ID:' in message:
            message_id = message.split('ID:')[-1].strip()
            message_ids.append(message_id)
        return True
    
    # Mock the necessary functions
    with patch('video_grouper.api_integrations.ntfy.get_video_duration', mock_get_duration), \
         patch('video_grouper.api_integrations.ntfy.create_screenshot', return_value=True), \
         patch('video_grouper.api_integrations.ntfy.os.path.exists', return_value=True), \
         patch.object(ntfy_api, 'send_notification', mock_send_notification), \
         patch('video_grouper.api_integrations.ntfy.os.remove'):
        
        # Start the ask_game_end_time task but don't await it yet
        task = asyncio.create_task(ntfy_api.ask_game_end_time(
            combined_video_path='test_video.mp4',
            group_dir='test_dir',
            start_time_offset='00:10:00'
        ))
        
        # Give it a moment to send the notification
        await asyncio.sleep(0.1)
        
        # Simulate a user response by completing the future
        if message_ids:
            message_id = message_ids[0]
            if message_id in ntfy_api.pending_messages:
                # Complete the future with a positive response
                ntfy_api.pending_messages[message_id].set_result("Yes, game ended at 01:30:00")
        
        # Now await the task
        result = await task
        
        # Should return a duration
        assert result is not None
        assert ':' in result  # Should be in HH:MM:SS format

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