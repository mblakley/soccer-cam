import os
import json
import pytest
import tempfile
import configparser
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, MagicMock, patch, mock_open
from video_grouper.video_grouper_app import VideoGrouperApp
from video_grouper.api_integrations.playmetrics.api import PlayMetricsAPI

@pytest.fixture
def mock_config():
    """Create a mock configuration for testing."""
    return {
        'enabled': 'true',
        'username': 'test@example.com',
        'password': 'password',
        'team_id': '12345',
        'team_name': 'Test Team'
    }

@pytest.fixture
def playmetrics_api(mock_config):
    """Create a PlayMetricsAPI instance with mock configuration."""
    with patch.object(PlayMetricsAPI, '_load_config', return_value=mock_config):
        api = PlayMetricsAPI('mock_config_path')
    yield api

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
    disabled_config = {'enabled': 'false'}
    
    with patch.object(PlayMetricsAPI, '_load_config', return_value=disabled_config):
        api = PlayMetricsAPI('mock_config_path')
        assert api.enabled is False

@pytest.mark.asyncio
async def test_get_team_events():
    """Test getting team events from PlayMetrics."""
    test_config = {
        'enabled': 'true',
        'username': 'test@example.com',
        'password': 'password',
        'team_id': '12345'
    }
    
    with patch.object(PlayMetricsAPI, '_load_config', return_value=test_config):
        api = PlayMetricsAPI('mock_config_path')
        
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

@pytest.mark.asyncio
async def test_find_game_for_recording():
    """Test finding a game for a recording timespan."""
    test_config = {
        'enabled': 'true',
        'username': 'test@example.com',
        'password': 'password',
        'team_name': 'Test Team'
    }
    
    with patch.object(PlayMetricsAPI, '_load_config', return_value=test_config):
        api = PlayMetricsAPI('mock_config_path')
        
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

@pytest.mark.asyncio
async def test_populate_match_info():
    """Test populating match info with PlayMetrics data."""
    test_config = {
        'enabled': 'true',
        'username': 'test@example.com',
        'password': 'password',
        'team_name': 'Test Team'
    }
    
    with patch.object(PlayMetricsAPI, '_load_config', return_value=test_config):
        api = PlayMetricsAPI('mock_config_path')
        
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

 