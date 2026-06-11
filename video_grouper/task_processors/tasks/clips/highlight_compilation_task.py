"""
Task for compiling multiple clips into a highlight reel using FFmpeg concat.
"""

import logging
import os
from dataclasses import dataclass
from typing import Any

from video_grouper.utils.ffmpeg_utils import combine_videos

from ...queue_type import QueueType
from ..base_task import BaseTask

logger = logging.getLogger(__name__)


@dataclass(unsafe_hash=True)
class HighlightCompilationTask(BaseTask):
    """Concatenate clip files into a single highlight reel video."""

    highlight_id: str
    title: str
    player_name: str
    clip_local_paths: tuple  # tuple for hashability
    output_dir: str

    @classmethod
    def queue_type(cls) -> QueueType:
        return QueueType.CLIPS

    @property
    def task_type(self) -> str:
        return "highlight_compilation"

    def get_item_path(self) -> str:
        return self.output_path

    @property
    def output_path(self) -> str:
        safe_title = "".join(
            c if c.isalnum() or c in " _-" else "_" for c in self.title
        )
        return os.path.join(self.output_dir, f"{safe_title}.mp4")

    async def execute(self) -> bool:
        """Concatenate clip files into the final highlight reel via PyAV."""
        os.makedirs(self.output_dir, exist_ok=True)

        logger.info(
            "HIGHLIGHT: Compiling %d clips into %s",
            len(self.clip_local_paths),
            os.path.basename(self.output_path),
        )

        success = await combine_videos(list(self.clip_local_paths), self.output_path)

        if success:
            logger.info("HIGHLIGHT: Compiled %s", os.path.basename(self.output_path))
        else:
            logger.error("HIGHLIGHT: Failed to compile %s", self.output_path)

        return success

    def serialize(self) -> dict[str, Any]:
        return {
            "task_type": self.task_type,
            "highlight_id": self.highlight_id,
            "title": self.title,
            "player_name": self.player_name,
            "clip_local_paths": list(self.clip_local_paths),
            "output_dir": self.output_dir,
        }

    @classmethod
    def deserialize(cls, data: dict[str, Any]) -> "HighlightCompilationTask":
        return cls(
            highlight_id=data["highlight_id"],
            title=data["title"],
            player_name=data["player_name"],
            clip_local_paths=tuple(data["clip_local_paths"]),
            output_dir=data["output_dir"],
        )

    def __str__(self) -> str:
        return (
            f"HighlightCompilationTask(id={self.highlight_id[:8]}, "
            f"{len(self.clip_local_paths)} clips)"
        )
