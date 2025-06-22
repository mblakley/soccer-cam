#!/usr/bin/env python3
"""
Tests for the TeamSnap API integration.
"""

import os
import sys
import unittest
import configparser
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

# Add the parent directory to the path so we can import the video_grouper package
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from video_grouper.api_integrations.teamsnap import TeamSnapAPI

class TestTeamSnapAPI(unittest.TestCase):
    """Tests for the TeamSnap API integration."""
    
    def setUp(self):
        """Set up the test."""
        # Create a mock config
        self.config_path = "test_config.ini"
        config = configparser.ConfigParser()
        config.add_section('TEAMSNAP')
        config.set('TEAMSNAP', 'enabled', 'true')
        config.set('TEAMSNAP', 'client_id', 'test_client_id')
        config.set('TEAMSNAP', 'client_secret', 'test_client_secret')
        config.set('TEAMSNAP', 'access_token', 'test_access_token')
        config.set('TEAMSNAP', 'team_id', 'test_team_id')
        config.set('TEAMSNAP', 'my_team_name', 'Test Team')
        
        with open(self.config_path, 'w') as f:
            config.write(f)
        
        # Create a TeamSnap API instance
        self.api = TeamSnapAPI(self.config_path)
        
        # Create mock games
        self.games = [
            {
                'id': '1',
                'start_date': '2025-03-08T17:10:14Z',
                'opponent_name': 'Opponent 1',
                'location_name': 'Location 1',
                'duration_in_minutes': '90'
            },
            {
                'id': '2',
                'start_date': '2025-03-09T14:00:00Z',
                'opponent_name': 'Opponent 2',
                'location_name': 'Location 2',
                'duration_in_minutes': '90'
            }
        ]
    
    def tearDown(self):
        """Clean up after the test."""
        if os.path.exists(self.config_path):
            os.remove(self.config_path)
    
    def test_initialization(self):
        """Test that the TeamSnap API initializes correctly."""
        self.assertTrue(self.api.enabled)
        self.assertEqual(self.api.team_id, 'test_team_id')
        self.assertEqual(self.api.my_team_name, 'Test Team')
    
    @patch('video_grouper.api_integrations.teamsnap.TeamSnapAPI._make_api_request')
    def test_discover_api_endpoints(self, mock_make_api_request):
        """Test that the API endpoints are discovered correctly."""
        # Mock the API response
        mock_make_api_request.return_value = {
            'collection': {
                'links': [
                    {'rel': 'events', 'href': 'https://api.teamsnap.com/v3/events'},
                    {'rel': 'teams', 'href': 'https://api.teamsnap.com/v3/teams'}
                ]
            }
        }
        
        # Call the method
        self.api._discover_api_endpoints()
        
        # Check that the endpoints were discovered
        self.assertEqual(self.api.endpoints['events'], 'https://api.teamsnap.com/v3/events')
        self.assertEqual(self.api.endpoints['teams'], 'https://api.teamsnap.com/v3/teams')
    
    @patch('video_grouper.api_integrations.teamsnap.TeamSnapAPI._make_api_request')
    def test_get_team_events(self, mock_make_api_request):
        """Test that team events are retrieved correctly."""
        # Mock the API response
        mock_make_api_request.return_value = {
            'collection': {
                'items': [
                    {
                        'data': [
                            {'name': 'id', 'value': '1'},
                            {'name': 'start_date', 'value': '2025-03-08T17:10:14Z'},
                            {'name': 'opponent_name', 'value': 'Opponent 1'},
                            {'name': 'location_name', 'value': 'Location 1'}
                        ]
                    },
                    {
                        'data': [
                            {'name': 'id', 'value': '2'},
                            {'name': 'start_date', 'value': '2025-03-09T14:00:00Z'},
                            {'name': 'opponent_name', 'value': 'Opponent 2'},
                            {'name': 'location_name', 'value': 'Location 2'}
                        ]
                    }
                ]
            }
        }
        
        # Set up the endpoints
        self.api.endpoints = {'events': 'https://api.teamsnap.com/v3/events'}
        
        # Call the method
        events = self.api.get_team_events()
        
        # Check that the events were retrieved
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]['id'], '1')
        self.assertEqual(events[0]['start_date'], '2025-03-08T17:10:14Z')
        self.assertEqual(events[0]['opponent_name'], 'Opponent 1')
        self.assertEqual(events[0]['location_name'], 'Location 1')
    
    @patch('video_grouper.api_integrations.teamsnap.TeamSnapAPI.get_team_events')
    def test_get_games(self, mock_get_team_events):
        """Test that games are filtered correctly."""
        # Mock the get_team_events method
        mock_get_team_events.return_value = [
            {
                'id': '1',
                'start_date': '2025-03-08T17:10:14Z',
                'opponent_name': 'Opponent 1',
                'location_name': 'Location 1',
                'event_type': 'game'
            },
            {
                'id': '2',
                'start_date': '2025-03-09T14:00:00Z',
                'opponent_name': 'Opponent 2',
                'location_name': 'Location 2',
                'event_type': 'practice'
            },
            {
                'id': '3',
                'start_date': '2025-03-10T15:00:00Z',
                'opponent_name': 'Opponent 3',
                'location_name': 'Location 3',
                'event_type': 'game'
            }
        ]
        
        # Call the method
        games = self.api.get_games()
        
        # Check that only games were returned
        self.assertEqual(len(games), 2)
        self.assertEqual(games[0]['id'], '1')
        self.assertEqual(games[1]['id'], '3')
    
    @patch('video_grouper.api_integrations.teamsnap.TeamSnapAPI.get_games')
    def test_find_game_for_recording(self, mock_get_games):
        """Test that games are found correctly for a recording timespan."""
        # Mock the get_games method
        mock_get_games.return_value = self.games
        
        # Test case 1: Recording overlaps with a game
        recording_start = datetime(2025, 3, 8, 17, 15, 0, tzinfo=timezone.utc)
        recording_end = recording_start + timedelta(minutes=60)
        
        game = self.api.find_game_for_recording(recording_start, recording_end)
        
        self.assertIsNotNone(game)
        self.assertEqual(game['id'], '1')
        
        # Test case 2: Recording does not overlap with any game
        recording_start = datetime(2025, 3, 10, 17, 15, 0, tzinfo=timezone.utc)
        recording_end = recording_start + timedelta(minutes=60)
        
        game = self.api.find_game_for_recording(recording_start, recording_end)
        
        self.assertIsNone(game)
    
    @patch('video_grouper.api_integrations.teamsnap.TeamSnapAPI.find_game_for_recording')
    def test_populate_match_info(self, mock_find_game):
        """Test that match info is populated correctly."""
        # Mock the find_game_for_recording method
        mock_find_game.return_value = self.games[0]
        
        # Create a recording timespan
        recording_start = datetime(2025, 3, 8, 17, 15, 0, tzinfo=timezone.utc)
        recording_end = recording_start + timedelta(minutes=60)
        
        # Create an empty match info dictionary
        match_info = {}
        
        # Call the method
        success = self.api.populate_match_info(match_info, recording_start, recording_end)
        
        # Check that the match info was populated
        self.assertTrue(success)
        self.assertEqual(match_info['home_team'], 'Test Team')
        self.assertEqual(match_info['away_team'], 'Opponent 1')
        self.assertEqual(match_info['location'], 'Location 1')
        self.assertEqual(match_info['date'], '2025-03-08')
        self.assertEqual(match_info['time'], '17:10')
        
        # Test case 2: No game found
        mock_find_game.return_value = None
        
        # Create an empty match info dictionary
        match_info = {}
        
        # Call the method
        success = self.api.populate_match_info(match_info, recording_start, recording_end)
        
        # Check that the match info was not populated
        self.assertFalse(success)
        self.assertEqual(match_info, {})

if __name__ == '__main__':
    unittest.main() 