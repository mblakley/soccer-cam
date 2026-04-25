"""
Register all task types with the task registry.
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
from .tasks.ball_tracking.ball_tracking_task import BallTrackingTask

from video_grouper.task_processors.task_registry import task_registry


def register_all_tasks():
    """Register all task types with the task registry."""
    task_registry.register_task(CombineTask)
    task_registry.register_task(TrimTask)
    task_registry.register_task(GameStartTask)
    task_registry.register_task(GameEndTask)
    task_registry.register_task(TeamInfoTask)
    task_registry.register_task(YoutubeUploadTask)
    task_registry.register_task(ClipRequestTask)
    task_registry.register_task(ClipExtractionTask)
    task_registry.register_task(HighlightCompilationTask)
    task_registry.register_task(BallTrackingTask)
