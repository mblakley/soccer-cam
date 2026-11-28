"""IRecordingSink: where soccer-cam reports what happens to a recording.

Every method is best-effort: it never raises and never blocks the pipeline on
a reporting problem -- the local state (state.json, phases.json) stays the
source of truth. Recordings are named by their group directory; a sink keeps
whatever ids of its own it needs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from video_grouper.integrations.registry import Provider


@dataclass(frozen=True)
class StepReport:
    """One processing step's state for a recording (download, combine, upload,
    a pipeline step, ...)."""

    step_id: str
    step_type: str
    label: str
    status: str  # pending | running | completed | failed | skipped
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None
    config: dict[str, Any] | None = None
    artifacts: dict[str, Any] | None = None
    pipeline_preset: str | None = None


class IRecordingSink(Provider, ABC):
    @abstractmethod
    async def recording_found(
        self, group_dir: str, files: list[Any], camera: str | None = None
    ) -> None:
        """New camera files were discovered for ``group_dir`` (by ``camera``,
        the camera's name, when known)."""

    @abstractmethod
    async def progress(self, group_dir: str, report: StepReport) -> None:
        """A processing step changed state."""

    @abstractmethod
    async def published(
        self, group_dir: str, video_url: str, recorded_at: datetime
    ) -> None:
        """The finished game video is online at ``video_url``."""

    @abstractmethod
    async def phases(self, group_dir: str, payload: dict[str, Any]) -> bool:
        """The game's phase boundaries (``{times:{kickoff,...}, ok, source}``).
        True iff the sink recorded them."""

    @abstractmethod
    async def phase_verified(self, group_dir: str, phase: str, verdict: str) -> bool:
        """A person confirmed (``correct``) or rejected (``not_correct``) one
        phase boundary. True iff the sink recorded it."""
