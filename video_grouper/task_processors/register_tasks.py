"""Register task types with the task registry.

All task types are import-light enough for both the service and the tray
bundle: the config-driven pipeline's work item (:class:`PipelineTask`) carries
only data (the per-group manifest owns step state, and the
:class:`~video_grouper.pipeline.runner.PipelineRunner` constructs steps lazily),
so no heavy ML dependency (``av`` / ``onnxruntime`` / ``cv2``) is imported at
registration time.

Entry points pick the right registration:

* Service / shared dashboard process -> :func:`register_service_tasks`
* Tray (Windows desktop session)     -> :func:`register_tray_tasks`
"""

from .tasks.video.combine_task import CombineTask
from .tasks.video.trim_task import TrimTask
from .tasks.ntfy.game_start_task import GameStartTask
from .tasks.ntfy.game_end_task import GameEndTask
from .tasks.ntfy.team_info_task import TeamInfoTask
from .tasks.upload.youtube_upload_task import YoutubeUploadTask
from .tasks.clip.clip_request_task import ClipRequestTask
from .tasks.clips.clip_extraction_task import ClipExtractionTask
from .tasks.clips.highlight_compilation_task import HighlightCompilationTask
from .tasks.pipeline.pipeline_task import PipelineTask

from video_grouper.task_processors.task_registry import task_registry


def _register_common_tasks() -> None:
    """Register every task type. None pull heavy ML deps at import time."""
    task_registry.register_task(CombineTask)
    task_registry.register_task(TrimTask)
    task_registry.register_task(GameStartTask)
    task_registry.register_task(GameEndTask)
    task_registry.register_task(TeamInfoTask)
    task_registry.register_task(YoutubeUploadTask)
    task_registry.register_task(ClipRequestTask)
    task_registry.register_task(ClipExtractionTask)
    task_registry.register_task(HighlightCompilationTask)
    # PipelineTask is import-light (the manifest carries step state and the
    # runner constructs steps lazily), so both the service and tray bundles
    # register it — the tray runs tray-runtime steps (e.g. autocam) from it.
    task_registry.register_task(PipelineTask)


def register_service_tasks() -> None:
    """Service-side registration."""
    _register_common_tasks()


def register_tray_tasks() -> None:
    """Tray-side registration.

    The tray bundle excludes onnxruntime / cv2 / av / googleapiclient
    (see VideoGrouper.spec ``TRAY_EXCLUDES``); every registered task here is
    import-light, and the pipeline runner only constructs the heavy steps
    lazily on the service side.
    """
    _register_common_tasks()


# Back-compat alias. Older call sites assume one global registration that
# covers everything; preserve that behaviour by routing to the service set,
# which is the broadest. New entry points should pick the explicit form.
def register_all_tasks() -> None:
    register_service_tasks()
