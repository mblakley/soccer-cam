"""
Mock PlayMetrics API integration for end-to-end testing.

This module provides a mock implementation of the PlayMetrics API that returns
realistic test data for comprehensive end-to-end testing scenarios.
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Union

logger = logging.getLogger(__name__)


class MockPlayMetricsAPI:
    """
    Mock PlayMetrics API that provides realistic test data for end-to-end testing.

    This mock:
    - Returns a scheduled match that overlaps with test recording times
    - Provides realistic team names, locations, and match details
    - Simulates API response delays and login processes
    """

    def __init__(self, username: str, password: str):
        """Initialize the mock PlayMetrics API."""
        self.username = username
        self.password = password
        self.session = None
        self.logged_in = False

        logger.info("Mock PlayMetrics API initialized")

    async def login(self) -> bool:
        """Mock login to PlayMetrics."""
        logger.info("Mock PlayMetrics: Performing login")
        # Simulate login delay
        import asyncio

        await asyncio.sleep(0.5)

        self.logged_in = True
        logger.info("Mock PlayMetrics: Login successful")
        return True

    def find_game_for_recording(
        self, recording_start: datetime, recording_end: datetime
    ) -> Optional[Dict[str, Union[str, datetime]]]:
        """Find a game for the recording timeframe."""
        if not self.logged_in:
            logger.warning("Mock PlayMetrics: Not logged in, cannot find games")
            return None

        logger.info(
            f"Mock PlayMetrics: Looking for games between {recording_start} and {recording_end}"
        )

        # Create a game that overlaps with the recording time
        # Game starts 5 minutes after recording starts and ends 5 minutes before recording ends
        game_start = recording_start + timedelta(minutes=5)
        game_end = recording_end - timedelta(minutes=5)

        # Ensure game duration is reasonable (at least 30 minutes)
        if (game_end - game_start).total_seconds() < 1800:  # 30 minutes
            # If the recording is too short, create a game that fits within it
            game_start = recording_start + timedelta(minutes=2)
            game_end = recording_end - timedelta(minutes=2)

        mock_game = {
            "id": "pm_game_789",
            "team_name": "Lightning",
            "opponent": "Thunder",
            "location": "Sports Complex Field A",
            "start_time": game_start,
            "end_time": game_end,
            "competition": "Spring League",
            "division": "U15",
            "game_type": "Regular Season",
            "home_team": "Lightning",
            "away_team": "Thunder",
            "source": "PlayMetrics",
        }

        logger.info(
            f"Mock PlayMetrics: Found game '{mock_game['team_name']} vs {mock_game['opponent']}' from {game_start} to {game_end}"
        )
        return mock_game

    def get_team_schedule(
        self, team_id: str, start_date: datetime, end_date: datetime
    ) -> List[Dict[str, Union[str, datetime]]]:
        """Get team schedule within a date range."""
        # For testing, just return our mock game if it's within the range
        mock_game = self.find_game_for_recording(start_date, end_date)
        return [mock_game] if mock_game else []

    def get_teams(self) -> List[Dict[str, Union[str, int]]]:
        """Get list of teams for the authenticated user."""
        return [
            {
                "id": "team_lightning_789",
                "name": "Lightning",
                "division": "U15",
                "competition": "Spring League",
            }
        ]

    def get_team_info(self, team_id: str) -> Dict[str, Union[str, int]]:
        """Get information about a specific team."""
        return {
            "id": team_id,
            "name": "Lightning",
            "division": "U15",
            "competition": "Spring League",
            "season": "Spring 2024",
        }


# Mock service class that matches the interface expected by the system
class MockPlayMetricsService:
    """Mock PlayMetrics service that integrates with the video grouper system."""

    def __init__(self, config):
        """Initialize the mock service."""
        self.config = config
        # Disable PlayMetrics for E2E testing to simplify - only TeamSnap will provide matches
        self.enabled = False

        if self.enabled:
            self.api = MockPlayMetricsAPI(
                username=getattr(config, "username", "mock@example.com"),
                password=getattr(config, "password", "mock_password"),
            )
        else:
            self.api = None

        logger.info(f"Mock PlayMetrics service initialized (enabled: {self.enabled})")

    async def initialize(self) -> bool:
        """Initialize the service (perform login)."""
        if not self.enabled or not self.api:
            return False

        return await self.api.login()

    def find_game_for_recording(
        self, recording_start: datetime, recording_end: datetime
    ) -> Optional[Dict[str, Union[str, datetime]]]:
        """Find a game for the recording timeframe."""
        if not self.enabled or not self.api:
            return None

        game = self.api.find_game_for_recording(recording_start, recording_end)
        if not game:
            return None

        # The mock already returns data in the expected format
        return game

    def get_team_name(self) -> str:
        """Get the configured team name."""
        return getattr(self.config, "team_name", "Lightning")

    def get_team_names(self) -> List[str]:
        """Get list of team names."""
        teams = getattr(self.config, "teams", [])
        if teams:
            return [
                team.get("team_name", "") for team in teams if team.get("enabled", True)
            ]

        # Fallback to single team configuration
        team_name = self.get_team_name()
        return [team_name] if team_name else []

    def close(self):
        """Close the service and clean up resources."""
        logger.info("Mock PlayMetrics service closed")
        # No cleanup needed for mock service
