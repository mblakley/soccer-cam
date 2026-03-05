"""
Queue type enumeration for task routing.
"""

from enum import Enum


class QueueType(Enum):
    """Enumeration of queue types for different task processors."""

    DOWNLOAD = "download"
    VIDEO = "video"
    UPLOAD = "upload"
    NTFY = "ntfy"
    YOUTUBE = "youtube"
    AUTOCAM = "autocam"
    CLIP_REQUEST = "clip_request"
