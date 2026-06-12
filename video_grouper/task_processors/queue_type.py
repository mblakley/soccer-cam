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
    BALL_TRACKING = "ball_tracking"
    PIPELINE = "pipeline"
    CLIP_REQUEST = "clip_request"
    CLIPS = "clips"
    # TTT cloud features — each polled into a QueueProcessor by TTTPoller.
    TTT_HIGHLIGHT_REEL = "ttt_highlight_reel"
    TTT_JOB = "ttt_job"
    TTT_REPROCESS_REQUEST = "ttt_reprocess_request"
