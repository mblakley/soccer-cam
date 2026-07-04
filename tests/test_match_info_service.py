"""Tests for MatchInfoService — TTT schedule consumer integration."""

from datetime import datetime
from unittest.mock import MagicMock

from video_grouper.task_processors.services.match_info_service import MatchInfoService


def _mk_service(schedule_service=None):
    teamsnap_service = MagicMock()
    teamsnap_service.enabled = False
    playmetrics_service = MagicMock()
    playmetrics_service.enabled = False
    ntfy_service = MagicMock()
    ntfy_service.enabled = False
    return MatchInfoService(
        teamsnap_service=teamsnap_service,
        playmetrics_service=playmetrics_service,
        ntfy_service=ntfy_service,
        schedule_service=schedule_service,
    )


# ---------------------------------------------------------------------------
# _collect_games_from_apis
# ---------------------------------------------------------------------------


class TestCollectGamesFromApis:
    def test_ttt_game_returned_skips_teamsnap_and_pm(self):
        """When TTT schedule returns a game, TeamSnap and PlayMetrics are not queried."""
        sched = MagicMock()
        ttt_game = {
            "id": "g1",
            "source": "TTT",
            "team_name": "Flash",
            "opponent_name": "Rivals",
            "location": "Home",
        }
        sched.find_game_for_recording.return_value = ttt_game

        svc = _mk_service(schedule_service=sched)
        svc.teamsnap_service.enabled = True
        svc.playmetrics_service.enabled = True

        start = datetime(2026, 6, 1, 10, 30)
        end = datetime(2026, 6, 1, 12, 30)
        result = svc._collect_games_from_apis(start, end)

        assert result == [ttt_game]
        svc.teamsnap_service.find_game_for_recording.assert_not_called()
        svc.playmetrics_service.find_game_for_recording.assert_not_called()

    def test_ttt_returns_none_falls_back_to_teamsnap(self):
        """When TTT returns None, the service falls through to TeamSnap."""
        sched = MagicMock()
        sched.find_game_for_recording.return_value = None

        ts_game = {
            "source": "TeamSnap",
            "team_name": "Flash",
            "opponent_name": "Rivals",
            "location_name": "Home Field",
        }
        svc = _mk_service(schedule_service=sched)
        svc.teamsnap_service.enabled = True
        svc.teamsnap_service.find_game_for_recording.return_value = ts_game

        start = datetime(2026, 6, 1, 10, 30)
        end = datetime(2026, 6, 1, 12, 30)
        result = svc._collect_games_from_apis(start, end)

        assert ts_game in result
        svc.teamsnap_service.find_game_for_recording.assert_called_once()

    def test_no_schedule_service_goes_straight_to_apis(self):
        """When schedule_service is None, TeamSnap/PM are queried directly."""
        ts_game = {
            "source": "TeamSnap",
            "team_name": "Flash",
            "opponent_name": "X",
            "location_name": "Y",
        }
        svc = _mk_service(schedule_service=None)
        svc.teamsnap_service.enabled = True
        svc.teamsnap_service.find_game_for_recording.return_value = ts_game

        start = datetime(2026, 6, 1, 10, 30)
        end = datetime(2026, 6, 1, 12, 30)
        result = svc._collect_games_from_apis(start, end)

        assert ts_game in result


# ---------------------------------------------------------------------------
# _convert_game_to_match_info — TTT branch
# ---------------------------------------------------------------------------


class TestConvertGameToMatchInfoTTT:
    def test_ttt_source_maps_correct_fields(self):
        svc = _mk_service()
        game = {
            "source": "TTT",
            "team_name": "Flash",
            "opponent_name": "Rivals",
            "location": "Home",
        }
        result = svc._convert_game_to_match_info(game)

        assert result["my_team_name"] == "Flash"
        assert result["opponent_team_name"] == "Rivals"
        assert result["location"] == "Home"

    def test_ttt_source_handles_missing_fields(self):
        svc = _mk_service()
        game = {"source": "TTT"}
        result = svc._convert_game_to_match_info(game)

        assert result["my_team_name"] == ""
        assert result["opponent_team_name"] == ""
        assert result["location"] == ""


# ---------------------------------------------------------------------------
# _select_best_game — TTT preference
# ---------------------------------------------------------------------------


class TestSelectBestGameTTT:
    def test_ttt_preferred_over_teamsnap(self):
        svc = _mk_service()
        ttt_game = {"source": "TTT", "id": "ttt-1"}
        ts_game = {"source": "TeamSnap", "id": "ts-1"}

        result = svc._select_best_game([ts_game, ttt_game])

        assert result == ttt_game

    def test_ttt_preferred_over_playmetrics(self):
        svc = _mk_service()
        ttt_game = {"source": "TTT", "id": "ttt-1"}
        pm_game = {"source": "PlayMetrics", "id": "pm-1"}

        result = svc._select_best_game([pm_game, ttt_game])

        assert result == ttt_game
