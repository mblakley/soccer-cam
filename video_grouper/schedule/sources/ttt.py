"""Team Tech Tools' schedule (``ScheduleService``, the local cache of TTT games).

Authoritative: a TTT team's TeamSnap / PlayMetrics / calendar games are already
in TTT, so a TTT match ends the search. Location as TTT gives it; no date/time.
"""

from __future__ import annotations

from typing import Any

from ..base import ScheduledGame, register_schedule_source
from ._service_adapter import ServiceBackedSource


@register_schedule_source
class TTTScheduleSource(ServiceBackedSource):
    name = "TTT"
    priority = 0
    authoritative = True
    service_key = "ttt_schedule"

    @property
    def enabled(self) -> bool:
        return self._service is not None

    def to_game(self, game: dict[str, Any]) -> ScheduledGame:
        return ScheduledGame(
            source=self.name,
            team_name=game.get("team_name", ""),
            opponent=game.get("opponent_name", ""),
            location=game.get("location", ""),
            raw=game,
        )
