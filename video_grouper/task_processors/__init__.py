"""Task processors package for video grouper application."""

from .base_polling_processor import PollingProcessor
from .base_queue_processor import QueueProcessor
from .camera_poller import CameraPoller
from .download_processor import DownloadProcessor
from .ntfy_processor import NtfyProcessor
from .state_auditor import StateAuditor
from .upload_processor import UploadProcessor
from .video_processor import VideoProcessor
from .autocam_processor import AutocamProcessor
from .autocam_discovery_processor import AutocamDiscoveryProcessor

__all__ = [
    "PollingProcessor",
    "QueueProcessor",
    "CameraPoller",
    "DownloadProcessor",
    "NtfyProcessor",
    "StateAuditor",
    "UploadProcessor",
    "VideoProcessor",
    "AutocamProcessor",
    "AutocamDiscoveryProcessor",
]
