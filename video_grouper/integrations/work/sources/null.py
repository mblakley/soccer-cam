"""No work source (TTT off): there is never any remote work; every report is a
no-op and nothing can be claimed."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from video_grouper.integrations.registry import NULL_PRIORITY, provides

from ..base import IWorkSource, WorkItem


@provides(IWorkSource)
class NullWorkSource(IWorkSource):
    name = "none"
    priority = NULL_PRIORITY

    @classmethod
    def from_context(cls, context) -> NullWorkSource:
        return cls()

    async def pending(self, kinds: Collection[str] | None = None) -> list[WorkItem]:
        return []

    async def claim(self, item: WorkItem) -> dict[str, Any] | None:
        return None

    async def details(self, item: WorkItem) -> dict[str, Any]:
        return {}

    async def latest(self, item: WorkItem) -> dict[str, Any]:
        return {}

    async def progress(
        self,
        item: WorkItem,
        step: str,
        pct: float | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        return None

    async def blocked(
        self, item: WorkItem, reason: str, detail: dict[str, Any] | None = None
    ) -> None:
        return None

    async def complete(self, item: WorkItem, result: dict[str, Any]) -> None:
        return None

    async def fail(self, item: WorkItem, error: str | None) -> None:
        return None

    async def cancelled(self, item: WorkItem) -> None:
        return None
