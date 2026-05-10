"""Abstract base for ball-tracking tasks.

Two concrete implementations share this interface:

* :class:`BallTrackingTask` (homegrown ML pipeline) — uses PyAV / ONNX Runtime
  / OpenCV, runs in the service bundle.
* :class:`ExternalBallTrackingTask` (autocam_gui) — spawns an external GUI
  process, runs in the tray bundle.

The base imports nothing from ``av`` / ``cv2`` / ``onnxruntime`` so it is
safe to load in the tray PyInstaller target, which excludes the inference
stack to avoid a DLL initialization conflict between onnxruntime's pybind11
.pyd and PyQt6's bundled MSVCP140.dll.
"""

from __future__ import annotations

from abc import abstractmethod
from pathlib import Path
from typing import Any, Dict

from ..base_task import BaseTask
from ...queue_type import QueueType


class BallTrackingTaskBase(BaseTask):
    """Shared interface + serialization for ball-tracking tasks.

    Subclasses pick their dependency footprint and own the mechanics of
    running a single video through their provider. The base owns field
    layout and queue persistence so the two implementations can't drift.
    """

    def __init__(
        self,
        group_dir: Path,
        input_path: str,
        output_path: str,
        provider_name: str,
        provider_config: Dict[str, Any],
        team_name: str | None = None,
        storage_path: str | None = None,
        ttt_config: Dict[str, Any] | None = None,
    ):
        self.group_dir = group_dir
        self.input_path = input_path
        self.output_path = output_path
        self.provider_name = provider_name
        self.provider_config = provider_config
        self.team_name = team_name
        self.storage_path = storage_path
        self.ttt_config = ttt_config

    @classmethod
    def queue_type(cls) -> QueueType:
        return QueueType.BALL_TRACKING

    def get_item_path(self) -> str:
        return str(self.group_dir)

    def serialize(self) -> Dict[str, object]:
        return {
            "task_type": self.task_type,
            "group_dir": str(self.group_dir),
            "input_path": self.input_path,
            "output_path": self.output_path,
            "provider_name": self.provider_name,
            "provider_config": dict(self.provider_config),
            "team_name": self.team_name,
            "storage_path": self.storage_path,
            "ttt_config": dict(self.ttt_config) if self.ttt_config else None,
        }

    @classmethod
    def deserialize(cls, data: Dict[str, object]) -> "BallTrackingTaskBase":
        ttt_cfg = data.get("ttt_config")
        return cls(
            group_dir=Path(data["group_dir"]),
            input_path=data["input_path"],
            output_path=data["output_path"],
            provider_name=data["provider_name"],
            provider_config=dict(data.get("provider_config") or {}),
            team_name=data.get("team_name"),
            storage_path=data.get("storage_path"),
            ttt_config=dict(ttt_cfg) if ttt_cfg else None,
        )

    @abstractmethod
    def _validate_video_file(self, path: str) -> bool:
        """Verify the source file is usable before invoking the provider."""

    @abstractmethod
    async def execute(self) -> bool:
        """Run the configured provider against ``input_path`` -> ``output_path``."""

    def __str__(self) -> str:
        return (
            f"{type(self).__name__}(group_dir={self.group_dir}, "
            f"provider={self.provider_name}, input={self.input_path})"
        )

    def __eq__(self, other: object) -> bool:
        if type(self) is not type(other):
            return False
        return (
            self.group_dir == other.group_dir
            and self.input_path == other.input_path
            and self.output_path == other.output_path
            and self.provider_name == other.provider_name
            and self.provider_config == other.provider_config
        )

    def __hash__(self) -> int:
        cfg_hashable = (
            tuple(sorted(self.provider_config.items())) if self.provider_config else ()
        )
        return hash(
            (
                type(self).__name__,
                self.group_dir,
                self.input_path,
                self.output_path,
                self.provider_name,
                cfg_hashable,
            )
        )

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "BallTrackingTaskBase":
        return cls.deserialize(data)
