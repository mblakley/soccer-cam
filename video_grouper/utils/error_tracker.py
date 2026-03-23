"""In-memory error ring buffer.

Tracks recent errors for local display (tray UI) and TTT reporting.
Valuable independently of TTT — the tray app can query this to show
recent errors without checking log files."""

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ErrorEntry:
    """A single error record."""

    stage: str  # download, combine, trim, autocam, upload
    message: str
    context: dict = field(default_factory=dict)  # recording_id, file_name, etc.
    timestamp: float = field(default_factory=time.time)

    @property
    def age_seconds(self) -> float:
        return time.time() - self.timestamp

    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "message": self.message,
            "context": self.context,
            "timestamp": self.timestamp,
        }


class ErrorTracker:
    """Thread-safe ring buffer of recent errors.

    Usage:
        tracker = ErrorTracker(max_errors=100)
        tracker.record("download", "Connection timeout", {"camera_ip": "192.168.1.100"})
        print(tracker.get_last_error())
        print(tracker.get_error_count_24h())
    """

    def __init__(self, max_errors: int = 100):
        self._errors: deque[ErrorEntry] = deque(maxlen=max_errors)
        self._lock = threading.Lock()

    def record(self, stage: str, message: str, context: dict | None = None) -> None:
        """Record a new error."""
        entry = ErrorEntry(stage=stage, message=message, context=context or {})
        with self._lock:
            self._errors.append(entry)
        logger.debug(f"Error recorded: [{stage}] {message}")

    def get_last_error(self) -> str | None:
        """Get the most recent error message, or None if no errors."""
        with self._lock:
            if self._errors:
                last = self._errors[-1]
                return f"[{last.stage}] {last.message}"
            return None

    def get_error_count_24h(self) -> int:
        """Count errors in the last 24 hours."""
        cutoff = time.time() - 86400
        with self._lock:
            return sum(1 for e in self._errors if e.timestamp >= cutoff)

    def get_recent_errors(self, limit: int = 20) -> list[dict]:
        """Get recent errors as dicts (for API/display)."""
        with self._lock:
            errors = list(self._errors)
        errors.reverse()  # Newest first
        return [e.to_dict() for e in errors[:limit]]

    def clear(self) -> None:
        """Clear all errors."""
        with self._lock:
            self._errors.clear()
