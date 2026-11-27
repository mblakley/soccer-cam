"""The PlayMetrics schedule source (wraps PlayMetricsService)."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from video_grouper.schedule import ScheduleContext
from video_grouper.schedule.sources.playmetrics import PlayMetricsScheduleSource

START = datetime(2026, 6, 1, 10, 30)
END = datetime(2026, 6, 1, 12, 30)


def _service(game=None, enabled=True):
    svc = MagicMock()
    svc.enabled = enabled
    svc.find_game_for_recording.return_value = game
    return svc


def _context(**services):
    return ScheduleContext(config=SimpleNamespace(), services=services)


class TestPlayMetricsSource:
    def test_maps_home_away_and_kickoff(self):
        source = PlayMetricsScheduleSource(
            _service(
                {
                    "team_name": "Flash",
                    "opponent": "Rivals",
                    "is_home": False,
                    "start_time": datetime(2026, 6, 1, 11, 0),
                }
            )
        )
        assert source.find_game(START, END).to_match_info() == {
            "my_team_name": "Flash",
            "opponent_team_name": "Rivals",
            "location": "Away",
            "date": "2026-06-01",
            "time": "11:00",
            "ttt_game_id": "",
        }

    def test_without_is_home_keeps_the_location_text(self):
        game = PlayMetricsScheduleSource(
            _service({"team_name": "Flash", "opponent": "X", "location": "Field B"})
        ).find_game(START, END)
        assert game.location == "Field B" and game.kickoff is None

    def test_no_game(self):
        assert PlayMetricsScheduleSource(_service(None)).find_game(START, END) is None

    def test_close_closes_the_service(self):
        svc = _service()
        PlayMetricsScheduleSource(svc).close()
        svc.close.assert_called_once()
