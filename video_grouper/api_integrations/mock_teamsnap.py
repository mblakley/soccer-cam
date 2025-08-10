"""
Mock TeamSnap API integration for end-to-end testing.

This module provides a mock implementation of the TeamSnap API that returns
realistic test data for comprehensive end-to-end testing scenarios.
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, TypedDict, Union

logger = logging.getLogger(__name__)


class MockTeamSnapEvent(TypedDict, total=False):
    """Represents a mock event from TeamSnap."""

    # Basic event info
    id: str
    name: str
    description: str
    start_date: str
    end_date: str
    duration_in_minutes: int

    # Event type
    event_type: str  # "game", "practice", "meeting", etc.
    is_game: bool

    # Location
    location_name: str
    location_address: str

    # Team info
    team_id: str
    team_name: str

    # Custom fields
    custom_fields: dict[str, Union[str, int, float, bool]]


class MockTeamSnapAPI:
    """
    Mock TeamSnap API that provides realistic test data for end-to-end testing.

    This mock:
    - Returns a scheduled match that overlaps with test recording times
    - Provides realistic team names, locations, and match details
    - Simulates API response delays and occasional failures
    """

    def __init__(self, client_id: str, client_secret: str, access_token: str):
        """Initialize the mock TeamSnap API."""
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.enabled = True

        logger.info("Mock TeamSnap API initialized")

    def find_game_for_recording(
        self, recording_start: datetime, recording_end: datetime
    ) -> Optional[MockTeamSnapEvent]:
        """Find a game for the recording timeframe."""
        logger.info(
            f"Mock TeamSnap: Looking for games between {recording_start} and {recording_end}"
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

        mock_game = MockTeamSnapEvent(
            id="mock_game_123",
            name="Hawks vs Eagles",
            description="League Championship Game",
            start_date=game_start.isoformat(),
            end_date=game_end.isoformat(),
            duration_in_minutes=int((game_end - game_start).total_seconds() / 60),
            event_type="game",
            is_game=True,
            location_name="Central Park Soccer Fields",
            location_address="Central Park, New York, NY",
            team_id="team_hawks_456",
            team_name="Hawks",
            custom_fields={
                "opponent_name": "Eagles",
                "field_number": "Field 3",
                "referee": "John Smith",
                "weather": "Sunny",
                "source": "TeamSnap",
            },
        )

        logger.info(
            f"Mock TeamSnap: Found game '{mock_game['name']}' from {game_start} to {game_end}"
        )
        return mock_game

    def get_teams(self) -> List[Dict[str, Union[str, int]]]:
        """Get list of teams for the authenticated user."""
        return [
            {
                "id": "team_hawks_456",
                "name": "Hawks",
                "location": "New York",
                "sport": "Soccer",
                "division": "U14",
            }
        ]

    def get_events_for_team(
        self, team_id: str, start_date: datetime, end_date: datetime
    ) -> List[MockTeamSnapEvent]:
        """Get events for a specific team within a date range."""
        # For testing, just return our mock game if it's within the range
        mock_game = self.find_game_for_recording(start_date, end_date)
        return [mock_game] if mock_game else []

    def get_team_info(self, team_id: str) -> Dict[str, Union[str, int]]:
        """Get information about a specific team."""
        return {
            "id": team_id,
            "name": "Hawks",
            "location": "New York",
            "sport": "Soccer",
            "division": "U14",
            "season": "Fall 2024",
        }


# Mock service class that matches the interface expected by the system
class MockTeamSnapService:
    """Mock TeamSnap service that integrates with the video grouper system."""

    def __init__(self, config):
        """Initialize the mock service."""
        self.config = config
        # Check if the main config is enabled and has at least one enabled team
        self.enabled = (
            getattr(config, "enabled", True) and len(getattr(config, "teams", [])) > 0
        )

        if self.enabled:
            # Get the first enabled team or create a default one
            teams = getattr(config, "teams", [])
            if teams:
                # Use the first team's configuration
                team_config = teams[0]
                team_name = getattr(team_config, "team_name", "Hawks")
            else:
                # Create a default team configuration
                team_name = getattr(config, "my_team_name", "Hawks")

            self.api = MockTeamSnapAPI(
                client_id=getattr(config, "client_id", "mock_client_id"),
                client_secret=getattr(config, "client_secret", "mock_client_secret"),
                access_token=getattr(config, "access_token", "mock_access_token"),
            )
            self.team_name = team_name
        else:
            self.api = None
            self.team_name = None

        logger.info(
            f"Mock TeamSnap service initialized (enabled: {self.enabled}, team: {self.team_name})"
        )

    def find_game_for_recording(
        self, recording_start: datetime, recording_end: datetime
    ) -> Optional[Dict[str, Union[str, int, bool]]]:
        """Find a game for the recording timeframe."""
        if not self.enabled or not self.api:
            return None

        game = self.api.find_game_for_recording(recording_start, recording_end)
        if not game:
            return None

        # Convert to the format expected by the system
        return {
            "source": "TeamSnap",
            "team_name": game.get("team_name", ""),
            "opponent_name": game.get("custom_fields", {}).get("opponent_name", ""),
            "location_name": game.get("location_name", ""),
            "start_time": datetime.fromisoformat(game["start_date"]),
            "end_time": datetime.fromisoformat(game["end_date"]),
            "event_type": game.get("event_type", "game"),
            "description": game.get("description", ""),
        }

    def get_team_name(self) -> str:
        """Get the configured team name."""
        if self.team_name:
            return self.team_name
        return getattr(self.config, "my_team_name", "Hawks")
