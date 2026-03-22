from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple, TypedDict
from datetime import datetime


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
    ) -> List[Dict[str, Any]]:
        """Get list of recording files from the camera."""
        pass

    @abstractmethod
    async def get_file_size(self, file_path: str) -> int:
        """Get size of a file on the camera."""
        pass

    @abstractmethod
    async def download_file(self, remote_path: str, local_path: str) -> bool:
        """Download a file from the camera."""
        pass

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

    async def delete_files(self, file_paths: List[str]) -> int:
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
    def get_connected_timeframes(self) -> List[Tuple[datetime, Optional[datetime]]]:
        """Returns a list of timeframes when the camera was connected.

        Each timeframe is a tuple of (start_time, end_time), where end_time is None
        if the camera is currently connected (i.e., the connection is ongoing).

        Returns:
            List of tuples representing start and end times of connection periods
        """
        pass

    @property
    @abstractmethod
    def connection_events(self) -> List[Tuple[datetime, str]]:
        """Get list of connection events."""
        pass

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Get connection status."""
        pass
