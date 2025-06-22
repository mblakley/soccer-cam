#!/usr/bin/env python3
"""
Test script for the TeamSnap API integration.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
import json
from video_grouper.api_integrations.teamsnap import TeamSnapAPI

class TestTeamSnapIntegration(unittest.TestCase):
    """Integration tests for the TeamSnap API."""
    
    def setUp(self):
        """Set up the test."""
        self.api = TeamSnapAPI()
        
        # Skip tests if TeamSnap API is not enabled
        if not self.api.enabled:
            self.skipTest("TeamSnap API is not enabled. Please check your configuration.")
    
    def test_get_events(self):
        """Test fetching team events."""
        # Get all events
        events = self.api.get_team_events()
        self.assertIsNotNone(events)
        
        # Get games only
        games = self.api.get_games()
        self.assertIsNotNone(games)
        
        # Verify games are a subset of events
        game_ids = set(game.get('id') for game in games if game.get('id'))
        event_ids = set(event.get('id') for event in events if event.get('id'))
        self.assertTrue(game_ids.issubset(event_ids))
    
    def test_find_game_for_recording(self):
        """Test finding a game for a recording timespan."""
        # Get all games
        games = self.api.get_games()
        
        if not games:
            self.skipTest("No games found")
        
        # Use the first game as a test case
        test_game = games[0]
        
        # Parse the game start time
        game_start_str = test_game.get('start_date')
        if not game_start_str:
            self.skipTest("Game has no start date")
        
        # Parse the game date
        game_start = datetime.fromisoformat(game_start_str.replace('Z', '+00:00'))
        
        # Create a recording timespan that overlaps with the game
        recording_start = game_start + timedelta(minutes=15)  # 15 minutes after game starts
        recording_end = recording_start + timedelta(minutes=60)  # Record for 60 minutes
        
        # Look up the game
        found_game = self.api.find_game_for_recording(recording_start, recording_end)
        
        self.assertIsNotNone(found_game)
        self.assertEqual(found_game.get('id'), test_game.get('id'))
    
    def test_populate_match_info(self):
        """Test populating match info for a recording."""
        # Get all games
        games = self.api.get_games()
        
        if not games:
            self.skipTest("No games found")
        
        # Use the first game as a test case
        test_game = games[0]
        
        # Parse the game start time
        game_start_str = test_game.get('start_date')
        if not game_start_str:
            self.skipTest("Game has no start date")
        
        # Parse the game date
        game_start = datetime.fromisoformat(game_start_str.replace('Z', '+00:00'))
        
        # Create a recording timespan that overlaps with the game
        recording_start = game_start + timedelta(minutes=15)  # 15 minutes after game starts
        recording_end = recording_start + timedelta(minutes=60)  # Record for 60 minutes
        
        # Create an empty match info dictionary
        match_info = {}
        
        # Populate match info
        success = self.api.populate_match_info(match_info, recording_start, recording_end)
        
        self.assertTrue(success)
        self.assertIn('home_team', match_info)
        self.assertIn('away_team', match_info)
        self.assertIn('location', match_info)
        self.assertIn('date', match_info)
        self.assertIn('time', match_info)

if __name__ == '__main__':
    unittest.main() 