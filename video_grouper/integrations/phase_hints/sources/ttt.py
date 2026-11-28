"""Team Tech Tools phase hints: the event taps parents recorded on the game
session (found by the recording's group directory name). Only asked while
signed in; any failure -> no hints."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from video_grouper.integrations.registry import provides

from ..base import IPhaseHintSource

logger = logging.getLogger(__name__)


@provides(IPhaseHintSource)
class TTTPhaseHintSource(IPhaseHintSource):
    name = "ttt"
    priority = 10

    def __init__(self, client: Any):
        self._client = client

    @classmethod
    def from_context(cls, context) -> TTTPhaseHintSource | None:
        return cls(context.ttt_client()) if context.ttt_enabled() else None

    def _event_taps_sync(self, group_dir: str) -> list[dict[str, Any]]:
        if not self._client.ensure_signed_in():  # in a thread: may refresh
            return []
        name = os.path.basename(str(group_dir).rstrip("/\\"))
        session = self._client.get_game_session_by_dir(name)
        if not session or not session.get("id"):
            return []
        anchors = self._client.get_sync_anchors(session["id"]) or []
        return [
            a
            for a in anchors
            if isinstance(a, dict) and a.get("anchor_type") == "event_tap"
        ]

    async def event_taps(self, group_dir: str) -> list[dict[str, Any]]:
        try:
            return await asyncio.to_thread(self._event_taps_sync, group_dir)
        except Exception as e:
            logger.warning("TTT: event-tap fetch for %s failed: %s", group_dir, e)
            return []
