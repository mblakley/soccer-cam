"""No recording sink (TTT off): reports go nowhere; local state is unaffected."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from video_grouper.integrations.registry import NULL_PRIORITY, provides

from ..base import IRecordingSink, StepReport


@provides(IRecordingSink)
class NullRecordingSink(IRecordingSink):
    name = "none"
    priority = NULL_PRIORITY

    @classmethod
    def from_context(cls, context) -> NullRecordingSink:
        return cls()

    async def recording_found(
        self, group_dir: str, files: list[Any], camera: str | None = None
    ) -> None:
        return None

    async def progress(self, group_dir: str, report: StepReport) -> None:
        return None

    async def published(
        self, group_dir: str, video_url: str, recorded_at: datetime
    ) -> None:
        return None

    async def phases(self, group_dir: str, payload: dict[str, Any]) -> bool:
        return False

    async def phase_verified(self, group_dir: str, phase: str, verdict: str) -> bool:
        return False
