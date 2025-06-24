import os
import pytest
import configparser
from unittest.mock import patch, MagicMock, mock_open
from datetime import datetime, timezone, timedelta
from video_grouper.video_grouper import VideoGrouperApp
from video_grouper.api_integrations.playmetrics.api import PlayMetricsAPI
import tempfile

@pytest.fixture
def mock_config():
    """Create a mock configuration for testing."""
    config = configparser.ConfigParser()
    config.add_section('PLAYMETRICS')
    config.set('PLAYMETRICS', 'enabled', 'true')
    config.set('PLAYMETRICS', 'username', 'test@example.com')
    config.set('PLAYMETRICS', 'password', 'password')
    config.set('PLAYMETRICS', 'team_id', '12345')
    config.set('PLAYMETRICS', 'team_name', 'Test Team')
    return config

@pytest.fixture
def playmetrics_api(mock_config):
    """Create a PlayMetricsAPI instance with mock configuration."""
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.ini') as f:
        mock_config.write(f)
        config_path = f.name
    
    api = PlayMetricsAPI(config_path)
    
    # Clean up the temporary file after the test
    yield api
    os.unlink(config_path)

@pytest.mark.asyncio
async def test_playmetrics_initialization(playmetrics_api):
    """Test that the PlayMetrics API initializes correctly."""
    assert playmetrics_api.enabled is True
    assert playmetrics_api.username == 'test@example.com'
    assert playmetrics_api.password == 'password'
    assert playmetrics_api.team_id == '12345'
    assert playmetrics_api.team_name == 'Test Team'

@pytest.mark.asyncio
async def test_playmetrics_disabled():
    """Test that the PlayMetrics API is disabled when not configured."""
    config = configparser.ConfigParser()
    config.add_section('PLAYMETRICS')
    config.set('PLAYMETRICS', 'enabled', 'false')
    
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.ini') as f:
        config.write(f)
        config_path = f.name
    
    try:
        api = PlayMetricsAPI(config_path)
        assert api.enabled is False
    finally:
        os.unlink(config_path)

@pytest.mark.asyncio
async def test_get_team_events():
    """Test getting team events from PlayMetrics."""
    config = configparser.ConfigParser()
    config.add_section('PLAYMETRICS')
    config.set('PLAYMETRICS', 'enabled', 'true')
    config.set('PLAYMETRICS', 'username', 'test@example.com')
    config.set('PLAYMETRICS', 'password', 'password')
    config.set('PLAYMETRICS', 'team_id', '12345')
    
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.ini') as f:
        config.write(f)
        config_path = f.name
    
    try:
        api = PlayMetricsAPI(config_path)
        
        # Create mock event data
        game_time = datetime(2025, 6, 22, 14, 0, 0, tzinfo=timezone.utc)
        mock_events = [
            {
                "id": "1",
                "title": "Game vs Test Opponent",
                "description": "Game description",
                "location": "Test Field",
                "start_time": game_time,
                "end_time": game_time + timedelta(hours=2),
                "is_game": True,
                "opponent": "Test Opponent"
            }
        ]
        
        # Mock the get_events method
        api.get_events = MagicMock(return_value=mock_events)
        
        # Call the get_games method
        games = api.get_games()
        
        # Check the results
        assert len(games) == 1
        assert games[0]['title'] == 'Game vs Test Opponent'
        assert games[0]['location'] == 'Test Field'
        assert games[0]['is_game'] is True
        assert games[0]['opponent'] == 'Test Opponent'
    finally:
        os.unlink(config_path)

@pytest.mark.asyncio
async def test_find_game_for_recording():
    """Test finding a game for a recording timespan."""
    config = configparser.ConfigParser()
    config.add_section('PLAYMETRICS')
    config.set('PLAYMETRICS', 'enabled', 'true')
    config.set('PLAYMETRICS', 'username', 'test@example.com')
    config.set('PLAYMETRICS', 'password', 'password')
    config.set('PLAYMETRICS', 'team_name', 'Test Team')
    
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.ini') as f:
        config.write(f)
        config_path = f.name
    
    try:
        api = PlayMetricsAPI(config_path)
        
        # Create mock event data
        game_time = datetime(2025, 6, 22, 14, 0, 0, tzinfo=timezone.utc)
        mock_events = [
            {
                "id": "1",
                "title": "Game vs Test Opponent",
                "description": "Game description",
                "location": "Test Field",
                "start_time": game_time,
                "end_time": game_time + timedelta(hours=2),
                "is_game": True,
                "opponent": "Test Opponent"
            },
            {
                "id": "2",
                "title": "Practice",
                "description": "Practice description",
                "location": "Training Ground",
                "start_time": game_time + timedelta(days=1),
                "end_time": game_time + timedelta(days=1, hours=2),
                "is_game": False,
                "opponent": None
            }
        ]
        
        # Mock the get_games method
        api.get_games = MagicMock(return_value=mock_events)
        
        # Test finding a game that overlaps with the recording timespan
        recording_start = game_time - timedelta(minutes=30)
        recording_end = game_time + timedelta(hours=1)
        
        game = api.find_game_for_recording(recording_start, recording_end)
        
        # Check the results
        assert game is not None
        assert game['id'] == '1'
        assert game['title'] == 'Game vs Test Opponent'
        
        # Test finding a game that doesn't match the recording timespan
        recording_start = game_time + timedelta(days=2)
        recording_end = recording_start + timedelta(hours=1)
        
        game = api.find_game_for_recording(recording_start, recording_end)
        
        # Check that no game was found
        assert game is None
    finally:
        os.unlink(config_path)

@pytest.mark.asyncio
async def test_populate_match_info():
    """Test populating match info with PlayMetrics data."""
    config = configparser.ConfigParser()
    config.add_section('PLAYMETRICS')
    config.set('PLAYMETRICS', 'enabled', 'true')
    config.set('PLAYMETRICS', 'username', 'test@example.com')
    config.set('PLAYMETRICS', 'password', 'password')
    config.set('PLAYMETRICS', 'team_name', 'Test Team')
    
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.ini') as f:
        config.write(f)
        config_path = f.name
    
    try:
        api = PlayMetricsAPI(config_path)
        
        # Create mock game data
        game_time = datetime(2025, 6, 22, 14, 0, 0, tzinfo=timezone.utc)
        mock_game = {
            "id": "1",
            "title": "Game vs Test Opponent",
            "description": "Game description",
            "location": "Test Field",
            "start_time": game_time,
            "end_time": game_time + timedelta(hours=2),
            "is_game": True,
            "opponent": "Test Opponent"
        }
        
        # Mock the find_game_for_recording method
        api.find_game_for_recording = MagicMock(return_value=mock_game)
        
        # Create a match info dictionary to populate
        match_info = {}
        
        # Call the populate_match_info method
        recording_start = game_time - timedelta(minutes=30)
        recording_end = game_time + timedelta(hours=1)
        result = api.populate_match_info(match_info, recording_start, recording_end)
        
        # Check the results
        assert result is True
        assert match_info['title'] == 'Game vs Test Opponent'
        assert match_info['opponent'] == 'Test Opponent'
        assert match_info['location'] == 'Test Field'
        assert match_info['date'] == game_time.strftime('%Y-%m-%d')
        assert match_info['time'] == game_time.strftime('%H:%M')
    finally:
        os.unlink(config_path)

@pytest.mark.asyncio
async def test_video_grouper_playmetrics_integration():
    """Test the PlayMetrics integration in the VideoGrouperApp class."""
    # Create a mock config with PlayMetrics enabled
    config = configparser.ConfigParser()
    
    # Add required sections
    config.add_section('CAMERA')
    config.set('CAMERA', 'type', 'dahua')
    config.set('CAMERA', 'device_ip', '192.168.1.100')
    config.set('CAMERA', 'username', 'admin')
    config.set('CAMERA', 'password', 'admin')
    
    config.add_section('STORAGE')
    config.set('STORAGE', 'path', './test_storage')
    
    config.add_section('PLAYMETRICS')
    config.set('PLAYMETRICS', 'enabled', 'true')
    config.set('PLAYMETRICS', 'username', 'test@example.com')
    config.set('PLAYMETRICS', 'password', 'password')
    
    # Create a VideoGrouperApp instance with the mock config
    with patch('video_grouper.video_grouper.PlayMetricsAPI') as mock_playmetrics_class, \
         patch('builtins.open', mock_open()), \
         patch('os.path.exists', return_value=True), \
         patch('os.path.join', lambda *args: '/'.join(args)), \
         patch('os.makedirs'), \
         patch('video_grouper.video_grouper.create_directory'), \
         patch('video_grouper.video_grouper.DahuaCamera'):
        
        # Set up the mock PlayMetrics API
        mock_playmetrics = MagicMock()
        mock_playmetrics.enabled = True
        mock_playmetrics.login = MagicMock(return_value=True)
        mock_playmetrics_class.return_value = mock_playmetrics
        
        app = VideoGrouperApp(config)
        
        # Manually initialize PlayMetrics
        app._initialize_playmetrics()
        
        # Check that the PlayMetrics API was initialized
        assert app.playmetrics_api is not None
        assert app._playmetrics_initialized is True 