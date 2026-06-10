"""Task for processing TTT reprocess requests — cross-network bridge
to the local pipeline_processor's reprocess-marker mechanism."""

from dataclasses import dataclass, field
from typing import Any

from ...queue_type import QueueType
from ..base_task import BaseTask


@dataclass(unsafe_hash=True)
class ReprocessRequestTask(BaseTask):
    """Carries a TTT reprocess request row to the processor.

    The processor's work is light (claim via TTT API, resolve the
    recording's local group_dir, write reprocess_request.json,
    nudge state.json), but going through the same QueueProcessor
    pattern means it benefits from priority + dedup + crash recovery
    + ResourceManager gating uniformly with the other TTT features.
    """

    ttt_id: str
    payload: dict[str, Any] = field(default_factory=dict, hash=False)

    @classmethod
    def queue_type(cls) -> QueueType:
        return QueueType.TTT_REPROCESS_REQUEST

    @property
    def task_type(self) -> str:
        return "ttt_reprocess_request"

    def get_item_path(self) -> str:
        return self.ttt_id

    def serialize(self) -> dict[str, Any]:
        return {
            "task_type": self.task_type,
            "ttt_id": self.ttt_id,
            "payload": self.payload,
        }

    @classmethod
    def deserialize(cls, data: dict[str, object]) -> "ReprocessRequestTask":
        return cls(ttt_id=data["ttt_id"], payload=dict(data.get("payload") or {}))

    async def execute(self) -> bool:
        """Execution is handled by ReprocessRequestQueueProcessor.process_item()."""
        raise NotImplementedError("Use ReprocessRequestQueueProcessor.process_item()")

    def __str__(self) -> str:
        return f"ReprocessRequestTask(id={self.ttt_id})"
