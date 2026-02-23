"""
Mock TeamSnap API integration for end-to-end testing.

This module provides a mock implementation of the TeamSnap API that returns
realistic test data for comprehensive end-to-end testing scenarios.

Two pre-generated games are created at init, timed to align with the simulator's
2 groups of 3 files. The proximity guard + midpoint heuristic ensures each
recording group is assigned the correct game:
  - Group 1 (0:00-3:00 from base_time) -> Hawks vs Eagles
  - Group 2 (3:10-6:10 from base_time) -> Hawks vs Falcons
"""

import logging
from datetime import datetime, timedelta, timezone
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
    - Pre-generates 2 games aligned with the simulator's 2 recording groups
    - Uses the same proximity guard + midpoint heuristic as the real API
    - Assigns different opponents to each group for validation
    """

    def __init__(self, client_id: str, client_secret: str, access_token: str):
        """Initialize the mock TeamSnap API with 2 pre-generated games."""
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.enabled = True

        # Pre-generate games aligned with simulator's base_time = now(UTC) - 12h.
        # Simulator files:
        #   Group 1: base_time + 0:00 to base_time + 3:00 (3 x 1-min files)
        #   Group 2: base_time + 3:10 to base_time + 6:10 (3 x 1-min files, after 10s gap)
        import pytz

        base_time = datetime.now(pytz.utc) - timedelta(hours=12)

        # Game 1: Hawks vs Eagles - overlaps with Group 1
        # Starts 30s before base_time, ends 3min30s after -> midpoint at ~1min30s
        game1_start = base_time - timedelta(seconds=30)
        game1_end = base_time + timedelta(minutes=3, seconds=30)

        # Game 2: Hawks vs Falcons - overlaps with Group 2
        # Starts at base_time+2min30s, ends at base_time+6min40s -> midpoint at ~4min35s
        game2_start = base_time + timedelta(minutes=2, seconds=30)
        game2_end = base_time + timedelta(minutes=6, seconds=40)

        self._games = [
            MockTeamSnapEvent(
                id="mock_game_001",
                name="Hawks vs Eagles",
                description="League Game 1",
                start_date=game1_start.isoformat(),
                end_date=game1_end.isoformat(),
                duration_in_minutes=int((game1_end - game1_start).total_seconds() / 60),
                event_type="game",
                is_game=True,
                location_name="Central Park Soccer Fields",
                location_address="Central Park, New York, NY",
                team_id="team_hawks_456",
                team_name="Hawks",
                custom_fields={
                    "opponent_name": "Eagles",
                    "field_number": "Field 3",
                    "source": "TeamSnap",
                },
            ),
            MockTeamSnapEvent(
                id="mock_game_002",
                name="Hawks vs Falcons",
                description="League Game 2",
                start_date=game2_start.isoformat(),
                end_date=game2_end.isoformat(),
                duration_in_minutes=int((game2_end - game2_start).total_seconds() / 60),
                event_type="game",
                is_game=True,
                location_name="Riverside Soccer Fields",
                location_address="Riverside Park, New York, NY",
                team_id="team_hawks_456",
                team_name="Hawks",
                custom_fields={
                    "opponent_name": "Falcons",
                    "field_number": "Field 1",
                    "source": "TeamSnap",
                },
            ),
        ]

        logger.info(
            f"Mock TeamSnap API initialized with {len(self._games)} pre-generated games"
        )
        for g in self._games:
            logger.info(
                f"  Game: {g['name']} from {g['start_date']} to {g['end_date']}"
            )

    def find_game_for_recording(
        self, recording_start: datetime, recording_end: datetime
    ) -> Optional[MockTeamSnapEvent]:
        """Find the best game for the recording using shared selection logic."""
        logger.info(
            f"Mock TeamSnap: Looking for games between {recording_start} and {recording_end}"
        )

        # Ensure recording times are timezone-aware (UTC)
        if recording_start.tzinfo is None:
            recording_start = recording_start.replace(tzinfo=timezone.utc)
        if recording_end.tzinfo is None:
            recording_end = recording_end.replace(tzinfo=timezone.utc)

        # Parse pre-generated games into (game, start_utc, end_utc) tuples
        candidates = []
        for game in self._games:
            game_start = datetime.fromisoformat(game["start_date"])
            game_end = datetime.fromisoformat(game["end_date"])

            if game_start.tzinfo is None:
                game_start = game_start.replace(tzinfo=timezone.utc)
            if game_end.tzinfo is None:
                game_end = game_end.replace(tzinfo=timezone.utc)

            candidates.append((game, game_start, game_end))

        from video_grouper.utils.game_selection import select_best_game

        return select_best_game(
            candidates,
            recording_start,
            recording_end,
            game_label_fn=lambda g: g.get("name", "Unknown"),
        )

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
        return list(self._games)

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
