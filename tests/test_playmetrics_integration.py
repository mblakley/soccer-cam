import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from video_grouper.api_integrations.playmetrics import PlayMetricsAPI
from video_grouper.utils.config import PlayMetricsConfig


@pytest.fixture
def mock_config():
    """Create a mock configuration for testing."""
    return PlayMetricsConfig(
        enabled=True,
        username="test@example.com",
        password="password",
        team_id="12345",
        team_name="Test Team",
    )


@pytest.fixture
def playmetrics_api(mock_config):
    """Create a PlayMetricsAPI instance with mock configuration."""
    from video_grouper.utils.config import AppConfig

    app_config = AppConfig(timezone="America/New_York")
    return PlayMetricsAPI(mock_config, app_config)


@pytest.mark.asyncio
async def test_playmetrics_initialization(playmetrics_api):
    """Test that the PlayMetrics API initializes correctly."""
    assert playmetrics_api.enabled is True
    assert playmetrics_api.username == "test@example.com"
    assert playmetrics_api.password == "password"
    assert playmetrics_api.team_id == "12345"
    assert playmetrics_api.team_name == "Test Team"


@pytest.mark.asyncio
async def test_playmetrics_disabled():
    """Test that the PlayMetrics API is disabled when not configured."""
    disabled_config = PlayMetricsConfig(enabled=False)
    from video_grouper.utils.config import AppConfig

    app_config = AppConfig(timezone="America/New_York")
    api = PlayMetricsAPI(disabled_config, app_config)
    assert api.enabled is False


@pytest.mark.asyncio
async def test_get_team_events(mock_config):
    """Test getting team events from PlayMetrics."""
    from video_grouper.utils.config import AppConfig

    app_config = AppConfig(timezone="America/New_York")
    api = PlayMetricsAPI(mock_config, app_config)

    # Create mock event data
    game_time = datetime(2025, 6, 22, 14, 0, 0, tzinfo=timezone.utc)
    mock_events = [
        {
            "id": "1",
            "title": "Game vs Test Opponent",
            "description": "Game description",
            "location": "Test Field",
            "start_time": game_time,
            "end_time": game_time + timedelta(hours=2),
            "is_game": True,
            "opponent": "Test Opponent",
        }
    ]

    # Mock the get_events method
    api.get_events = MagicMock(return_value=mock_events)

    # Call the get_games method
    games = api.get_games()

    # Check the results
    assert len(games) == 1
    assert games[0]["title"] == "Game vs Test Opponent"
    assert games[0]["location"] == "Test Field"
    assert games[0]["is_game"] is True
    assert games[0]["opponent"] == "Test Opponent"


@pytest.mark.asyncio
async def test_find_game_for_recording(mock_config):
    """Test finding a game for a recording timespan."""
    from video_grouper.utils.config import AppConfig

    app_config = AppConfig(timezone="America/New_York")
    api = PlayMetricsAPI(mock_config, app_config)

    # Create mock event data
    game_time = datetime(2025, 6, 22, 14, 0, 0, tzinfo=timezone.utc)
    mock_events = [
        {
            "id": "1",
            "title": "Game vs Test Opponent",
            "description": "Game description",
            "location": "Test Field",
            "start_time": game_time,
            "end_time": game_time + timedelta(hours=2),
            "is_game": True,
            "opponent": "Test Opponent",
        },
        {
            "id": "2",
            "title": "Practice",
            "description": "Practice description",
            "location": "Training Ground",
            "start_time": game_time + timedelta(days=1),
            "end_time": game_time + timedelta(days=1, hours=2),
            "is_game": False,
            "opponent": None,
        },
    ]

    # Mock the get_games method
    api.get_games = MagicMock(return_value=mock_events)

    # Test finding a game that overlaps with the recording timespan
    recording_start = game_time - timedelta(minutes=30)
    recording_end = game_time + timedelta(hours=1)

    game = api.find_game_for_recording(recording_start, recording_end)

    # Check the results
    assert game is not None
    assert game["id"] == "1"
    assert game["title"] == "Game vs Test Opponent"

    # Test finding a game that doesn't match the recording timespan
    recording_start = game_time + timedelta(days=2)
    recording_end = recording_start + timedelta(hours=1)

    game = api.find_game_for_recording(recording_start, recording_end)

    # Check that no game was found
    assert game is None


@pytest.mark.asyncio
async def test_populate_match_info(mock_config):
    """Test populating match info with PlayMetrics data."""
    from video_grouper.utils.config import AppConfig

    app_config = AppConfig(timezone="America/New_York")
    api = PlayMetricsAPI(mock_config, app_config)

    # Create mock game data
    game_time = datetime(2025, 6, 22, 14, 0, 0, tzinfo=timezone.utc)
    mock_game = {
        "id": "1",
        "title": "Game vs Test Opponent",
        "description": "Game description",
        "location": "Test Field",
        "start_time": game_time,
        "end_time": game_time + timedelta(hours=2),
        "is_game": True,
        "opponent": "Test Opponent",
    }

    # Mock the find_game_for_recording method
    api.find_game_for_recording = MagicMock(return_value=mock_game)

    # Create a match info dictionary to populate
    match_info = {}

    # Call the populate_match_info method
    recording_start = game_time - timedelta(minutes=30)
    recording_end = game_time + timedelta(hours=1)
    result = api.populate_match_info(match_info, recording_start, recording_end)

    # Check the results
    assert result is True
    assert match_info["title"] == "Game vs Test Opponent"
    assert match_info["opponent"] == "Test Opponent"
    assert match_info["location"] == "Test Field"
    assert match_info["date"] == "2025-06-22"
    assert match_info["time"] == "14:00"
    assert match_info["description"] == "Game description"
