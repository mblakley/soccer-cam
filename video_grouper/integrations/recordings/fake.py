"""In-memory IRecordingSink that records every report, for tests. Not registered."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .base import IRecordingSink, StepReport


class FakeRecordingSink(IRecordingSink):
    name = "fake"

    def __init__(self, accept_phases: bool = True):
        self.found: list[tuple[str, list[Any]]] = []
        self.steps: list[tuple[str, StepReport]] = []
        self.published_videos: list[tuple[str, str, datetime]] = []
        self.phase_payloads: list[tuple[str, dict[str, Any]]] = []
        self.verdicts: list[tuple[str, str, str]] = []
        self._accept_phases = accept_phases

    @classmethod
    def from_context(cls, context) -> None:
        return None

    async def recording_found(self, group_dir: str, files: list[Any]) -> None:
        self.found.append((group_dir, list(files)))

    async def progress(self, group_dir: str, report: StepReport) -> None:
        self.steps.append((group_dir, report))

    async def published(
        self, group_dir: str, video_url: str, recorded_at: datetime
    ) -> None:
        self.published_videos.append((group_dir, video_url, recorded_at))

    async def phases(self, group_dir: str, payload: dict[str, Any]) -> bool:
        self.phase_payloads.append((group_dir, payload))
        return self._accept_phases

    async def phase_verified(self, group_dir: str, phase: str, verdict: str) -> bool:
        self.verdicts.append((group_dir, phase, verdict))
        return self._accept_phases

    def step_statuses(self, group_dir: str | None = None) -> list[tuple[str, str]]:
        """(step_id, status) pairs in report order, optionally for one group."""
        return [
            (r.step_id, r.status)
            for g, r in self.steps
            if group_dir is None or g == group_dir
        ]
