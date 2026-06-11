"""The work item for the config-driven pipeline.

A :class:`PipelineTask` names one game (its ``group_dir``) and carries the
shared-data ``storage_path``; the per-group ``pipeline_state.json`` manifest
owns all per-step progress, so the task itself stays tiny and resumable. The
:class:`~video_grouper.task_processors.pipeline_processor.PipelineProcessor`
resolves the configured steps and runs them via the
:class:`~video_grouper.pipeline.runner.PipelineRunner`.

This module imports nothing from ``av`` / ``onnxruntime`` / ``cv2`` at module
top, so it is safe to register in the tray bundle (which excludes the inference
stack). The heavy step modules are imported lazily by the runner / processor.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from ...queue_type import QueueType
from ..base_task import BaseTask


class PipelineTask(BaseTask):
    """One game enqueued for the config-driven pipeline.

    ``input_path`` / ``output_path`` are the trimmed source and broadcast output
    for this game (the same paths the legacy ball-tracking discovery derived via
    ``get_ball_tracking_io_paths``). ``team_name`` / ``ttt_config`` feed the
    :class:`~video_grouper.pipeline.base.StepContext` the runner builds.
    """

    def __init__(
        self,
        group_dir: Path,
        input_path: str,
        output_path: str,
        team_name: str | None = None,
        storage_path: str | None = None,
        ttt_config: dict[str, object] | None = None,
    ):
        self.group_dir = group_dir
        self.input_path = input_path
        self.output_path = output_path
        self.team_name = team_name
        self.storage_path = storage_path
        self.ttt_config = ttt_config

    @classmethod
    def queue_type(cls) -> QueueType:
        return QueueType.PIPELINE

    @property
    def task_type(self) -> str:
        return "pipeline"

    def get_item_path(self) -> str:
        return str(self.group_dir)

    def serialize(self) -> dict[str, object]:
        return {
            "task_type": self.task_type,
            "group_dir": str(self.group_dir),
            "input_path": self.input_path,
            "output_path": self.output_path,
            "team_name": self.team_name,
            "storage_path": self.storage_path,
            "ttt_config": dict(self.ttt_config) if self.ttt_config else None,
        }

    @classmethod
    def deserialize(cls, data: dict[str, object]) -> PipelineTask:
        # data is the serialized form produced by serialize(); the value types
        # are known from that schema but typed as `object` in the dict, so cast
        # each field to the type the constructor expects.
        ttt_cfg = cast("dict[str, object] | None", data.get("ttt_config"))
        return cls(
            group_dir=Path(cast(str, data["group_dir"])),
            input_path=cast(str, data["input_path"]),
            output_path=cast(str, data["output_path"]),
            team_name=cast("str | None", data.get("team_name")),
            storage_path=cast("str | None", data.get("storage_path")),
            ttt_config=dict(ttt_cfg) if ttt_cfg else None,
        )

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> PipelineTask:
        return cls.deserialize(data)

    async def execute(self) -> bool:
        """No-op — the PipelineProcessor drives the runner directly.

        The pipeline's unit of work is the manifest-resumable run owned by the
        processor (it needs the resolved step specs + resource manager), so the
        task carries data only. ``BaseTask`` requires ``execute``; returning
        ``True`` keeps the contract satisfied for any generic caller.
        """
        return True

    def __str__(self) -> str:
        return f"PipelineTask(group_dir={self.group_dir}, input={self.input_path})"

    def __eq__(self, other: object) -> bool:
        if type(self) is not type(other):
            return False
        return (
            self.group_dir == other.group_dir
            and self.input_path == other.input_path
            and self.output_path == other.output_path
        )

    def __hash__(self) -> int:
        return hash(
            (
                type(self).__name__,
                self.group_dir,
                self.input_path,
                self.output_path,
            )
        )
