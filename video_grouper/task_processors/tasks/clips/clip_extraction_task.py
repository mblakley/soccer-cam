"""
Task for extracting a single clip from a trimmed video using FFmpeg.
"""

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict

from ..base_task import BaseTask
from ...queue_type import QueueType
from video_grouper.utils.ffmpeg_utils import trim_video

logger = logging.getLogger(__name__)


@dataclass(unsafe_hash=True)
class ClipExtractionTask(BaseTask):
    """Extract a short clip from a trimmed video centered on a tagged moment."""

    tag_id: str
    clip_id: str
    game_session_id: str
    group_dir: str
    trimmed_video_path: str
    clip_start: float
    clip_end: float
    clip_output_path: str

    @classmethod
    def queue_type(cls) -> QueueType:
        return QueueType.CLIPS

    @property
    def task_type(self) -> str:
        return "clip_extraction"

    def get_item_path(self) -> str:
        return self.clip_output_path

    async def execute(self) -> bool:
        """Extract the clip using FFmpeg."""
        os.makedirs(os.path.dirname(self.clip_output_path), exist_ok=True)

        duration = self.clip_end - self.clip_start
        start_offset = f"{self.clip_start:.2f}"
        duration_str = f"{duration:.2f}"

        logger.info(
            "CLIP: Extracting %s → %s (%.1fs–%.1fs)",
            os.path.basename(self.trimmed_video_path),
            os.path.basename(self.clip_output_path),
            self.clip_start,
            self.clip_end,
        )

        success = await trim_video(
            self.trimmed_video_path,
            self.clip_output_path,
            start_offset,
            duration_str,
        )

        if success:
            logger.info(
                "CLIP: Extracted clip %s", os.path.basename(self.clip_output_path)
            )
        else:
            logger.error("CLIP: Failed to extract clip %s", self.clip_output_path)

        return success

    def serialize(self) -> Dict[str, Any]:
        return {
            "task_type": self.task_type,
            "tag_id": self.tag_id,
            "clip_id": self.clip_id,
            "game_session_id": self.game_session_id,
            "group_dir": self.group_dir,
            "trimmed_video_path": self.trimmed_video_path,
            "clip_start": self.clip_start,
            "clip_end": self.clip_end,
            "clip_output_path": self.clip_output_path,
        }

    @classmethod
    def deserialize(cls, data: Dict[str, Any]) -> "ClipExtractionTask":
        return cls(
            tag_id=data["tag_id"],
            clip_id=data["clip_id"],
            game_session_id=data["game_session_id"],
            group_dir=data["group_dir"],
            trimmed_video_path=data["trimmed_video_path"],
            clip_start=float(data["clip_start"]),
            clip_end=float(data["clip_end"]),
            clip_output_path=data["clip_output_path"],
        )

    def __str__(self) -> str:
        return (
            f"ClipExtractionTask(tag={self.tag_id[:8]}, "
            f"{self.clip_start:.1f}–{self.clip_end:.1f}s)"
        )
