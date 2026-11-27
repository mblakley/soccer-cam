"""PlayMetrics (``PlayMetricsService``). Home/Away from ``is_home`` (else its
location text); date/time from the kickoff."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..base import ScheduledGame, register_schedule_source
from ._service_adapter import ServiceBackedSource


@register_schedule_source
class PlayMetricsScheduleSource(ServiceBackedSource):
    name = "PlayMetrics"
    priority = 20
    service_key = "playmetrics"

    def to_game(self, game: dict[str, Any]) -> ScheduledGame:
        if "is_home" in game:
            location = "Home" if game["is_home"] else "Away"
        else:
            location = game.get("location", "")
        start = game.get("start_time")
        return ScheduledGame(
            source=self.name,
            team_name=game.get("team_name", ""),
            opponent=game.get("opponent", ""),
            location=location,
            kickoff=start if isinstance(start, datetime) else None,
            raw=game,
        )
