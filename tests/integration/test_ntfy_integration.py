import pytest
from unittest.mock import patch, AsyncMock
from video_grouper.api_integrations.ntfy import NtfyAPI
from video_grouper.utils.config import NtfyConfig


@pytest.fixture
def ntfy_config():
    """Create a mock NtfyConfig for testing."""
    return NtfyConfig(
        enabled=True, server_url="https://ntfy.sh", topic="test-soccer-cam"
    )


@pytest.fixture
def ntfy_api(ntfy_config):
    """Create a NtfyAPI instance with mock configuration."""
    return NtfyAPI(ntfy_config)


@pytest.fixture
def mock_http_client():
    """Create a mock HTTP client for testing."""
    mock_client = AsyncMock()
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_client.post.return_value = mock_response
    mock_client.put.return_value = mock_response
    mock_client.get.return_value = mock_response

    mock_async_client = AsyncMock()
    mock_async_client.__aenter__.return_value = mock_client
    mock_async_client.__aexit__.return_value = None

    return mock_async_client, mock_client


@pytest.mark.asyncio
async def test_ntfy_initialization(ntfy_api):
    """Test that the NTFY API initializes correctly."""
    assert ntfy_api.enabled is True
    assert ntfy_api.base_url == "https://ntfy.sh"
    assert ntfy_api.topic == "test-soccer-cam"


@pytest.mark.asyncio
async def test_ntfy_disabled():
    """Test that the NTFY API is disabled when not configured."""
    config = NtfyConfig(enabled=False, server_url="https://ntfy.sh", topic="test-topic")
    ntfy_api = NtfyAPI(config)
    assert ntfy_api.enabled is False


@pytest.mark.asyncio
async def test_ntfy_auto_topic():
    """Test that a random topic is generated when not provided."""
    config = NtfyConfig(enabled=True, server_url="https://ntfy.sh", topic=None)

    ntfy_api = NtfyAPI(config)
    assert ntfy_api.enabled is True
    assert ntfy_api.topic is not None
    assert ntfy_api.topic.startswith("soccer-cam-")


@pytest.mark.asyncio
async def test_send_notification(mock_http_client):
    """Test sending a notification."""
    mock_async_client, mock_client = mock_http_client

    config = NtfyConfig(enabled=True, topic="test-topic", server_url="https://ntfy.sh")

    ntfy_api = NtfyAPI(config)

    with patch("httpx.AsyncClient", return_value=mock_async_client):
        result = await ntfy_api.send_notification(
            message="Test message", title="Test Title", tags=["test", "notification"]
        )

        assert result is True
        mock_client.post.assert_called_once()
        args, kwargs = mock_client.post.call_args
        assert args[0] == "https://ntfy.sh"
        assert kwargs["json"]["topic"] == "test-topic"
        assert kwargs["json"]["message"] == "Test message"
        assert kwargs["json"]["title"] == "Test Title"
        assert kwargs["json"]["tags"] == ["test", "notification"]


@pytest.mark.asyncio
async def test_listen_for_responses(ntfy_api):
    """Test the response queue functionality."""
    await ntfy_api.response_queue.put(
        {"id": "456", "time": 1642307389, "message": "yes", "is_affirmative": True}
    )

    assert ntfy_api.response_queue.qsize() == 1
    response = await ntfy_api.response_queue.get()
    assert response["id"] == "456"
    assert response["message"] == "yes"
    assert response["is_affirmative"] is True


@pytest.mark.asyncio
async def test_ask_game_start_time(ntfy_api):
    """Test sending notification about game start time."""

    async def mock_get_duration(*args, **kwargs):
        return "01:30:00"

    with (
        patch.object(ntfy_api, "send_notification", AsyncMock(return_value=True)),
        patch(
            "video_grouper.api_integrations.ntfy.get_video_duration", mock_get_duration
        ),
        patch(
            "video_grouper.api_integrations.ntfy.create_screenshot",
            AsyncMock(return_value=True),
        ),
        patch(
            "video_grouper.api_integrations.ntfy.compress_image",
            AsyncMock(return_value="test_screenshot.jpg"),
        ),
        patch("video_grouper.api_integrations.ntfy.os.path.exists", return_value=True),
        patch("video_grouper.api_integrations.ntfy.os.remove"),
    ):
        result = await ntfy_api.ask_game_start_time(
            combined_video_path="test_video.mp4", group_dir="test_dir"
        )

        assert (
            result is None
        )  # This method just sends notification, doesn't return data

        ntfy_api.send_notification.assert_called_once()
        args, kwargs = ntfy_api.send_notification.call_args
        assert "Game start time needs to be set manually" in kwargs["message"]
        assert kwargs["title"] == "Set Game Start Time"
        assert kwargs["tags"] == ["warning", "info"]


@pytest.mark.asyncio
async def test_ask_game_end_time(ntfy_api):
    """Test sending notification about game end time."""

    async def mock_get_duration(*args, **kwargs):
        return "02:00:00"

    with (
        patch.object(ntfy_api, "send_notification", AsyncMock(return_value=True)),
        patch(
            "video_grouper.api_integrations.ntfy.get_video_duration", mock_get_duration
        ),
        patch(
            "video_grouper.api_integrations.ntfy.create_screenshot",
            AsyncMock(return_value=True),
        ),
        patch(
            "video_grouper.api_integrations.ntfy.compress_image",
            AsyncMock(return_value="test_screenshot.jpg"),
        ),
        patch("video_grouper.api_integrations.ntfy.os.path.exists", return_value=True),
        patch("video_grouper.api_integrations.ntfy.os.remove"),
    ):
        result = await ntfy_api.ask_game_end_time(
            combined_video_path="test_video.mp4",
            group_dir="test_dir",
            start_time_offset="00:10:00",
        )

        assert (
            result is None
        )  # This method just sends notification, doesn't return data

        ntfy_api.send_notification.assert_called_once()
        args, kwargs = ntfy_api.send_notification.call_args
        assert "Game end time needs to be set manually" in kwargs["message"]
        assert kwargs["title"] == "Set Game End Time"
        assert kwargs["tags"] == ["warning", "info"]


@pytest.mark.asyncio
async def test_ask_team_info(ntfy_api):
    """Test sending notification about missing team information."""

    async def mock_get_duration(*args, **kwargs):
        return "01:30:00"

    with (
        patch.object(ntfy_api, "send_notification", AsyncMock(return_value=True)),
        patch(
            "video_grouper.api_integrations.ntfy.get_video_duration", mock_get_duration
        ),
        patch(
            "video_grouper.api_integrations.ntfy.create_screenshot",
            AsyncMock(return_value=True),
        ),
        patch(
            "video_grouper.api_integrations.ntfy.compress_image",
            AsyncMock(return_value="test_screenshot.jpg"),
        ),
        patch("video_grouper.api_integrations.ntfy.os.path.exists", return_value=True),
        patch("video_grouper.api_integrations.ntfy.os.remove"),
    ):
        result = await ntfy_api.ask_team_info(
            combined_video_path="test_video.mp4", existing_info={}
        )

        assert result == {}  # Should return empty dict when no existing info provided

        ntfy_api.send_notification.assert_called_once()
        args, kwargs = ntfy_api.send_notification.call_args
        assert "Missing match information:" in kwargs["message"]

        ntfy_api.send_notification.reset_mock()

        existing_info = {"team_name": "Existing Team", "location": "Existing Stadium"}
        result = await ntfy_api.ask_team_info(
            combined_video_path="test_video.mp4", existing_info=existing_info
        )

        assert result == existing_info  # Should return the existing info dict

        ntfy_api.send_notification.assert_called_once()
        args, kwargs = ntfy_api.send_notification.call_args
        assert (
            "Missing match information:" in kwargs["message"]
        )  # Should still notify about missing opponent


@pytest.mark.asyncio
async def test_ask_resolve_game_conflict(ntfy_api):
    """Test sending notification to resolve a game conflict."""
    games = [
        {
            "source": "TeamSnap",
            "team_name": "A",
            "opponent_name": "B",
            "location_name": "Field 1",
        },
        {
            "source": "PlayMetrics",
            "team_name": "C",
            "opponent": "D",
            "location": "Field 2",
        },
    ]

    # Mock the response that would normally come from user interaction
    mock_response = {"response": "select_game/0"}

    with (
        patch.object(ntfy_api, "send_notification", AsyncMock(return_value=True)),
        patch("asyncio.wait_for", AsyncMock(return_value=mock_response)),
        patch(
            "video_grouper.api_integrations.ntfy.get_video_duration",
            AsyncMock(return_value="01:30:00"),
        ),
        patch(
            "video_grouper.api_integrations.ntfy.create_screenshot",
            AsyncMock(return_value=True),
        ),
        patch(
            "video_grouper.api_integrations.ntfy.compress_image",
            AsyncMock(return_value="test_screenshot.jpg"),
        ),
        patch("video_grouper.api_integrations.ntfy.os.path.exists", return_value=True),
        patch("video_grouper.api_integrations.ntfy.os.remove"),
    ):
        result = await ntfy_api.ask_resolve_game_conflict(
            combined_video_path="test_video.mp4",
            group_dir="test_dir",
            game_options=games,
        )

        assert result is not None
        assert (
            result == games[0]
        )  # Should return the first game since we mocked selection of index 0

        ntfy_api.send_notification.assert_called_once()
        args, kwargs = ntfy_api.send_notification.call_args
        assert "Multiple games found" in kwargs["message"]
        assert "Game Conflict - Select Correct Game" == kwargs["title"]
        assert kwargs["tags"] == ["warning", "question"]
