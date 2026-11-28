"""IWorkSource: where soccer-cam takes remote work from.

A work source hands out *items* of a few kinds (a clip request, a highlight
reel, a recording to process, a reprocess request) and is told how each one
goes. Every kind follows the same lifecycle::

    pending -> claim -> (details) -> progress / blocked -> complete | fail | cancelled

The methods are kind-agnostic; the implementation maps each verb onto whatever
its service does for that kind. A verb a source has no way to express for a
kind (TTT has no "fail" for clip requests, say) raises ``NotImplementedError``;
the processors only call what their kind supports.

Errors: ``pending`` isolates kinds -- one kind failing to list never hides the
others' items. Every other verb raises what the service raised, so each
processor keeps its own retry / report-and-move-on policy.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any

from video_grouper.integrations.registry import Provider

# Kinds of work. ``WorkItem.kind`` is one of these.
CLIP = "clip"  # cut segments from a recording and deliver them
HIGHLIGHT_REEL = "highlight_reel"  # render a reel from clips and upload it
PROCESS_RECORDING = "process_recording"  # run a recording through download -> upload
REPROCESS = "reprocess"  # re-run / correct part of a recording's processing

KINDS = (CLIP, HIGHLIGHT_REEL, PROCESS_RECORDING, REPROCESS)


@dataclass(frozen=True)
class WorkItem:
    """One piece of remote work.

    ``payload`` is the source's own description of the work (segments, delivery
    method, the reel's ``source``, a reprocess request's own ``kind`` ...).
    ``status`` is ``pending`` for new work; a source may also list work this
    install already holds, with its in-flight status (a reprocess request that
    is ``claimed`` / ``running``), so cancel and progress can reach it.
    """

    kind: str
    id: str
    payload: dict[str, Any] = field(default_factory=dict, compare=False)
    status: str = "pending"


class IWorkSource(Provider, ABC):
    @abstractmethod
    async def pending(self, kinds: Collection[str] | None = None) -> list[WorkItem]:
        """Work waiting for this install, of the given ``kinds`` (all when None).

        Only the kinds the caller can run are asked for, so a source never polls
        for work nobody will take. Never raises for one kind's failure: that
        kind is logged and skipped. Empty when signed out.
        """

    @abstractmethod
    async def claim(self, item: WorkItem) -> dict[str, Any] | None:
        """Take ``item`` for this install.

        Returns the claimed item as the source now has it (possibly ``{}``), or
        None when another worker already has it -- skip it then, don't fail it.
        """

    @abstractmethod
    async def details(self, item: WorkItem) -> dict[str, Any]:
        """What the item refers to beyond its payload.

        - ``highlight_reel``: ``{"clips": [...]}`` -- the reel's ordered clips,
          each with its ``recording_group_dir``.
        - ``reprocess``: ``{"recording": {...}}`` -- the recording the request
          is for (``file_group`` is its local group directory name).
        """

    @abstractmethod
    async def latest(self, item: WorkItem) -> dict[str, Any]:
        """The item as the source has it now (e.g. a reel that a previous run
        already uploaded carries its ``youtube_video_id``)."""

    @abstractmethod
    async def progress(
        self,
        item: WorkItem,
        step: str,
        pct: float | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """The item is at ``step`` (e.g. ``trimming``, ``downloading``, a
        pipeline step id), ``pct`` percent through it. ``detail`` carries extra
        fields for the source (e.g. a human-readable ``message``)."""

    @abstractmethod
    async def blocked(
        self, item: WorkItem, reason: str, detail: dict[str, Any] | None = None
    ) -> None:
        """This install can't do ``item`` (e.g. its source video isn't here);
        the item stays pending for another worker."""

    @abstractmethod
    async def complete(self, item: WorkItem, result: dict[str, Any]) -> None:
        """The item is done. ``result`` by kind:

        - ``clip``: ``{"url": <where the clips are>, "notes": <str | None>}``
        - ``highlight_reel``: ``{"file_path": <local file>, "youtube_video_id": <id>}``
        - ``process_recording``: the job's result (e.g. ``{"youtube_url": ...}``), as is
        - ``reprocess``: ``{}``
        """

    @abstractmethod
    async def fail(self, item: WorkItem, error: str | None) -> None:
        """The item failed; ``error`` says why (None when there is no message)."""

    @abstractmethod
    async def cancelled(self, item: WorkItem) -> None:
        """The item stopped because its requester cancelled it."""
