"""TeamSnap (``TeamSnapService``). Location is the venue; no date/time."""

from __future__ import annotations

from typing import Any

from ..base import ScheduledGame, register_schedule_source
from ._service_adapter import ServiceBackedSource


@register_schedule_source
class TeamSnapScheduleSource(ServiceBackedSource):
    name = "TeamSnap"
    priority = 10
    service_key = "teamsnap"

    def to_game(self, game: dict[str, Any]) -> ScheduledGame:
        return ScheduledGame(
            source=self.name,
            team_name=game.get("team_name", ""),
            opponent=game.get("opponent_name", ""),
            location=game.get("location_name", ""),
            raw=game,
        )
