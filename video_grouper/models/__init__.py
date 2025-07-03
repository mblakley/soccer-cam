"""
Models package for VideoGrouper.

This package contains all the data models used throughout the application.
"""

# Core models
from .connection_event import ConnectionEvent
from .match_info import MatchInfo
from .recording_file import RecordingFile
from .directory_state import DirectoryState

# Task classes from task processors
from video_grouper.task_processors.tasks.video import (
    BaseFfmpegTask,
    ConvertTask,
    CombineTask,
    TrimTask,
)
from video_grouper.task_processors.tasks.upload import (
    BaseUploadTask,
    YoutubeUploadTask,
)
from video_grouper.task_processors.tasks.download import (
    BaseDownloadTask,
    DahuaDownloadTask,
)

# Aliases for backward compatibility with existing code
FFmpegTask = BaseFfmpegTask
VideoUploadTask = YoutubeUploadTask

__all__ = [
    # Core models
    "ConnectionEvent",
    "MatchInfo",
    "RecordingFile",
    "DirectoryState",
    # Task classes
    "BaseFfmpegTask",
    "FFmpegTask",  # alias
    "ConvertTask",
    "CombineTask",
    "TrimTask",
    "BaseUploadTask",
    "YoutubeUploadTask",
    "VideoUploadTask",  # alias
    "BaseDownloadTask",
    "DahuaDownloadTask",
]
