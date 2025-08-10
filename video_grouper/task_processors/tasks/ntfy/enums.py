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
    WAS_THERE_A_MATCH = "was_there_a_match"


class NtfyStatus(Enum):
    """Enum for NTFY task statuses."""

    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    FAILED_TO_SEND = "failed_to_send"
    CANCELLED = "cancelled"
