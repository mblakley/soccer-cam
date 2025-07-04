#!/usr/bin/env python3
"""
Tests for the TeamSnap API integration.
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


class TestTeamSnapAPI(unittest.TestCase):
    """Test the TeamSnap API."""

    def setUp(self):
        """Set up the test."""
        # Create test config data
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
        self.api = TeamSnapAPI(self.config, self.team_config, self.app_config)

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

    def tearDown(self):
        """Clean up after the test."""
        # No file cleanup needed since we're using mocks
        pass

    def test_initialization(self):
        """Test that the TeamSnap API initializes correctly."""
        self.assertTrue(self.api.enabled)
        self.assertEqual(self.api.team_id, "test_team_id")
        self.assertEqual(self.api.team_name, "Test Team")

    @patch("video_grouper.api_integrations.teamsnap.TeamSnapAPI._make_api_request")
    def test_discover_api_endpoints(self, mock_make_api_request):
        """Test that the API endpoints are discovered correctly."""
        # Mock the API response
        mock_make_api_request.return_value = {
            "collection": {
                "links": [
                    {"rel": "events", "href": "https://api.teamsnap.com/v3/events"},
                    {"rel": "teams", "href": "https://api.teamsnap.com/v3/teams"},
                ]
            }
        }

        # Call the method
        self.api._discover_api_endpoints()

        # Check that the endpoints were discovered
        self.assertEqual(
            self.api.endpoints["events"], "https://api.teamsnap.com/v3/events"
        )
        self.assertEqual(
            self.api.endpoints["teams"], "https://api.teamsnap.com/v3/teams"
        )

    @patch("video_grouper.api_integrations.teamsnap.TeamSnapAPI._make_api_request")
    def test_get_team_events(self, mock_make_api_request):
        """Test that team events are retrieved correctly."""
        # Mock the API response
        mock_make_api_request.return_value = {
            "collection": {
                "items": [
                    {
                        "data": [
                            {"name": "id", "value": "1"},
                            {"name": "start_date", "value": "2025-03-08T17:10:14Z"},
                            {"name": "opponent_name", "value": "Opponent 1"},
                            {"name": "location_name", "value": "Location 1"},
                        ]
                    },
                    {
                        "data": [
                            {"name": "id", "value": "2"},
                            {"name": "start_date", "value": "2025-03-09T14:00:00Z"},
                            {"name": "opponent_name", "value": "Opponent 2"},
                            {"name": "location_name", "value": "Location 2"},
                        ]
                    },
                ]
            }
        }

        # Set up the endpoints
        self.api.endpoints = {"events": "https://api.teamsnap.com/v3/events"}

        # Call the method
        events = self.api.get_team_events()

        # Check that the events were retrieved
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["id"], "1")
        self.assertEqual(events[0]["start_date"], "2025-03-08T17:10:14Z")
        self.assertEqual(events[0]["opponent_name"], "Opponent 1")
        self.assertEqual(events[0]["location_name"], "Location 1")

    @patch("video_grouper.api_integrations.teamsnap.TeamSnapAPI.get_team_events")
    def test_get_games(self, mock_get_team_events):
        """Test that games are filtered correctly."""
        # Mock the get_team_events method
        mock_get_team_events.return_value = [
            {
                "id": "1",
                "start_date": "2025-03-08T17:10:14Z",
                "opponent_name": "Opponent 1",
                "location_name": "Location 1",
                "event_type": "game",
            },
            {
                "id": "2",
                "start_date": "2025-03-09T14:00:00Z",
                "opponent_name": "Opponent 2",
                "location_name": "Location 2",
                "event_type": "practice",
            },
            {
                "id": "3",
                "start_date": "2025-03-10T15:00:00Z",
                "opponent_name": "Opponent 3",
                "location_name": "Location 3",
                "event_type": "game",
            },
        ]

        # Call the method
        games = self.api.get_games()

        # Check that only games were returned
        self.assertEqual(len(games), 2)
        self.assertEqual(games[0]["id"], "1")
        self.assertEqual(games[1]["id"], "3")

    @patch("video_grouper.api_integrations.teamsnap.TeamSnapAPI.get_games")
    def test_find_game_for_recording(self, mock_get_games):
        """Test that games are found correctly for a recording timespan."""
        # Mock the get_games method
        mock_get_games.return_value = self.games

        # Test case 1: Recording overlaps with a game
        recording_start = datetime(2025, 3, 8, 17, 15, 0, tzinfo=timezone.utc)
        recording_end = recording_start + timedelta(minutes=60)

        game = self.api.find_game_for_recording(recording_start, recording_end)

        self.assertIsNotNone(game)
        self.assertEqual(game["id"], "1")

        # Test case 2: Recording does not overlap with any game
        recording_start = datetime(2025, 3, 10, 17, 15, 0, tzinfo=timezone.utc)
        recording_end = recording_start + timedelta(minutes=60)

        game = self.api.find_game_for_recording(recording_start, recording_end)

        self.assertIsNone(game)

    @patch(
        "video_grouper.api_integrations.teamsnap.TeamSnapAPI.find_game_for_recording"
    )
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
        success = self.api.populate_match_info(
            match_info, recording_start, recording_end
        )

        # Check that the match info was populated
        self.assertTrue(success)
        self.assertEqual(match_info["home_team"], "Test Team")
        self.assertEqual(match_info["away_team"], "Opponent 1")
        self.assertEqual(match_info["location"], "Location 1")
        self.assertEqual(match_info["date"], "2025-03-08")
        # The time should be converted to local timezone (America/New_York)
        # 17:10 UTC = 12:10 EST (UTC-5)
        self.assertEqual(match_info["time"], "12:10")

        # Test case 2: No game found
        mock_find_game.return_value = None

        # Create an empty match info dictionary
        match_info = {}

        # Call the method
        success = self.api.populate_match_info(
            match_info, recording_start, recording_end
        )

        # Check that the match info was not populated
        self.assertFalse(success)
        self.assertEqual(match_info, {})

    @patch("video_grouper.api_integrations.teamsnap.requests.get")
    def test_token_valid(self, mock_get):
        """Test that a valid token is accepted and not refreshed."""
        mock_get.return_value.status_code = 200
        self.api.access_token = "valid_token"
        self.assertTrue(self.api._ensure_valid_token())
        mock_get.assert_called_once()

    @patch("video_grouper.api_integrations.teamsnap.requests.get")
    @patch.object(TeamSnapAPI, "get_access_token")
    def test_token_invalid_refresh_success(self, mock_get_access_token, mock_get):
        """Test that an invalid token triggers a refresh and succeeds."""
        # First call: token invalid (401), then refresh returns True
        mock_get.return_value.status_code = 401
        mock_get_access_token.return_value = True
        self.api.access_token = "expired_token"
        self.assertTrue(self.api._ensure_valid_token())
        self.assertEqual(mock_get.call_count, 1)
        mock_get_access_token.assert_called_once()

    @patch("video_grouper.api_integrations.teamsnap.requests.get")
    @patch.object(TeamSnapAPI, "get_access_token")
    def test_token_invalid_refresh_fail(self, mock_get_access_token, mock_get):
        """Test that an invalid token triggers a refresh and fails if refresh fails."""
        mock_get.return_value.status_code = 401
        mock_get_access_token.return_value = False
        self.api.access_token = "expired_token"
        self.assertFalse(self.api._ensure_valid_token())
        self.assertEqual(mock_get.call_count, 1)
        mock_get_access_token.assert_called_once()

    def test_update_config_token(self):
        """Test that _update_config_token updates the config object and calls save if available."""

        class DummyConfig:
            def __init__(self):
                self.enabled = True
                self.client_id = "test_id"
                self.client_secret = "test_secret"
                self.access_token = None
                self.team_id = "test_team"
                self.team_name = "Test Team"
                self.save_called = False

            def save(self):
                self.save_called = True

        dummy = DummyConfig()
        api = TeamSnapAPI(dummy, self.team_config, self.app_config)
        api.access_token = "new_token"
        api._update_config_token()
        self.assertEqual(dummy.access_token, "new_token")
        self.assertTrue(dummy.save_called)

    @patch.object(TeamSnapAPI, "_ensure_valid_token", return_value=True)
    @patch.object(TeamSnapAPI, "get_teams", return_value=[{"id": "1", "name": "Team1"}])
    def test_test_connection_success(self, mock_get_teams, mock_ensure):
        """Test test_connection returns True when teams are fetched."""
        self.assertTrue(self.api.test_connection())
        mock_ensure.assert_called_once()
        mock_get_teams.assert_called_once()

    @patch.object(TeamSnapAPI, "_ensure_valid_token", return_value=False)
    def test_test_connection_fail(self, mock_ensure):
        """Test test_connection returns False if token is not valid."""
        self.assertFalse(self.api.test_connection())
        mock_ensure.assert_called_once()


if __name__ == "__main__":
    unittest.main()
