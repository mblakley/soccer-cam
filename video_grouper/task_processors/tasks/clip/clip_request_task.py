"""Task for processing TTT clip requests."""

import logging
from dataclasses import dataclass, field
from typing import Dict, Any

from ..base_task import BaseTask
from ...queue_type import QueueType

logger = logging.getLogger(__name__)


@dataclass(unsafe_hash=True)
class ClipRequestTask(BaseTask):
    """Task representing a TTT clip request to be processed.

    The processor handles execution — this task just carries the data.
    """

    clip_request_id: str
    game_session_id: str
    recording_group_dir: str
    segments: list = field(default_factory=list, hash=False)
    is_compilation: bool = False
    delivery_method: str = "external_storage"

    @classmethod
    def queue_type(cls) -> QueueType:
        return QueueType.CLIP_REQUEST

    @property
    def task_type(self) -> str:
        return "clip_request"

    def get_item_path(self) -> str:
        return self.recording_group_dir

    def serialize(self) -> Dict[str, Any]:
        return {
            "task_type": self.task_type,
            "clip_request_id": self.clip_request_id,
            "game_session_id": self.game_session_id,
            "recording_group_dir": self.recording_group_dir,
            "segments": self.segments,
            "is_compilation": self.is_compilation,
            "delivery_method": self.delivery_method,
        }

    @classmethod
    def deserialize(cls, data: Dict[str, object]) -> "ClipRequestTask":
        return cls(
            clip_request_id=data["clip_request_id"],
            game_session_id=data["game_session_id"],
            recording_group_dir=data["recording_group_dir"],
            segments=data.get("segments", []),
            is_compilation=data.get("is_compilation", False),
            delivery_method=data.get("delivery_method", "external_storage"),
        )

    async def execute(self) -> bool:
        """Execution is handled by ClipRequestProcessor.process_item()."""
        raise NotImplementedError("Use ClipRequestProcessor.process_item()")

    def __str__(self) -> str:
        return f"ClipRequestTask(id={self.clip_request_id}, dir={self.recording_group_dir})"
