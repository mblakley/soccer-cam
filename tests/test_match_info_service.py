"""MatchInfoService, tested only through the IScheduleSource interface.

No real provider appears here: every source is a FakeSource. Each implementation
is tested on its own under tests/schedule/.
"""

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from video_grouper.schedule import IScheduleSource, ScheduledGame
from video_grouper.task_processors.services.match_info_service import MatchInfoService

START = datetime(2026, 6, 1, 10, 30)
END = datetime(2026, 6, 1, 12, 30)


class FakeSource(IScheduleSource):
    """A source that returns a fixed game (or raises), and records its calls."""

    def __init__(
        self,
        name: str,
        priority: int = 50,
        game: ScheduledGame | None = None,
        authoritative: bool = False,
        enabled: bool = True,
        error: Exception | None = None,
    ):
        self.name = name  # type: ignore[misc]
        self.priority = priority  # type: ignore[misc]
        self.authoritative = authoritative  # type: ignore[misc]
        self._enabled = enabled
        self._game = game
        self._error = error
        self.calls = 0
        self.closed = False

    @classmethod
    def from_context(cls, context):
        return []

    @property
    def enabled(self) -> bool:
        return self._enabled

    def find_game(self, recording_start, recording_end):
        self.calls += 1
        if self._error:
            raise self._error
        return self._game

    def close(self) -> None:
        self.closed = True


def game(source: str, opponent: str = "Rivals") -> ScheduledGame:
    return ScheduledGame(source=source, team_name="Flash", opponent=opponent)


def service(*sources: IScheduleSource) -> MatchInfoService:
    ntfy = MagicMock()
    ntfy.enabled = False
    return MatchInfoService(list(sources), ntfy)


class TestFindGame:
    def test_an_authoritative_match_ends_the_search(self):
        other = FakeSource("B", priority=10, game=game("B"))
        svc = service(
            other, FakeSource("A", priority=0, game=game("A"), authoritative=True)
        )
        assert svc.find_game(START, END).source == "A"
        assert other.calls == 0

    def test_an_authoritative_source_with_no_match_falls_through(self):
        svc = service(
            FakeSource("A", priority=0, authoritative=True),
            FakeSource("B", priority=10, game=game("B")),
        )
        assert svc.find_game(START, END).source == "B"

    def test_lowest_priority_match_wins_whatever_the_order(self):
        svc = service(
            FakeSource("late", priority=20, game=game("late")),
            FakeSource("early", priority=10, game=game("early")),
        )
        assert svc.find_game(START, END).source == "early"

    def test_disabled_sources_are_not_asked(self):
        off = FakeSource("off", priority=0, game=game("off"), enabled=False)
        svc = service(off, FakeSource("on", priority=10, game=game("on")))
        assert svc.find_game(START, END).source == "on"
        assert off.calls == 0

    def test_a_failing_source_is_skipped(self):
        svc = service(
            FakeSource("broken", priority=0, error=RuntimeError("API down")),
            FakeSource("ok", priority=10, game=game("ok")),
        )
        assert svc.find_game(START, END).source == "ok"

    def test_no_match_anywhere(self):
        assert service(FakeSource("A"), FakeSource("B")).find_game(START, END) is None

    def test_no_sources(self):
        assert service().find_game(START, END) is None


class TestAddSource:
    def test_a_source_added_later_takes_its_priority_place(self):
        svc = service(FakeSource("B", priority=10, game=game("B")))
        svc.add_source(FakeSource("A", priority=0, game=game("A"), authoritative=True))
        assert [s.name for s in svc.sources] == ["A", "B"]
        assert svc.find_game(START, END).source == "A"

    def test_adding_a_source_with_the_same_name_replaces_it(self):
        svc = service(FakeSource("A", game=game("A", opponent="old")))
        svc.add_source(FakeSource("A", game=game("A", opponent="new")))
        assert len(svc.sources) == 1
        assert svc.find_game(START, END).opponent == "new"


class TestShutdown:
    @pytest.mark.asyncio
    async def test_every_source_is_closed(self):
        a, b = FakeSource("A"), FakeSource("B")
        svc = service(a, b)

        async def _noop():
            return None

        svc.ntfy_service.shutdown = _noop
        await svc.shutdown()
        assert a.closed and b.closed


class TestScheduledGame:
    def test_match_info_fields(self):
        info = ScheduledGame(
            source="X",
            team_name="Flash",
            opponent="Rivals",
            location="Away",
            kickoff=datetime(2026, 6, 1, 11, 0),
        ).to_match_info()
        assert info == {
            "my_team_name": "Flash",
            "opponent_team_name": "Rivals",
            "location": "Away",
            "date": "2026-06-01",
            "time": "11:00",
            "ttt_game_id": "",
        }

    def test_a_known_ttt_game_id_is_written(self):
        info = ScheduledGame(
            source="X", team_name="A", opponent="B", ttt_game_id="g-42"
        ).to_match_info()
        assert info["ttt_game_id"] == "g-42"

    def test_a_non_ttt_game_states_it_has_no_ttt_game(self):
        # "" (not absent) so a re-match clears an id from an earlier TTT match.
        info = ScheduledGame(source="X", team_name="A", opponent="B").to_match_info()
        assert info["ttt_game_id"] == ""

    def test_no_kickoff_writes_no_date_or_time(self):
        info = ScheduledGame(source="X", team_name="A", opponent="B").to_match_info()
        assert "date" not in info and "time" not in info
