"""
Enums for NTFY task types and statuses.
"""

from enum import Enum


class NtfyInputType(Enum):
    """Enum for NTFY input types (task types)."""

    TEAM_INFO = "team_info"
    GAME_START_TIME = "game_start_time"
    GAME_END_TIME = "game_end_time"
    PLAYLIST_NAME = "playlist_name"


class NtfyStatus(Enum):
    """Enum for NTFY task statuses."""

    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
