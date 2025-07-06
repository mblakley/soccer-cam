"""Task processors package for video grouper application."""

from .base_polling_processor import PollingProcessor
from .base_queue_processor import QueueProcessor
from .state_auditor import StateAuditor
from .camera_poller import CameraPoller
from .download_processor import DownloadProcessor
from .video_processor import VideoProcessor
from .upload_processor import UploadProcessor
from .ntfy_processor import NtfyProcessor

__all__ = [
    "PollingProcessor",
    "QueueProcessor",
    "StateAuditor",
    "CameraPoller",
    "DownloadProcessor",
    "VideoProcessor",
    "UploadProcessor",
    "NtfyProcessor",
]
