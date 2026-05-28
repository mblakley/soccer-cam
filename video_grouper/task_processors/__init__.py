"""Task processors package for video grouper application."""

from .ball_tracking_discovery_processor import BallTrackingDiscoveryProcessor
from .ball_tracking_processor import BallTrackingProcessor
from .base_polling_processor import PollingProcessor
from .base_queue_processor import QueueProcessor
from .camera_poller import CameraPoller
from .clip_discovery_processor import ClipDiscoveryProcessor
from .clip_processor import ClipProcessor
from .download_processor import DownloadProcessor
from .ntfy_processor import NtfyProcessor
from .state_auditor import StateAuditor
from .ttt_job_processor import TTTJobProcessor
from .upload_processor import UploadProcessor
from .video_processor import VideoProcessor

__all__ = [
    "PollingProcessor",
    "QueueProcessor",
    "CameraPoller",
    "DownloadProcessor",
    "NtfyProcessor",
    "StateAuditor",
    "UploadProcessor",
    "VideoProcessor",
    "BallTrackingProcessor",
    "BallTrackingDiscoveryProcessor",
    "ClipProcessor",
    "ClipDiscoveryProcessor",
    "TTTJobProcessor",
]
