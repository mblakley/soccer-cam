"""Task implementations for the video processing system."""

# Base classes
from .base_task import BaseTask
from .queue_type import QueueType

# Download tasks
from .download import (
    BaseDownloadTask,
    DahuaDownloadTask,
)

# Upload tasks
from .upload import (
    BaseUploadTask,
    YoutubeUploadTask,
)

# Video tasks
from .video import (
    BaseFfmpegTask,
    ConvertTask,
    CombineTask,
    TrimTask,
)

__all__ = [
    # Base classes
    "BaseTask",
    "QueueType",
    # Download tasks
    "BaseDownloadTask",
    "DahuaDownloadTask",
    # Upload tasks
    "BaseUploadTask",
    "YoutubeUploadTask",
    # Video tasks
    "BaseFfmpegTask",
    "ConvertTask",
    "CombineTask",
    "TrimTask",
    # Utility functions
    "task_from_dict",
]


def task_from_dict(data: dict):
    """Create a task from a serialized dictionary - supports all task types."""
    task_type = data.get("task_type", "")

    # Video tasks
    if task_type == "convert":
        return ConvertTask.from_dict(data)
    elif task_type == "combine":
        return CombineTask.from_dict(data)
    elif task_type == "trim":
        return TrimTask.from_dict(data)

    # Upload tasks
    elif task_type == "youtube_upload":
        return YoutubeUploadTask.from_dict(data)

    # Download tasks
    elif task_type == "dahua_download":
        return DahuaDownloadTask.from_dict(data)

    else:
        import logging

        logger = logging.getLogger(__name__)
        logger.warning(f"Unknown task type in task_from_dict: {task_type}")
        return None
