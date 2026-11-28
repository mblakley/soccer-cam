import errno
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, TypedDict

import httpx


class CameraUnreachableError(Exception):
    """Raised when a download fails because the camera is offline.

    The queue processor's retry loop catches this specifically and
    requeues the item WITHOUT incrementing the per-item retry counter
    — camera-unreachable is a global state, not the file's fault, and
    the file should be tried again after a backoff once the camera is
    back. Burning 3 strikes per file during a 30-minute camera unplug
    would mark every queued file terminally failed: exactly the bug
    we hit on 2026-05-09 when 10 of a 19-file game's recordings got
    stuck `download_failed` and CameraPoller's high-water mark
    advanced past them so they were never rediscovered.
    """


def describe_download_error(exc: BaseException) -> str:
    """A download exception as a short phrase a person can act on.

    Names the two causes people can do something about -- the disk filling
    up and a timeout -- in plain words, and otherwise keeps the exception's
    own message (plus its type, which is often the only clue an empty
    ``ConnectionResetError()`` gives).
    """
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return f"local disk full while writing the download ({exc})"
    if isinstance(exc, TimeoutError | httpx.TimeoutException):
        detail = str(exc)
        return f"timed out ({detail})" if detail else "timed out"
    detail = str(exc)
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


class DeviceInfo(TypedDict):
    """Represents device information from a camera."""

    device_name: str
    device_type: str
    firmware_version: str
    serial_number: str
    ip_address: str
    mac_address: str
    model: str
    manufacturer: str


class ConfigResult(TypedDict):
    """Result of reading or applying a single camera configuration setting."""

    setting: str  # "recording", "ntp", "encoding"
    success: bool
    current_value: str  # Human-readable current state
    applied_value: str  # What was applied (or proposed)
    error: str  # Empty string if no error


class Camera(ABC):
    """Base class for camera implementations.

    To add a new camera type, subclass this and implement all abstract methods.
    Then register it with the camera registry::

        from video_grouper.cameras import register_camera
        register_camera("mytype", MyCameraClass)

    Constructor signature: ``__init__(self, config: CameraConfig, storage_path: str, client=None)``

    - *config*: A :class:`~video_grouper.utils.config.CameraConfig` instance.
    - *storage_path*: Root directory for downloaded videos.
    - *client*: Optional HTTP client for dependency injection in tests.

    See ``docs/ADDING_A_CAMERA.md`` for a full walkthrough.
    """

    @property
    def name(self) -> str:
        """Get the camera name from config."""
        return self.config.name

    @abstractmethod
    async def check_availability(self) -> bool:
        """Check if the camera is available."""
        pass

    @abstractmethod
    async def get_file_list(
        self, start_time: datetime = None, end_time: datetime = None
    ) -> list[dict[str, Any]]:
        """Get list of recording files from the camera."""
        pass

    @abstractmethod
    async def get_file_size(self, file_path: str) -> int:
        """Get size of a file on the camera."""
        pass

    #: Why the most recent ``download_file`` call failed, as a short phrase a
    #: person deciding whether to give up on the footage can act on ("HTTP 500
    #: from the camera", "disk full ..."). None before any download and after
    #: a success. ``download_file`` keeps returning a bare bool for its
    #: callers; this is the side channel for the reason, which
    #: DownloadProcessor stores on the file for the dashboard.
    last_download_error: str | None = None

    @abstractmethod
    async def download_file(self, remote_path: str, local_path: str) -> bool:
        """Download a file from the camera.

        Implementations clear :attr:`last_download_error` when they start and
        set it (via :meth:`_download_failed`) on every path that returns
        False.
        """
        pass

    def _download_failed(self, reason: str) -> bool:
        """Record why this download failed and return False, for
        ``return self._download_failed("...")`` on a failure path."""
        self.last_download_error = reason
        return False

    @abstractmethod
    async def stop_recording(self) -> bool:
        """Stop recording on the camera."""
        pass

    async def start_recording(self) -> bool:
        """Re-enable recording on the camera.

        Called before sending the unplug notification so the camera
        is ready to record at the field.  Default is a no-op (cameras
        that auto-record on power-on don't need this).
        """
        return True

    @abstractmethod
    async def get_recording_status(self) -> bool:
        """Get recording status from the camera."""
        pass

    @property
    def supports_file_deletion(self) -> bool:
        """Whether this camera supports programmatic file deletion."""
        return False

    async def delete_files(self, file_paths: list[str]) -> int:
        """Delete recording files from the camera's storage.

        Not all cameras support this. Check supports_file_deletion first.

        Args:
            file_paths: List of remote file paths to delete.

        Returns:
            Number of files successfully deleted.
        """
        return 0

    @abstractmethod
    async def get_device_info(self) -> DeviceInfo:
        """Get device information from the camera."""
        pass

    @abstractmethod
    def get_connected_timeframes(self) -> list[tuple[datetime, datetime | None]]:
        """Returns a list of timeframes when the camera was connected.

        Each timeframe is a tuple of (start_time, end_time), where end_time is None
        if the camera is currently connected (i.e., the connection is ongoing).

        Returns:
            List of tuples representing start and end times of connection periods
        """
        pass

    @property
    @abstractmethod
    def connection_events(self) -> list[tuple[datetime, str]]:
        """Get list of connection events."""
        pass

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Get connection status."""
        pass

    # ── Configuration push (optional) ─────────────────────────────────

    async def get_current_settings(self) -> list["ConfigResult"]:
        """Read current camera configuration for display.

        Returns a list of ConfigResult dicts describing the current state
        of each configurable setting (recording, NTP, encoding).
        """
        return []

    async def apply_optimal_settings(self, timezone: str = "") -> list["ConfigResult"]:
        """Push optimal settings for 24/7 soccer recording.

        Args:
            timezone: IANA timezone string (e.g. "America/New_York").
                      Used to configure camera clock/NTP.

        Returns a list of ConfigResult dicts with per-setting success/failure.
        """
        return []

    async def change_camera_password(
        self, current_password: str, new_password: str
    ) -> bool:
        """Change the camera's login password.

        Args:
            current_password: The current password (needed by some cameras).
            new_password: The desired new password.

        Returns True on success, False on failure or if unsupported.
        """
        return False
