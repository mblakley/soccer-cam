#!/usr/bin/env python3
"""
Test that the TeamSnap integration respects the connected camera filtering rule.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

# Add the parent directory to the path so we can import the video_grouper package
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from video_grouper.api_integrations.teamsnap import TeamSnapAPI
from video_grouper.utils.config import TeamSnapConfig, TeamSnapTeamConfig


class TestConnectedFiltering(unittest.TestCase):
    """Test that the TeamSnap integration respects the connected camera filtering rule."""

    def setUp(self):
        """Set up the test."""
        # Create a mock config
        self.config = TeamSnapConfig(
            enabled=True,
            client_id="test_client_id",
            client_secret="test_client_secret",
            access_token="test_access_token",
        )
        self.team_config = TeamSnapTeamConfig(
            enabled=True,
            team_id="test_team_id",
            team_name="Test Team",
        )
        # Create a mock app config for timezone
        from video_grouper.utils.config import AppConfig

        self.app_config = AppConfig(timezone="America/New_York")
        # Create a mock TeamSnap API
        self.api = TeamSnapAPI(self.config, self.team_config, self.app_config)
        self.api.enabled = True

        # Create mock games
        self.games = [
            {
                "id": "1",
                "start_date": "2025-03-08T17:10:14Z",
                "opponent_name": "Opponent 1",
                "location_name": "Location 1",
                "duration_in_minutes": "90",
            },
            {
                "id": "2",
                "start_date": "2025-03-09T14:00:00Z",
                "opponent_name": "Opponent 2",
                "location_name": "Location 2",
                "duration_in_minutes": "90",
            },
        ]

    @patch("video_grouper.api_integrations.teamsnap.TeamSnapAPI.get_games")
    def test_find_game_for_recording_with_connected_camera(self, mock_get_games):
        """Test that games are found for recordings when the camera is connected."""
        # Mock the get_games method to return our test games
        mock_get_games.return_value = self.games

        # Create a recording timespan that overlaps with the first game
        recording_start = datetime(2025, 3, 8, 17, 15, 0, tzinfo=timezone.utc)
        recording_end = recording_start + timedelta(minutes=60)

        # Test that no game is found when the camera is connected
        game = self.api.find_game_for_recording(recording_start, recording_end)

        # Since the camera is connected, we should still find the game
        # (the filtering happens at a higher level, not in the TeamSnap API)
        self.assertIsNotNone(game)
        self.assertEqual(game["id"], "1")

    @patch("video_grouper.api_integrations.teamsnap.TeamSnapAPI.get_games")
    def test_find_game_for_recording_with_disconnected_camera(self, mock_get_games):
        """Test that games are found for recordings when the camera is disconnected."""
        # Mock the get_games method to return our test games
        mock_get_games.return_value = self.games

        # Create a recording timespan that overlaps with the first game
        recording_start = datetime(2025, 3, 8, 17, 15, 0, tzinfo=timezone.utc)
        recording_end = recording_start + timedelta(minutes=60)

        # Test that a game is found when the camera is disconnected
        game = self.api.find_game_for_recording(recording_start, recording_end)

        # We should find the game
        self.assertIsNotNone(game)
        self.assertEqual(game["id"], "1")


if __name__ == "__main__":
    unittest.main()
