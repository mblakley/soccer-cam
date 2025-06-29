import os
import pytest
import tempfile
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock, mock_open
from selenium.webdriver.common.by import By
import configparser

from video_grouper.api_integrations.playmetrics.api import PlayMetricsAPI

class TestPlayMetricsCalendarIntegration:
    """Test PlayMetrics API calendar integration."""
    
    def test_initialization(self):
        """Test initialization with config."""
        test_config = {
            'enabled': 'true',
            'username': 'test@example.com',
            'password': 'testpassword',
            'team_id': '123456',
            'team_name': 'Test Team'
        }
        
        with patch.object(PlayMetricsAPI, '_load_config', return_value=test_config):
            api = PlayMetricsAPI('mock_config_path')
            
            # Check that values were loaded correctly
            assert api.enabled == True
            assert api.username == "test@example.com"
            assert api.password == "testpassword"
            assert api.team_id == "123456"
            assert api.team_name == "Test Team"
    
    def test_disabled_when_not_configured(self):
        """Test that API is disabled when not configured."""
        # Mock config without PlayMetrics section
        with patch.object(PlayMetricsAPI, '_load_config', return_value={}):
            api = PlayMetricsAPI('mock_config_path')
            
            # Check that API is disabled
            assert api.enabled == False
    
    @patch('video_grouper.api_integrations.playmetrics.api.webdriver')
    def test_login(self, mock_webdriver):
        """Test login to PlayMetrics."""
        # Mock the webdriver
        mock_driver = MagicMock()
        mock_webdriver.Chrome.return_value = mock_driver
        mock_options = MagicMock()
        mock_webdriver.ChromeOptions.return_value = mock_options
        
        # Set up find_element behavior for CSS_SELECTOR
        def mock_find_element(by, value):
            mock_element = MagicMock()
            if by == By.CSS_SELECTOR:
                if value in ["input[type='email']", "#username", "#email"]:
                    return mock_element
                elif value in ["input[type='password']", "#password"]:
                    return mock_element
            elif by == By.XPATH and value == "//button[@type='submit']":
                return mock_element
            raise Exception(f"Element not found: {by}, {value}")
            
        mock_driver.find_element.side_effect = mock_find_element
        
        # Mock successful login redirect
        mock_driver.current_url = "https://playmetrics.com/dashboard"
        
        test_config = {
            'enabled': 'true',
            'username': 'test@example.com',
            'password': 'testpassword',
            'team_id': '123456',
            'team_name': 'Test Team'
        }
        
        with patch.object(PlayMetricsAPI, '_load_config', return_value=test_config):
            api = PlayMetricsAPI('mock_config_path')
            
            # Call login
            result = api.login()
            
            # Check that login was successful
            assert result == True
            assert api.logged_in == True
            
            # Verify that the driver was called correctly
            mock_driver.get.assert_called_with("https://playmetrics.com/login")
    
    @patch('video_grouper.api_integrations.playmetrics.api.requests')
    def test_download_calendar(self, mock_requests):
        """Test downloading the calendar."""
        # Mock the calendar URL
        calendar_url = "https://api.playmetrics.com/calendar/test.ics"
        
        # Mock the response
        mock_response = MagicMock()
        mock_response.content = b"BEGIN:VCALENDAR\nEND:VCALENDAR"
        mock_requests.get.return_value = mock_response
        
        # Create a PlayMetrics API instance with mocked calendar URL
        api = PlayMetricsAPI()
        api.enabled = True
        api.logged_in = True  # Set logged_in to True
        api.calendar_url = calendar_url
        
        # Call download_calendar with mocked file operations
        with patch('tempfile.mkstemp', return_value=(1, '/tmp/test.ics')):
            with patch('os.close'):
                with patch('builtins.open', mock_open()):
                    result = api.download_calendar()
        
        # Check that the result is the path to the downloaded file
        assert result == '/tmp/test.ics'
        
        # Verify that requests.get was called with the correct URL
        mock_requests.get.assert_called_with(calendar_url)
    
    def test_parse_calendar(self):
        """Test parsing a calendar file."""
        # Create a sample calendar content
        calendar_content = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//PlayMetrics//Calendar//EN
BEGIN:VEVENT
SUMMARY:Test Game vs Opponent
DESCRIPTION:Game description
LOCATION:Test Field
DTSTART:20250615T140000Z
DTEND:20250615T160000Z
END:VEVENT
BEGIN:VEVENT
SUMMARY:Test Practice
DESCRIPTION:Practice description
LOCATION:Practice Field
DTSTART:20250616T180000Z
DTEND:20250616T200000Z
END:VEVENT
END:VCALENDAR"""
        
        # Create a properly initialized API instance
        test_config = {'enabled': 'true'}
        
        with patch.object(PlayMetricsAPI, '_load_config', return_value=test_config):
            api = PlayMetricsAPI('mock_config_path')
            
            # Mock file reading for calendar parsing only
            with patch('builtins.open', mock_open(read_data=calendar_content)):
                # Call parse_calendar
                events = api.parse_calendar('mock_calendar_path')
                
                # Check that events were parsed correctly
                assert len(events) == 2
                
                # Check the game event
                game_event = events[0]
                assert game_event['title'] == "Test Game vs Opponent"
                assert game_event['description'] == "Game description"
                assert game_event['location'] == "Test Field"
                assert game_event['is_game'] == True
                assert game_event['opponent'] == "opponent"
                
                # Check the practice event
                practice_event = events[1]
                assert practice_event['title'] == "Test Practice"
                assert practice_event['description'] == "Practice description"
                assert practice_event['location'] == "Practice Field"
                assert practice_event['is_game'] == False
                assert practice_event['opponent'] is None
    
    def test_find_game_for_recording(self):
        """Test finding a game for a recording timespan."""
        # Create a PlayMetrics API instance
        api = PlayMetricsAPI()
        api.enabled = True
        
        # Create some sample events
        game_time = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
        events = [
            {
                'id': '1',
                'title': 'Test Game vs Opponent',
                'description': 'Game description',
                'location': 'Test Field',
                'start_time': game_time,
                'end_time': game_time + timedelta(hours=2),
                'is_game': True,
                'opponent': 'Opponent'
            },
            {
                'id': '2',
                'title': 'Test Practice',
                'description': 'Practice description',
                'location': 'Practice Field',
                'start_time': game_time + timedelta(days=1),
                'end_time': game_time + timedelta(days=1, hours=2),
                'is_game': False,
                'opponent': None
            }
        ]
        
        # Mock the get_games method
        api.get_games = MagicMock(return_value=events)
        
        # Test finding a game that matches the recording timespan
        recording_start = game_time - timedelta(minutes=30)
        recording_end = game_time + timedelta(hours=1)
        
        game = api.find_game_for_recording(recording_start, recording_end)
        
        # Check that the correct game was found
        assert game is not None
        assert game['id'] == '1'
        assert game['title'] == 'Test Game vs Opponent'
        
        # Test finding a game that doesn't match the recording timespan
        recording_start = game_time + timedelta(days=2)
        recording_end = recording_start + timedelta(hours=1)
        
        game = api.find_game_for_recording(recording_start, recording_end)
        
        # Check that no game was found
        assert game is None
    
    def test_populate_match_info(self):
        """Test populating match info from a game."""
        # Create a PlayMetrics API instance
        api = PlayMetricsAPI()
        api.enabled = True
        
        # Create a sample game
        game_time = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
        game = {
            'id': '1',
            'title': 'Test Game vs Opponent',
            'description': 'Game description',
            'location': 'Test Field',
            'start_time': game_time,
            'end_time': game_time + timedelta(hours=2),
            'is_game': True,
            'opponent': 'Opponent'
        }
        
        # Mock the find_game_for_recording method
        api.find_game_for_recording = MagicMock(return_value=game)
        
        # Create a match info dictionary
        match_info = {}
        
        # Call populate_match_info
        recording_start = game_time - timedelta(minutes=30)
        recording_end = game_time + timedelta(hours=1)
        
        result = api.populate_match_info(match_info, recording_start, recording_end)
        
        # Check that the result is True
        assert result == True
        
        # Check that match_info was populated correctly
        assert match_info['title'] == 'Test Game vs Opponent'
        assert match_info['opponent'] == 'Opponent'
        assert match_info['location'] == 'Test Field'
        assert match_info['date'] == '2025-06-15'
        assert match_info['time'] == '14:00'
        assert match_info['description'] == 'Game description' 