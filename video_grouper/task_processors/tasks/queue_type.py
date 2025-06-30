"""
Queue type enumeration for task routing.
"""

from enum import Enum


class QueueType(Enum):
    """
    Enumeration of queue types for task routing.

    This determines which processor a task should be routed to.
    """

    DOWNLOAD = "download"
    VIDEO = "video"
    UPLOAD = "upload"

    def __str__(self) -> str:
        """String representation of the queue type."""
        return self.value
