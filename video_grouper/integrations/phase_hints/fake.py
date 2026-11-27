"""In-memory IPhaseHintSource for tests. Not registered."""

from __future__ import annotations

from typing import Any

from .base import IPhaseHintSource


class FakePhaseHintSource(IPhaseHintSource):
    name = "fake"

    def __init__(self, taps: dict[str, list[dict[str, Any]]] | None = None):
        self.taps = dict(taps or {})  # group dir name -> taps
        self.asked: list[str] = []

    @classmethod
    def from_context(cls, context) -> None:
        return None

    async def event_taps(self, group_dir: str) -> list[dict[str, Any]]:
        import os

        name = os.path.basename(str(group_dir).rstrip("/\\"))
        self.asked.append(name)
        return list(self.taps.get(name, []))
