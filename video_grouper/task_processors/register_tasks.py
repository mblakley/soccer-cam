"""Register task types with the task registry.

The service and tray bundles need different ball-tracking task classes:
the service runs the homegrown ML pipeline (PyAV / ONNX / CV2) while the
tray spawns the external AutoCam GUI. Mixing both into one registration
function would force the tray to import ``av``, which the tray bundle
deliberately excludes — see VideoGrouper.spec ``TRAY_EXCLUDES``.

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
    """Register tasks shared by every bundle (no heavy ML deps)."""
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
    """Service-side registration — includes both ball-tracking task types.

    The homegrown task pulls in PyAV at module-import time; that's expected
    in the service bundle. Externally-driven tasks are registered too
    because a service-only deploy on Linux/Docker can still drive a remote
    AutoCam-equivalent provider via the same task class.
    """
    _register_common_tasks()
    # Lazy-import inside the function so importing this module doesn't
    # drag av into callers that only need register_tray_tasks.
    from .tasks.ball_tracking.ball_tracking_task import BallTrackingTask
    from .tasks.ball_tracking.external_ball_tracking_task import (
        ExternalBallTrackingTask,
    )

    task_registry.register_task(BallTrackingTask)
    task_registry.register_task(ExternalBallTrackingTask)


def register_tray_tasks() -> None:
    """Tray-side registration — external ball-tracking only.

    The tray bundle excludes onnxruntime / cv2 / av / googleapiclient
    (see VideoGrouper.spec ``TRAY_EXCLUDES``), so the homegrown task
    cannot be loaded here even on demand. The autocam_gui provider runs
    the external AutoCam process via pywinauto and needs no inference
    stack of its own.
    """
    _register_common_tasks()
    from .tasks.ball_tracking.external_ball_tracking_task import (
        ExternalBallTrackingTask,
    )

    task_registry.register_task(ExternalBallTrackingTask)


# Back-compat alias. Older call sites assume one global registration that
# covers everything; preserve that behaviour by routing to the service set,
# which is the broadest. New entry points should pick the explicit form.
def register_all_tasks() -> None:
    register_service_tasks()
