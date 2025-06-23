import os
import pytest
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

from video_grouper.api_integrations.playmetrics.api import PlayMetricsAPI

class TestPlayMetricsCalendarIntegration:
    """Test PlayMetrics API calendar integration."""
    
    def test_initialization(self):
        """Test initialization with config."""
        # Create a temporary config file
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.ini') as f:
            f.write("""
[PLAYMETRICS]
enabled = true
username = test@example.com
password = testpassword
team_id = 123456
team_name = Test Team
""")
            config_path = f.name
        
        try:
            # Initialize the API
            api = PlayMetricsAPI(config_path)
            
            # Check that values were loaded correctly
            assert api.enabled == True
            assert api.username == "test@example.com"
            assert api.password == "testpassword"
            assert api.team_id == "123456"
            assert api.team_name == "Test Team"
        finally:
            # Clean up the temporary file
            os.unlink(config_path)
    
    def test_disabled_when_not_configured(self):
        """Test that API is disabled when not configured."""
        # Create a temporary config file without PlayMetrics section
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.ini') as f:
            f.write("""
[OTHER_SECTION]
foo = bar
""")
            config_path = f.name
        
        try:
            # Initialize the API
            api = PlayMetricsAPI(config_path)
            
            # Check that API is disabled
            assert api.enabled == False
        finally:
            # Clean up the temporary file
            os.unlink(config_path)
    
    @patch('video_grouper.api_integrations.playmetrics.api.webdriver')
    def test_login(self, mock_webdriver):
        """Test login to PlayMetrics."""
        # Mock the webdriver
        mock_driver = MagicMock()
        mock_webdriver.Chrome.return_value = mock_driver
        
        # Mock successful login
        mock_driver.current_url = "https://playmetrics.com/dashboard"
        
        # Create a temporary config file
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.ini') as f:
            f.write("""
[PLAYMETRICS]
enabled = true
username = test@example.com
password = testpassword
team_id = 123456
team_name = Test Team
""")
            config_path = f.name
        
        try:
            # Initialize the API
            api = PlayMetricsAPI(config_path)
            
            # Call login
            result = api.login()
            
            # Check that login was successful
            assert result == True
            assert api.logged_in == True
            
            # Verify that the driver was called correctly
            mock_driver.get.assert_called_with("https://playmetrics.com/login")
        finally:
            # Clean up the temporary file
            os.unlink(config_path)
    
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
        
        # Call download_calendar
        with patch('tempfile.mkstemp', return_value=(1, '/tmp/test.ics')):
            with patch('os.close'):
                with patch('builtins.open', create=True):
                    result = api.download_calendar()
        
        # Check that the result is the path to the downloaded file
        assert result == '/tmp/test.ics'
        
        # Verify that requests.get was called with the correct URL
        mock_requests.get.assert_called_with(calendar_url)
    
    def test_parse_calendar(self):
        """Test parsing a calendar file."""
        # Create a sample calendar file
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
        
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.ics') as f:
            f.write(calendar_content)
            calendar_path = f.name
        
        try:
            # Initialize the API
            api = PlayMetricsAPI()
            
            # Call parse_calendar
            events = api.parse_calendar(calendar_path)
            
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
        finally:
            # Clean up the temporary file
            os.unlink(calendar_path)
    
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