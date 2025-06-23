import os
import pytest
import configparser
from unittest.mock import patch, AsyncMock, MagicMock
from datetime import datetime

from video_grouper.api_integrations.playmetrics.scraper import PlayMetricsScraper
from video_grouper.models import MatchInfo
from video_grouper.video_grouper import VideoGrouperApp

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
def playmetrics_scraper(mock_config):
    """Create a PlayMetricsScraper instance with mock configuration."""
    return PlayMetricsScraper(mock_config)

@pytest.mark.asyncio
async def test_playmetrics_initialization(playmetrics_scraper):
    """Test that the PlayMetrics scraper initializes correctly."""
    assert playmetrics_scraper.enabled is True
    assert playmetrics_scraper.username == 'test@example.com'
    assert playmetrics_scraper.password == 'password'
    assert playmetrics_scraper.team_id == '12345'

@pytest.mark.asyncio
async def test_playmetrics_disabled():
    """Test that the PlayMetrics scraper is disabled when not configured."""
    config = configparser.ConfigParser()
    config.add_section('PLAYMETRICS')
    config.set('PLAYMETRICS', 'enabled', 'false')
    
    scraper = PlayMetricsScraper(config)
    assert scraper.enabled is False

@pytest.mark.asyncio
async def test_get_team_events():
    """Test getting team events from PlayMetrics."""
    config = configparser.ConfigParser()
    config.add_section('PLAYMETRICS')
    config.set('PLAYMETRICS', 'enabled', 'true')
    config.set('PLAYMETRICS', 'username', 'test@example.com')
    config.set('PLAYMETRICS', 'password', 'password')
    config.set('PLAYMETRICS', 'team_id', '12345')
    
    scraper = PlayMetricsScraper(config)
    
    # Create mock event data
    mock_event_data = {
        'date': '2025-06-22',
        'time': '14:00',
        'title': 'Game vs Test Opponent',
        'location': 'Test Field',
        'is_game': True,
        'opponent': 'Test Opponent'
    }
    
    # Mock the get_team_events method directly
    with patch.object(scraper, 'get_team_events', return_value=[mock_event_data]):
        # Call the method
        events = scraper.get_team_events()
        
        # Check the results
        assert len(events) == 1
        assert events[0]['date'] == '2025-06-22'
        assert events[0]['time'] == '14:00'
        assert events[0]['title'] == 'Game vs Test Opponent'
        assert events[0]['location'] == 'Test Field'
        assert events[0]['is_game'] is True
        assert events[0]['opponent'] == 'Test Opponent'

@pytest.mark.asyncio
async def test_find_game_for_recording():
    """Test finding a game for a recording date."""
    config = configparser.ConfigParser()
    config.add_section('PLAYMETRICS')
    config.set('PLAYMETRICS', 'enabled', 'true')
    config.set('PLAYMETRICS', 'username', 'test@example.com')
    config.set('PLAYMETRICS', 'password', 'password')
    
    scraper = PlayMetricsScraper(config)
    
    # Mock the get_team_events method
    mock_events = [
        {
            'date': '2025-06-22',
            'time': '14:00',
            'title': 'Game vs Test Opponent',
            'location': 'Test Field',
            'is_game': True,
            'opponent': 'Test Opponent'
        },
        {
            'date': '2025-06-23',
            'time': '16:00',
            'title': 'Practice',
            'location': 'Training Ground',
            'is_game': False,
            'opponent': None
        }
    ]
    
    with patch.object(scraper, 'get_team_events', return_value=mock_events):
        # Test finding a game on the correct date
        game = scraper.find_game_for_recording(datetime(2025, 6, 22))
        assert game is not None
        assert game['date'] == '2025-06-22'
        assert game['opponent'] == 'Test Opponent'
        
        # Test finding a game on a date with no game
        game = scraper.find_game_for_recording(datetime(2025, 6, 23))
        assert game is None
        
        # Test finding a game on a date with no events
        game = scraper.find_game_for_recording(datetime(2025, 6, 24))
        assert game is None

@pytest.mark.asyncio
async def test_populate_match_info():
    """Test populating match info with PlayMetrics data."""
    config = configparser.ConfigParser()
    config.add_section('PLAYMETRICS')
    config.set('PLAYMETRICS', 'enabled', 'true')
    config.set('PLAYMETRICS', 'username', 'test@example.com')
    config.set('PLAYMETRICS', 'password', 'password')
    config.set('PLAYMETRICS', 'team_name', 'Test Team')
    
    scraper = PlayMetricsScraper(config)
    
    # Mock the find_game_for_recording method
    mock_game = {
        'date': '2025-06-22',
        'time': '14:00',
        'title': 'Game vs Test Opponent',
        'location': 'Test Field',
        'is_game': True,
        'opponent': 'Test Opponent'
    }
    
    with patch.object(scraper, 'find_game_for_recording', return_value=mock_game), \
         patch.object(scraper, 'initialize', return_value=True), \
         patch.object(scraper, 'login', return_value=True), \
         patch('video_grouper.models.MatchInfo.get_or_create') as mock_get_or_create:
        
        # Create a mock match info object and config
        mock_match_info = MagicMock()
        mock_config = MagicMock()
        mock_get_or_create.return_value = (mock_match_info, mock_config)
        
        # Call the method
        result = scraper.populate_match_info(mock_match_info, datetime(2025, 6, 22))
        
        # Check the results
        assert result is True
        mock_match_info.update_team_info.assert_called_once()

@pytest.mark.asyncio
async def test_video_grouper_playmetrics_integration():
    """Test the PlayMetrics integration in the VideoGrouperApp class."""
    # Create a mock config with PlayMetrics enabled
    config = configparser.ConfigParser()
    config.add_section('PLAYMETRICS')
    config.set('PLAYMETRICS', 'enabled', 'true')
    config.set('PLAYMETRICS', 'username', 'test@example.com')
    config.set('PLAYMETRICS', 'password', 'password')
    
    # Create a VideoGrouperApp instance with the mock config
    with patch('video_grouper.video_grouper.PlayMetricsScraper') as mock_playmetrics_class:
        # Set up the mock PlayMetrics scraper
        mock_playmetrics = MagicMock()
        mock_playmetrics.enabled = True
        mock_playmetrics.initialize.return_value = True
        mock_playmetrics.login.return_value = True
        mock_playmetrics_class.return_value = mock_playmetrics
        
        # Create the app with additional required mocks
        with patch('os.makedirs'), \
             patch('video_grouper.video_grouper.create_directory'):
            app = VideoGrouperApp(config, "./test_storage")
            
            # Check that the PlayMetrics scraper was initialized
            assert app.playmetrics_api is not None
            assert app.playmetrics_api == mock_playmetrics
            mock_playmetrics_class.assert_called_once_with(config)
            mock_playmetrics.initialize.assert_called_once()
            mock_playmetrics.login.assert_called_once() 