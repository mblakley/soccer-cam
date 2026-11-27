"""Shared base for sources that wrap one of the app's existing schedule services
(``enabled`` + ``find_game_for_recording(start, end) -> dict | None``)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..base import IScheduleSource, ScheduledGame


class ServiceBackedSource(IScheduleSource):
    """Subclasses set ``name``/``priority``/``service_key`` and map the service's
    game dict with ``to_game``."""

    service_key = ""

    def __init__(self, service: Any):
        self._service = service

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._service, "enabled", False))

    def find_game(
        self, recording_start: datetime, recording_end: datetime
    ) -> ScheduledGame | None:
        game = self._service.find_game_for_recording(recording_start, recording_end)
        return self.to_game(game) if game else None

    def to_game(self, game: dict[str, Any]) -> ScheduledGame:
        raise NotImplementedError

    def close(self) -> None:
        close = getattr(self._service, "close", None)
        if callable(close):
            close()

    @classmethod
    def from_context(cls, context) -> list[IScheduleSource]:
        service = context.services.get(cls.service_key)
        return [cls(service)] if service is not None else []
