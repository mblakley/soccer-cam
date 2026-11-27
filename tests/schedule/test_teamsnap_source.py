"""The TeamSnap schedule source (wraps TeamSnapService)."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from video_grouper.schedule import ScheduleContext
from video_grouper.schedule.sources.teamsnap import TeamSnapScheduleSource

START = datetime(2026, 6, 1, 10, 30)
END = datetime(2026, 6, 1, 12, 30)


def _service(game=None, enabled=True):
    svc = MagicMock()
    svc.enabled = enabled
    svc.find_game_for_recording.return_value = game
    return svc


def _context(**services):
    return ScheduleContext(config=SimpleNamespace(), services=services)


class TestTeamSnapSource:
    def test_maps_the_venue_and_writes_no_date(self):
        source = TeamSnapScheduleSource(
            _service(
                {
                    "team_name": "Flash",
                    "opponent_name": "Rivals",
                    "location_name": "Field 3",
                }
            )
        )
        info = source.find_game(START, END).to_match_info()
        assert info == {
            "my_team_name": "Flash",
            "opponent_team_name": "Rivals",
            "location": "Field 3",
            "ttt_game_id": "",
        }

    def test_follows_the_service_enabled_flag(self):
        assert not TeamSnapScheduleSource(_service(enabled=False)).enabled

    def test_built_from_the_teamsnap_service(self):
        assert TeamSnapScheduleSource.from_context(_context()) == []
        assert (
            len(TeamSnapScheduleSource.from_context(_context(teamsnap=_service()))) == 1
        )
