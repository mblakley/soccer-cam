"""Pipeline task package.

Houses :class:`PipelineTask` — the work item for the config-driven pipeline
(``QueueType.PIPELINE``). Kept import-light (no ``av`` / ``onnxruntime`` /
``cv2`` at module top) so the tray bundle can register it without dragging in
the inference stack — the per-step manifest carries all heavy state, and the
:class:`~video_grouper.pipeline.runner.PipelineRunner` constructs steps lazily.
"""

from __future__ import annotations

from .pipeline_task import PipelineTask

__all__ = ["PipelineTask"]
