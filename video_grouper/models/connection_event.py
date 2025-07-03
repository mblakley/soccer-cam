"""
Connection event model for camera connection tracking.
"""

from typing import TypedDict


class ConnectionEvent(TypedDict):
    """Represents a single camera connection event."""

    event_datetime: str
    event_type: str  # "connected" or "disconnected"
    message: str
