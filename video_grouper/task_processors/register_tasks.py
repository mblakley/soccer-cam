"""
Register all task types with the task registry.
"""

from .task_registry import task_registry

# Import all task classes
from .tasks.download.dahua_download_task import DahuaDownloadTask
from .tasks.video.combine_task import CombineTask
from .tasks.video.trim_task import TrimTask
from .tasks.autocam.autocam_task import AutocamTask
from .tasks.upload.youtube_upload_task import YoutubeUploadTask


def register_all_tasks():
    """Register all task types with the task registry."""
    task_registry.register_task(DahuaDownloadTask)
    task_registry.register_task(CombineTask)
    task_registry.register_task(TrimTask)
    task_registry.register_task(AutocamTask)
    task_registry.register_task(YoutubeUploadTask) 