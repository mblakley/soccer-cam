"""No hint source (TTT off): the phase detector runs on its own."""

from __future__ import annotations

from typing import Any

from video_grouper.integrations.registry import NULL_PRIORITY, provides

from ..base import IPhaseHintSource


@provides(IPhaseHintSource)
class NullPhaseHintSource(IPhaseHintSource):
    name = "none"
    priority = NULL_PRIORITY

    @classmethod
    def from_context(cls, context) -> NullPhaseHintSource:
        return cls()

    async def event_taps(self, group_dir: str) -> list[dict[str, Any]]:
        return []
