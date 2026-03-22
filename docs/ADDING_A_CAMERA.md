# Adding a New Camera Type

Soccer-cam uses a modular camera system. Each camera type is a self-contained Python module that implements the `Camera` abstract base class and registers itself with the camera registry.

## Overview

Adding a new camera requires changes to exactly 3 files:
1. Your new camera module: `video_grouper/cameras/yourcamera.py`
2. Import registration: `video_grouper/video_grouper_app.py` (one line)
3. Tests: `tests/test_yourcamera.py`

## Step-by-Step

### Step 1: Create Your Camera Module

Create `video_grouper/cameras/yourcamera.py`:

```python
import logging
from datetime import datetime
from typing import List, Tuple, Dict, Any, Optional

from .base import Camera, DeviceInfo
from . import register_camera
from video_grouper.utils.config import CameraConfig

logger = logging.getLogger(__name__)


class YourCamera(Camera):
    """YourBrand camera implementation."""

    def __init__(self, config: CameraConfig, storage_path: str, client=None):
        self.config = config
        self.storage_path = storage_path
        self.device_ip = config.device_ip
        self.username = config.username
        self.password = config.password
        self._is_connected = False
        self._connection_events = []

    async def check_availability(self) -> bool:
        """Check if the camera is reachable on the network."""
        # Make an HTTP request to your camera's API
        # Return True if successful, False otherwise
        # Track connection state changes:
        #   self._is_connected = True/False
        #   self._connection_events.append((datetime.now(), "connected"/"disconnected"))
        ...

    async def get_file_list(
        self, start_time: datetime, end_time: datetime
    ) -> List[Dict[str, Any]]:
        """Return recordings in the given time range.

        Each dict must contain:
          - "path": remote file path on camera
          - "startTime": "YYYY-MM-DD HH:MM:SS"
          - "endTime": "YYYY-MM-DD HH:MM:SS"
        """
        ...

    async def get_file_size(self, file_path: str) -> int:
        """Return file size in bytes. Return 0 if unknown."""
        ...

    async def download_file(self, remote_path: str, local_path: str) -> bool:
        """Download a file from the camera to local_path.

        Must:
          - Create parent directories if needed
          - Clean up partial files on failure
          - Return True on success, False on failure
        """
        ...

    async def stop_recording(self) -> bool:
        """Disable recording on the camera. Return True on success."""
        ...

    async def get_recording_status(self) -> bool:
        """Return True if the camera is currently recording."""
        ...

    async def get_device_info(self) -> DeviceInfo:
        """Return device metadata."""
        return DeviceInfo(
            device_name="",
            device_type="YourBrand",
            firmware_version="",
            serial_number="",
            ip_address=self.device_ip,
            mac_address="",
            model="",
            manufacturer="YourBrand",
        )

    def get_connected_timeframes(self) -> List[Tuple[datetime, Optional[datetime]]]:
        """Return list of (start, end) tuples for when the camera was connected.
        end=None means currently connected."""
        # Build from self._connection_events
        ...

    @property
    def connection_events(self) -> List[Tuple[datetime, str]]:
        return self._connection_events

    @property
    def is_connected(self) -> bool:
        return self._is_connected


# Register with the camera registry -- this line is required!
register_camera("yourcamera", YourCamera)
```

### Step 2: Register the Import

In `video_grouper/video_grouper_app.py`, find the `_create_camera` method and add your import alongside the existing ones:

```python
import video_grouper.cameras.dahua  # noqa: F401
import video_grouper.cameras.reolink  # noqa: F401
import video_grouper.cameras.yourcamera  # noqa: F401  # <-- add this
```

### Step 3: Configure

In your `config.ini`, set the camera type to match your registered name:

```ini
[CAMERA.mycam]
type = yourcamera
device_ip = 192.168.1.100
username = admin
password = admin
```

### Step 4: Write Tests

Create `tests/test_yourcamera.py`. Key test areas:
- `check_availability()` returns True/False correctly
- `get_file_list()` returns properly formatted dicts
- `download_file()` creates the local file and returns True
- `download_file()` cleans up on failure and returns False
- Connection event tracking works
- Error handling for network timeouts

See `tests/test_dahua_camera.py` for reference (comprehensive test patterns).

## Camera Config

The `CameraConfig` model provides these fields:
- `name`: Camera name (from config section name, e.g., "mycam")
- `type`: Camera type string (must match your `register_camera()` call)
- `device_ip`: IP address
- `username`: Login username
- `password`: Login password
- `channel`: Channel number (default 0)
- `baichuan_port`: Reolink-specific, can be ignored for other cameras

If your camera needs additional config fields, add them to `CameraConfig` in `video_grouper/utils/config.py` with sensible defaults so existing configs aren't broken.

## Key Contracts

### File List Format
`get_file_list()` must return a list of dicts with at least:
```python
{"path": "/mnt/sd/2026-03-22/game.mp4", "startTime": "2026-03-22 09:00:00", "endTime": "2026-03-22 10:30:00"}
```
The path is used to download the file. The times are used to group recordings.

### Download Contract
- The caller provides `remote_path` (from `get_file_list()`) and `local_path` (where to save)
- You must handle creating parent directories
- On failure, delete any partial file at `local_path` and return False
- The pipeline retries failed downloads up to 3 times

### Connection State
- Track `_is_connected` and `_connection_events` so the pipeline knows when the camera is available
- `get_connected_timeframes()` is used to filter out "home recordings" -- only files within connected timeframes are processed

## Optional Overrides

These methods have default implementations in the base class:
- `start_recording()`: Default returns True (no-op). Override if your camera needs explicit recording start.
- `supports_file_deletion` property: Default False. Set True and implement `delete_files()` if supported.
- `delete_files(file_paths)`: Default returns 0. Implement if camera supports deleting recordings.
