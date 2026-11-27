"""IPhaseHintSource: human hints about where a game's phases are.

Today: parents tapping "kickoff" / "halftime" / "game end" on their phones
while watching. The phase detector fuses these with what it sees; with no
hints it runs detector-only. Best-effort: never raises, ``[]`` when unknown.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from video_grouper.integrations.registry import Provider


class IPhaseHintSource(Provider, ABC):
    @abstractmethod
    async def event_taps(self, group_dir: str) -> list[dict[str, Any]]:
        """Event taps for the game recorded in ``group_dir``: dicts with at
        least ``label`` and ``video_time_seconds`` (and ``anchor_type ==
        "event_tap"``). Empty when there are none or nobody to ask."""
