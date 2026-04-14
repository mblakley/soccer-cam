"""Unit tests for the pure-HTTP PlayMetrics client.

The client previously used Selenium; the legacy browser-based tests have
been replaced with HTTP-mocked equivalents that exercise the same public
contract (login → fetch calendars → parse → match recordings).
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from video_grouper.api_integrations.playmetrics import PlayMetricsAPI
from video_grouper.utils.config import AppConfig, PlayMetricsConfig


@pytest.fixture(autouse=True)
def _firebase_api_key(monkeypatch):
    """Every test runs with a non-empty Firebase API key so login() proceeds."""
    monkeypatch.setenv("PLAYMETRICS_FIREBASE_WEB_API_KEY", "test-key")


def _make_api(**overrides):
    """Build a PlayMetricsAPI with sensible defaults for tests."""
    cfg = PlayMetricsConfig(
        enabled=overrides.pop("enabled", True),
        username=overrides.pop("username", "test@example.com"),
        password=overrides.pop("password", "testpassword"),
        team_id=overrides.pop("team_id", "123456"),
        team_name=overrides.pop("team_name", "Test Team"),
    )
    app_config = AppConfig(timezone="America/New_York")
    return PlayMetricsAPI(cfg, app_config)


def _mock_response(status_code=200, json_body=None):
    """Build a minimal requests.Response stand-in."""
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_body or {}
    response.raise_for_status = MagicMock()
    if status_code >= 400:
        response.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    return response


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestPlayMetricsConstruction:
    def test_initialization(self):
        api = _make_api()
        assert api.enabled
        assert api.username == "test@example.com"
        assert api.password == "testpassword"
        assert api.team_id == "123456"
        assert api.team_name == "Test Team"

    def test_disabled_when_not_configured(self):
        api = _make_api(enabled=False)
        assert not api.enabled

    def test_login_returns_false_when_disabled(self):
        api = _make_api(enabled=False)
        assert api.login() is False
        assert api.logged_in is False

    def test_login_returns_false_when_firebase_key_missing(self, monkeypatch):
        monkeypatch.delenv("PLAYMETRICS_FIREBASE_WEB_API_KEY", raising=False)
        api = _make_api()
        assert api.login() is False

    def test_close_is_noop(self):
        # Pure-HTTP client has no resources to release; calling close()
        # twice in a row must not raise.
        api = _make_api()
        api.close()
        api.close()


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


class TestPlayMetricsLogin:
    def test_login_signs_in_and_caches_tokens(self):
        api = _make_api()

        with patch(
            "video_grouper.api_integrations.playmetrics.requests"
        ) as mock_requests:
            mock_requests.post.side_effect = [
                # signInWithPassword
                _mock_response(
                    json_body={
                        "idToken": "id-1",
                        "refreshToken": "refresh-1",
                        "expiresIn": "3600",
                    }
                ),
                # firebase/user/login (empty body — fetch roles)
                _mock_response(
                    json_body={"roles": [{"id": "role-1", "name": "Coach"}]}
                ),
            ]

            assert api.login() is True

        assert api.logged_in
        assert api.refresh_token == "refresh-1"
        assert api.current_role_id == "role-1"
        # signInWithPassword + role discovery
        assert mock_requests.post.call_count == 2

    def test_login_uses_refresh_token_when_present(self):
        api = _make_api()
        api.refresh_token = "preexisting-refresh"

        with patch(
            "video_grouper.api_integrations.playmetrics.requests"
        ) as mock_requests:
            mock_requests.post.side_effect = [
                # securetoken refresh
                _mock_response(
                    json_body={
                        "id_token": "id-2",
                        "refresh_token": "preexisting-refresh",
                        "expires_in": "3600",
                    }
                ),
                # firebase/user/login (empty body — roles)
                _mock_response(
                    json_body={"roles": [{"id": "role-1", "name": "Coach"}]}
                ),
            ]

            assert api.login() is True

        # Confirm the securetoken endpoint was hit (not signInWithPassword)
        first_call_url = mock_requests.post.call_args_list[0][0][0]
        assert "securetoken" in first_call_url

    def test_login_failure_returns_false(self):
        api = _make_api()
        with patch(
            "video_grouper.api_integrations.playmetrics.requests"
        ) as mock_requests:
            mock_requests.post.side_effect = Exception("network down")
            assert api.login() is False
            assert api.logged_in is False


# ---------------------------------------------------------------------------
# Calendar parsing
# ---------------------------------------------------------------------------


class TestPlayMetricsParser:
    def test_parse_api_calendars_extracts_games_and_practices(self):
        api = _make_api(team_id="123456")
        calendars = [
            {
                "team": {
                    "id": "123456",
                    "name": "Test Team",
                    "games": [
                        {
                            "id": "g-1",
                            "start_datetime": "2026-04-01T14:00:00Z",
                            "end_datetime": "2026-04-01T16:00:00Z",
                            "opponent_team_name": "Rival FC",
                            "is_home": True,
                            "field": {"display_name": "Field A"},
                            "league": {"name": "Spring League"},
                        },
                    ],
                    "practices": [
                        {
                            "id": "p-1",
                            "start_datetime": "2026-04-02T18:00:00Z",
                            "end_datetime": "2026-04-02T19:30:00Z",
                            "field": {"display_name": "Practice Field"},
                        },
                    ],
                }
            }
        ]
        events = api._parse_api_calendars(calendars)

        assert len(events) == 2
        game = next(e for e in events if e["is_game"])
        practice = next(e for e in events if not e["is_game"])

        assert game["id"] == "g-1"
        assert game["title"] == "Test Team vs Rival FC"
        assert game["opponent"] == "Rival FC"
        assert game["is_home"] is True
        assert game["location"] == "Field A"
        assert game["description"] == "Spring League"

        assert practice["id"] == "p-1"
        assert practice["location"] == "Practice Field"
        assert practice["opponent"] is None

    def test_parse_filters_by_configured_team_id(self):
        api = _make_api(team_id="123456")
        calendars = [
            {
                "team": {
                    "id": "999999",  # different team — should be skipped
                    "name": "Other Team",
                    "games": [
                        {
                            "id": "ignored",
                            "start_datetime": "2026-04-01T14:00:00Z",
                            "opponent_team_name": "X",
                        }
                    ],
                }
            },
            {
                "team": {
                    "id": "123456",
                    "name": "Test Team",
                    "games": [
                        {
                            "id": "g-keep",
                            "start_datetime": "2026-04-01T14:00:00Z",
                            "opponent_team_name": "Rival FC",
                        }
                    ],
                }
            },
        ]
        events = api._parse_api_calendars(calendars)
        assert [e["id"] for e in events] == ["g-keep"]

    def test_parse_includes_all_teams_when_team_id_zero(self):
        api = _make_api(team_id="0")  # discovery mode
        calendars = [
            {
                "team": {
                    "id": "111",
                    "name": "Team One",
                    "games": [
                        {
                            "id": "g-1",
                            "start_datetime": "2026-04-01T14:00:00Z",
                            "opponent_team_name": "X",
                        }
                    ],
                }
            },
            {
                "team": {
                    "id": "222",
                    "name": "Team Two",
                    "games": [
                        {
                            "id": "g-2",
                            "start_datetime": "2026-04-02T14:00:00Z",
                            "opponent_team_name": "Y",
                        }
                    ],
                }
            },
        ]
        events = api._parse_api_calendars(calendars)
        assert {e["id"] for e in events} == {"g-1", "g-2"}


# ---------------------------------------------------------------------------
# Match selection
# ---------------------------------------------------------------------------


class TestPlayMetricsFindGame:
    def test_find_game_for_recording_match(self):
        api = _make_api()
        game_time = datetime(2026, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
        events = [
            {
                "id": "1",
                "title": "Test vs Opponent",
                "start_time": game_time,
                "end_time": game_time + timedelta(hours=2),
                "is_game": True,
                "opponent": "Opponent",
            },
            {
                "id": "2",
                "title": "Practice",
                "start_time": game_time + timedelta(days=1),
                "end_time": game_time + timedelta(days=1, hours=2),
                "is_game": False,
            },
        ]
        api.get_games = MagicMock(return_value=[e for e in events if e["is_game"]])

        recording_start = game_time - timedelta(minutes=30)
        recording_end = game_time + timedelta(hours=1)
        result = api.find_game_for_recording(recording_start, recording_end)
        assert result is not None
        assert result["id"] == "1"

    def test_find_game_for_recording_no_match(self):
        api = _make_api()
        game_time = datetime(2026, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
        api.get_games = MagicMock(
            return_value=[
                {
                    "id": "1",
                    "title": "Test",
                    "start_time": game_time,
                    "end_time": game_time + timedelta(hours=2),
                    "is_game": True,
                }
            ]
        )

        recording_start = game_time + timedelta(days=2)
        recording_end = recording_start + timedelta(hours=1)
        assert api.find_game_for_recording(recording_start, recording_end) is None

    def test_populate_match_info_writes_fields(self):
        api = _make_api()
        game_time = datetime(2026, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
        api.find_game_for_recording = MagicMock(
            return_value={
                "id": "1",
                "title": "Test vs Opponent",
                "description": "Spring League",
                "location": "Field A",
                "start_time": game_time,
                "end_time": game_time + timedelta(hours=2),
                "is_game": True,
                "opponent": "Opponent",
            }
        )

        match_info: dict = {}
        assert (
            api.populate_match_info(
                match_info,
                game_time - timedelta(minutes=30),
                game_time + timedelta(hours=1),
            )
            is True
        )
        assert match_info["title"] == "Test vs Opponent"
        assert match_info["opponent"] == "Opponent"
        assert match_info["location"] == "Field A"
        assert match_info["date"] == "2026-06-15"
        assert match_info["time"] == "14:00"
        assert match_info["description"] == "Spring League"


# ---------------------------------------------------------------------------
# End-to-end auth + fetch flow (mocked)
# ---------------------------------------------------------------------------


class TestPlayMetricsEndToEnd:
    def test_get_games_full_http_path(self):
        api = _make_api(team_id="123456")

        sign_in = _mock_response(
            json_body={
                "idToken": "id-1",
                "refreshToken": "refresh-1",
                "expiresIn": "3600",
            }
        )
        roles_response = _mock_response(
            json_body={"roles": [{"id": "role-1", "name": "Coach"}]}
        )
        access_key_response = _mock_response(json_body={"access_key": "ak-1"})
        calendars_response = _mock_response(
            json_body=[
                {
                    "team": {
                        "id": "123456",
                        "name": "Test Team",
                        "games": [
                            {
                                "id": "g-1",
                                "start_datetime": "2026-04-01T14:00:00Z",
                                "end_datetime": "2026-04-01T16:00:00Z",
                                "opponent_team_name": "Rival FC",
                                "is_home": True,
                                "field": {"display_name": "Field A"},
                            }
                        ],
                    }
                }
            ]
        )

        with patch(
            "video_grouper.api_integrations.playmetrics.requests"
        ) as mock_requests:
            mock_requests.post.side_effect = [
                sign_in,
                roles_response,
                access_key_response,
            ]
            mock_requests.get.return_value = calendars_response

            games = api.get_games()

        assert len(games) == 1
        assert games[0]["title"] == "Test Team vs Rival FC"
        # 3 POSTs: signin → roles → access_key
        assert mock_requests.post.call_count == 3
        # 1 GET: /user/calendars
        assert mock_requests.get.call_count == 1
