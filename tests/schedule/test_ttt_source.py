"""The TTT schedule source (wraps ScheduleService)."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from video_grouper.schedule import ScheduleContext
from video_grouper.schedule.sources.ttt import TTTScheduleSource

START = datetime(2026, 6, 1, 10, 30)
END = datetime(2026, 6, 1, 12, 30)


def _service(game=None, enabled=True):
    svc = MagicMock()
    svc.enabled = enabled
    svc.find_game_for_recording.return_value = game
    return svc


def _context(**services):
    return ScheduleContext(config=SimpleNamespace(), services=services)


class TestTTTSource:
    def test_maps_the_ttt_game(self):
        source = TTTScheduleSource(
            _service(
                {"team_name": "Flash", "opponent_name": "Rivals", "location": "Home"}
            )
        )
        assert source.find_game(START, END).to_match_info() == {
            "my_team_name": "Flash",
            "opponent_team_name": "Rivals",
            "location": "Home",
            "ttt_game_id": "",
        }

    def test_missing_fields_become_empty(self):
        assert TTTScheduleSource(_service({"id": "g1"})).find_game(
            START, END
        ).to_match_info() == {
            "my_team_name": "",
            "opponent_team_name": "",
            "location": "",
            "ttt_game_id": "",
        }

    def test_keeps_the_matched_ttt_game_id(self):
        game = TTTScheduleSource(_service({"game_id": "g-42"})).find_game(START, END)
        assert game.ttt_game_id == "g-42"
        assert game.to_match_info()["ttt_game_id"] == "g-42"

    def test_is_authoritative(self):
        assert TTTScheduleSource.authoritative is True

    def test_built_only_once_signed_in(self):
        assert TTTScheduleSource.from_context(_context()) == []
        assert (
            len(TTTScheduleSource.from_context(_context(ttt_schedule=_service()))) == 1
        )
