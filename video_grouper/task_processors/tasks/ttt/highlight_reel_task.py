"""Task for processing TTT highlight reels."""

from dataclasses import dataclass, field
from typing import Any

from ...queue_type import QueueType
from ..base_task import BaseTask


@dataclass(unsafe_hash=True)
class HighlightReelTask(BaseTask):
    """Carries a TTT highlight reel response dict to the processor.

    The processor reads ``payload`` directly (source, title, clips
    etc.); the only structured field on the task itself is ``ttt_id``
    so the queue's dedup-by-key check can identify duplicates without
    parsing the payload.
    """

    ttt_id: str
    payload: dict[str, Any] = field(default_factory=dict, hash=False)

    @classmethod
    def queue_type(cls) -> QueueType:
        return QueueType.TTT_HIGHLIGHT_REEL

    @property
    def task_type(self) -> str:
        return "ttt_highlight_reel"

    def get_item_path(self) -> str:
        return self.ttt_id

    def serialize(self) -> dict[str, Any]:
        return {
            "task_type": self.task_type,
            "ttt_id": self.ttt_id,
            "payload": self.payload,
        }

    @classmethod
    def deserialize(cls, data: dict[str, object]) -> "HighlightReelTask":
        return cls(ttt_id=data["ttt_id"], payload=dict(data.get("payload") or {}))

    async def execute(self) -> bool:
        """Execution is handled by HighlightReelQueueProcessor.process_item()."""
        raise NotImplementedError("Use HighlightReelQueueProcessor.process_item()")

    def __str__(self) -> str:
        return f"HighlightReelTask(id={self.ttt_id})"
