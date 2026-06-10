"""Task for processing TTT processing-jobs."""

from dataclasses import dataclass, field
from typing import Any

from ...queue_type import QueueType
from ..base_task import BaseTask


@dataclass(unsafe_hash=True)
class TTTJobTask(BaseTask):
    """Carries a TTT job response dict (download → combine → trim →
    upload pipeline driven by remote config) to the processor."""

    ttt_id: str
    payload: dict[str, Any] = field(default_factory=dict, hash=False)

    @classmethod
    def queue_type(cls) -> QueueType:
        return QueueType.TTT_JOB

    @property
    def task_type(self) -> str:
        return "ttt_job"

    def get_item_path(self) -> str:
        return self.ttt_id

    def serialize(self) -> dict[str, Any]:
        return {
            "task_type": self.task_type,
            "ttt_id": self.ttt_id,
            "payload": self.payload,
        }

    @classmethod
    def deserialize(cls, data: dict[str, object]) -> "TTTJobTask":
        return cls(ttt_id=data["ttt_id"], payload=dict(data.get("payload") or {}))

    async def execute(self) -> bool:
        """Execution is handled by TTTJobQueueProcessor.process_item()."""
        raise NotImplementedError("Use TTTJobQueueProcessor.process_item()")

    def __str__(self) -> str:
        return f"TTTJobTask(id={self.ttt_id})"
