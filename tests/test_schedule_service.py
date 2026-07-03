"""Tests for ScheduleService — TTT schedule cache and recording auto-match."""

import json
from datetime import datetime
from unittest.mock import MagicMock

from video_grouper.task_processors.services.schedule_service import ScheduleService


def _mk_client(team_id="team-1", schedule=None):
    client = MagicMock()
    client.get_team_assignments.return_value = [{"team_id": team_id}]
    client.get_schedule.return_value = schedule or []
    return client


def _mk_service(tmp_path, client=None, config=None):
    if client is None:
        client = _mk_client()
    if config is None:
        config = MagicMock()
        config.ttt.team_name = ""
    return ScheduleService(str(tmp_path), config, client)


# ---------------------------------------------------------------------------
# refresh()
# ---------------------------------------------------------------------------


def test_refresh_writes_json_and_returns_true(tmp_path):
    games = [
        {
            "id": "g1",
            "start_time": "2026-06-01T10:00:00",
            "end_time": "2026-06-01T12:00:00",
            "opponent_name": "Rivals",
        }
    ]
    client = _mk_client(team_id="team-1", schedule=games)
    svc = _mk_service(tmp_path, client)

    result = svc.refresh()

    assert result is True
    cache = tmp_path / "ttt" / "schedule_team-1.json"
    assert cache.exists()
    saved = json.loads(cache.read_text())
    assert saved == games


def test_refresh_returns_false_on_client_error(tmp_path):
    client = _mk_client()
    client.get_schedule.side_effect = Exception("network failure")
    svc = _mk_service(tmp_path, client)

    result = svc.refresh()  # must not raise

    assert result is False


def test_refresh_returns_false_when_no_team_id(tmp_path):
    client = MagicMock()
    client.get_team_assignments.return_value = []
    svc = _mk_service(tmp_path, client)

    result = svc.refresh()

    assert result is False
    client.get_schedule.assert_not_called()


# ---------------------------------------------------------------------------
# find_game_for_recording()
# ---------------------------------------------------------------------------


def _seed_cache(tmp_path, team_id, games):
    ttt_dir = tmp_path / "ttt"
    ttt_dir.mkdir(parents=True, exist_ok=True)
    (ttt_dir / f"schedule_{team_id}.json").write_text(json.dumps(games))


def test_find_game_returns_overlapping_game(tmp_path):
    games = [
        {
            "id": "g1",
            "start_time": "2026-06-01T10:00:00",
            "end_time": "2026-06-01T12:00:00",
            "opponent_name": "Rivals",
            "location": "Home",
        }
    ]
    _seed_cache(tmp_path, "team-1", games)
    client = _mk_client(team_id="team-1")
    svc = _mk_service(tmp_path, client)

    # Recording from 10:30 to 11:30 — well within the game window
    result = svc.find_game_for_recording(
        datetime(2026, 6, 1, 10, 30),
        datetime(2026, 6, 1, 11, 30),
    )

    assert result is not None
    assert result["id"] == "g1"
    assert result["source"] == "TTT"
    assert result["team_name"] == ""  # no team_name in config mock


def test_find_game_returns_none_when_no_overlap(tmp_path):
    games = [
        {
            "id": "g1",
            "start_time": "2026-06-01T10:00:00",
            "end_time": "2026-06-01T12:00:00",
            "opponent_name": "Rivals",
        }
    ]
    _seed_cache(tmp_path, "team-1", games)
    client = _mk_client(team_id="team-1")
    svc = _mk_service(tmp_path, client)

    # Recording at 15:30–16:00 — game midpoint is 11:00, distance ~4.5h > 2h guard
    result = svc.find_game_for_recording(
        datetime(2026, 6, 1, 15, 30),
        datetime(2026, 6, 1, 16, 0),
    )

    assert result is None


def test_find_game_returns_none_when_no_cache(tmp_path):
    client = _mk_client(team_id="team-1")
    svc = _mk_service(tmp_path, client)

    result = svc.find_game_for_recording(
        datetime(2026, 6, 1, 10, 30),
        datetime(2026, 6, 1, 11, 30),
    )

    assert result is None


def test_find_game_tags_team_name_from_config(tmp_path):
    games = [
        {
            "id": "g1",
            "start_time": "2026-06-01T10:00:00",
            "end_time": "2026-06-01T12:00:00",
        }
    ]
    _seed_cache(tmp_path, "team-1", games)
    client = _mk_client(team_id="team-1")
    config = MagicMock()
    config.ttt.team_name = "Flash"
    svc = ScheduleService(str(tmp_path), config, client)

    result = svc.find_game_for_recording(
        datetime(2026, 6, 1, 10, 30),
        datetime(2026, 6, 1, 11, 30),
    )

    assert result is not None
    assert result["team_name"] == "Flash"


def test_find_game_tags_team_name_from_assignment(tmp_path):
    """The /device-link/me assignment carries team_name; it wins over config."""
    games = [
        {
            "id": "g1",
            "start_time": "2026-06-01T10:00:00",
            "end_time": "2026-06-01T12:00:00",
        }
    ]
    _seed_cache(tmp_path, "team-1", games)
    client = MagicMock()
    client.get_team_assignments.return_value = [
        {"team_id": "team-1", "team_name": "Heat"}
    ]
    config = MagicMock()
    config.ttt.team_name = "ConfigOverride"
    svc = ScheduleService(str(tmp_path), config, client)

    result = svc.find_game_for_recording(
        datetime(2026, 6, 1, 10, 30),
        datetime(2026, 6, 1, 11, 30),
    )

    assert result is not None
    assert result["team_name"] == "Heat"  # assignment wins over config fallback


def test_find_game_converts_utc_to_camera_timezone(tmp_path):
    """Regression: TTT serialises game times as UTC (TIMESTAMPTZ); recording times
    are naive camera-local. A 14:00 EDT game arrives as 18:00Z and must be
    converted to 14:00 local — NOT stripped to a naive 18:00 (4h off → never
    matches within the 2-hour proximity guard)."""
    games = [
        {
            "game_id": "g-utc",
            "start_time": "2026-06-01T18:00:00+00:00",  # 14:00 EDT (UTC-4 in June)
            "end_time": "2026-06-01T20:00:00+00:00",  # 16:00 EDT
            "opponent_name": "Rivals",
        }
    ]
    _seed_cache(tmp_path, "team-1", games)
    client = _mk_client(team_id="team-1")
    config = MagicMock()
    config.ttt.team_name = ""
    config.app.timezone = "America/New_York"
    svc = ScheduleService(str(tmp_path), config, client)

    # Recording 14:30–15:30 LOCAL — inside the game once the UTC time is
    # converted to EDT. With the old naive strip this returned None.
    result = svc.find_game_for_recording(
        datetime(2026, 6, 1, 14, 30),
        datetime(2026, 6, 1, 15, 30),
    )
    assert result is not None
    assert result["game_id"] == "g-utc"


# ---------------------------------------------------------------------------
# _resolve_team_id()
# ---------------------------------------------------------------------------


def test_resolve_team_id_caches_result(tmp_path):
    client = _mk_client(team_id="team-1")
    svc = _mk_service(tmp_path, client)

    id1 = svc._resolve_team_id()
    id2 = svc._resolve_team_id()

    assert id1 == "team-1"
    assert id2 == "team-1"
    # Should only call the API once
    client.get_team_assignments.assert_called_once()


def test_resolve_team_id_returns_none_on_error(tmp_path):
    client = MagicMock()
    client.get_team_assignments.side_effect = Exception("network error")
    svc = _mk_service(tmp_path, client)

    result = svc._resolve_team_id()

    assert result is None


def test_resolve_team_id_returns_none_when_no_assignments(tmp_path):
    client = MagicMock()
    client.get_team_assignments.return_value = []
    svc = _mk_service(tmp_path, client)

    result = svc._resolve_team_id()

    assert result is None
