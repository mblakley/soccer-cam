"""Base contract every pipeline step must implement.

A *step* is the only unit of the processing pipeline. The pipeline is an
ordered list of step instances, resolved from config and run by
:class:`~video_grouper.pipeline.runner.PipelineRunner`. Steps hand state to
one another through the filesystem, recording the paths they produce in the
:class:`~video_grouper.pipeline.manifest.PipelineManifest`.

This module deliberately imports nothing from the rest of ``video_grouper``
beyond pydantic so it stays cheap to import in every bundle (the tray bundle
must be able to read step metadata without dragging in the ONNX/cv2 stack).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from video_grouper.pipeline.manifest import PipelineManifest


@dataclass
class StepContext:
    """Runtime context passed to :meth:`PipelineStep.run`.

    Promoted from the old ``ball_tracking.ProviderContext``.

    Attributes:
        group_dir: Absolute path to the game's video-group directory. The
            manifest and all per-step artifacts live here.
        team_name: Team identifier (e.g. ``"flash"``) or ``None`` if unknown.
        storage_path: Absolute path to soccer-cam's shared-data root.
        ttt_config: Dict-form ``TTTConfig`` (or ``None`` when TTT is disabled).
            Steps that acquire a TTT-licensed asset read credentials here. Kept
            a plain dict so this module stays free of upstream config imports.
    """

    group_dir: Path
    team_name: str | None
    storage_path: Path
    ttt_config: dict | None = None


class PipelineStep(ABC):
    """One step in the processing pipeline.

    Concrete steps register themselves at import time via
    :func:`video_grouper.pipeline.register_step` and set the class-level
    declarations below. A step is constructed once per pipeline invocation
    with an already-validated instance of its own :attr:`config_model`.

    Class-level declarations:
        name: Registry key — the string that appears as ``type =`` in a
            ``[PIPELINE.<id>]`` config section (e.g. ``"detect"``).
        config_model: The step's own Pydantic config schema. The step owns and
            validates only its own fields; the runner hands it a validated
            instance.
        consumes / produces: Artifact keys this step reads from / writes to the
            manifest (e.g. ``("input_path",)`` -> ``("detections_path",)``). The
            runner validates ``consumes`` before running and ``produces`` after.
        runtime: Where the step may run — ``"service"`` (Session-0 safe),
            ``"tray"`` (needs the interactive desktop), or ``"any"``.
        requires: Importable module names the step needs (e.g.
            ``("onnxruntime", "cv2")``). Used to decide bundle availability so a
            tray bundle lacking the inference stack greys the step out rather
            than crashing.
        resources: Named contention tags the scheduler serializes on (e.g.
            ``("gpu",)``, ``("autocam_ui",)``). Empty means freely parallel.
    """

    name: ClassVar[str]
    config_model: ClassVar[type[BaseModel]]
    consumes: ClassVar[tuple[str, ...]] = ()
    produces: ClassVar[tuple[str, ...]] = ()
    runtime: ClassVar[str] = "any"
    requires: ClassVar[tuple[str, ...]] = ()
    resources: ClassVar[tuple[str, ...]] = ()

    def __init__(self, config: BaseModel):
        self.config = config

    @abstractmethod
    async def run(self, manifest: "PipelineManifest", ctx: StepContext) -> bool:
        """Process this step.

        Read inputs from the manifest's artifact map (the keys named in
        :attr:`consumes`), write output files under ``ctx.group_dir``, and
        record produced paths via ``manifest.put(key, path)`` for the keys named
        in :attr:`produces`.

        Returns:
            ``True`` on success, ``False`` on an expected failure (log + return,
            don't raise). The runner treats both a ``False`` return and a raised
            exception as a failed step.
        """
