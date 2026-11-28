"""In-memory IWorkSource that hands out the items it was given and records
every call, for tests. Not registered."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any, NamedTuple

from .base import IWorkSource, WorkItem


class WorkCall(NamedTuple):
    verb: str
    kind: str
    id: str
    args: tuple[Any, ...] = ()


class FakeWorkSource(IWorkSource):
    """``claims`` / ``details`` / ``latest`` are keyed by item id. An unlisted
    claim returns the item's payload (a successful claim); a listed ``None`` is
    a lost race. ``errors`` maps a verb to the exception that verb raises."""

    name = "fake"

    def __init__(
        self,
        items: Collection[WorkItem] = (),
        *,
        claims: dict[str, dict[str, Any] | None] | None = None,
        details: dict[str, dict[str, Any]] | None = None,
        latest: dict[str, dict[str, Any]] | None = None,
        errors: dict[str, BaseException] | None = None,
    ):
        self.items: list[WorkItem] = list(items)
        self.claims = dict(claims or {})
        self.details_by_id = dict(details or {})
        self.latest_by_id = dict(latest or {})
        self.errors = dict(errors or {})
        self.calls: list[WorkCall] = []
        self.pending_kinds: list[Collection[str] | None] = []

    @classmethod
    def from_context(cls, context) -> None:
        return None

    def called(self, verb: str, item_id: str | None = None) -> list[WorkCall]:
        """The recorded calls of ``verb`` (optionally for one item), in order."""
        return [
            c
            for c in self.calls
            if c.verb == verb and (item_id is None or c.id == item_id)
        ]

    def _record(self, verb: str, item: WorkItem, *args: Any) -> None:
        self.calls.append(WorkCall(verb, item.kind, item.id, args))
        if verb in self.errors:
            raise self.errors[verb]

    async def pending(self, kinds: Collection[str] | None = None) -> list[WorkItem]:
        self.pending_kinds.append(kinds)
        if "pending" in self.errors:
            raise self.errors["pending"]
        return [i for i in self.items if kinds is None or i.kind in kinds]

    async def claim(self, item: WorkItem) -> dict[str, Any] | None:
        self._record("claim", item)
        if item.id in self.claims:
            return self.claims[item.id]
        return dict(item.payload)

    async def details(self, item: WorkItem) -> dict[str, Any]:
        self._record("details", item)
        return self.details_by_id.get(item.id, {})

    async def latest(self, item: WorkItem) -> dict[str, Any]:
        self._record("latest", item)
        return self.latest_by_id.get(item.id, dict(item.payload))

    async def progress(
        self,
        item: WorkItem,
        step: str,
        pct: float | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self._record("progress", item, step, pct, detail)

    async def blocked(
        self, item: WorkItem, reason: str, detail: dict[str, Any] | None = None
    ) -> None:
        self._record("blocked", item, reason, detail)

    async def complete(self, item: WorkItem, result: dict[str, Any]) -> None:
        self._record("complete", item, result)

    async def fail(self, item: WorkItem, error: str | None) -> None:
        self._record("fail", item, error)

    async def cancelled(self, item: WorkItem) -> None:
        self._record("cancelled", item)
