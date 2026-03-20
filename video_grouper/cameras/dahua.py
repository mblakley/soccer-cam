import json
import os
import httpx
import logging
import time
import aiofiles
from datetime import datetime
from typing import List, Tuple, Dict, Any, Optional
import pytz

from .base import Camera, DeviceInfo
from video_grouper.models import ConnectionEvent
from video_grouper.utils.config import CameraConfig
from video_grouper.utils.paths import get_camera_state_path

# Default timeout for camera HTTP requests (30 seconds connect, 60 seconds read)
CAMERA_HTTP_TIMEOUT = httpx.Timeout(60.0, connect=30.0)

logger = logging.getLogger(__name__)


class DahuaCamera(Camera):
    """Dahua camera implementation."""

    def __init__(self, config: CameraConfig, storage_path: str, client=None):
        """Initialize the Dahua camera with configuration."""
        self.config = config
        self.storage_path = storage_path
        self.device_ip = config.device_ip
        self.username = config.username
        self.password = config.password
        self._state_file = get_camera_state_path(storage_path)
        self._connection_events: List[ConnectionEvent] = []
        self._is_connected = False
        self._log_dir = os.path.join(self.storage_path, "camera_http_logs")
        os.makedirs(self._log_dir, exist_ok=True)
        self._client = client
        self.logger = logging.getLogger(__name__)
        self._load_state()

    async def _log_http_call(
        self,
        name: str,
        request: httpx.Request,
        response: httpx.Response = None,
        error: Exception = None,
        stream_response: bool = False,
    ):
        """Logs the details of an HTTP request and its response to files if LOG_LEVEL is 'debug'."""
        if os.environ.get("LOG_LEVEL", "").lower() != "debug":
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")

        # Log Request
        req_filename = os.path.join(self._log_dir, f"{timestamp}_{name}_request.log")
        async with aiofiles.open(req_filename, "w") as f:
            await f.write(f"URL: {request.method} {request.url}\n")
            await f.write("Headers:\n")
            for key, value in request.headers.items():
                await f.write(f"  {key}: {value}\n")
            if request.content:
                await f.write("\nBody:\n")
                await f.write(request.content.decode("utf-8", errors="ignore"))

        # Log Response
        res_filename = os.path.join(self._log_dir, f"{timestamp}_{name}_response.log")
        async with aiofiles.open(res_filename, "w") as f:
            if response:
                await f.write(f"Status Code: {response.status_code}\n")
                await f.write("Headers:\n")
                for key, value in response.headers.items():
                    await f.write(f"  {key}: {value}\n")
                await f.write("\nBody:\n")
                # Handle streamed content differently
                if stream_response:
                    await f.write("[Streamed content not logged]")
                else:
                    await f.write(response.text)
            elif error:
                await f.write(f"Error: {type(error).__name__}\n")
                await f.write(str(error))

    def _load_state(self):
        """Load this camera's state from the shared state file."""
        try:
            if os.path.exists(self._state_file):
                with open(self._state_file, "r") as f:
                    all_state = json.load(f)
                    state = all_state.get(self.config.name, {})
                    self._connection_events: List[ConnectionEvent] = state.get(
                        "connection_events", []
                    )
                    self._is_connected = state.get("is_connected", False)
        except Exception as e:
            logger.error(f"Error loading camera state: {e}")

    def _save_state(self):
        """Save this camera's state to the shared state file."""
        try:
            # Load existing state for other cameras
            all_state = {}
            if os.path.exists(self._state_file):
                with open(self._state_file, "r") as f:
                    all_state = json.load(f)
            all_state[self.config.name] = {
                "connection_events": self._connection_events,
                "is_connected": self._is_connected,
            }
            os.makedirs(os.path.dirname(self._state_file), exist_ok=True)
            with open(self._state_file, "w") as f:
                json.dump(all_state, f, indent=4)
        except Exception as e:
            logger.error(f"Error saving camera state: {e}")

    def _get_local_timezone(self):
        """Get the local timezone from config, falling back to America/New_York then UTC."""
        timezone_str = getattr(self.config, "timezone", None)
        if not timezone_str and hasattr(self.config, "app"):
            timezone_str = getattr(self.config.app, "timezone", None)
        if not timezone_str:
            timezone_str = "America/New_York"
        try:
            return pytz.timezone(timezone_str)
        except pytz.UnknownTimeZoneError:
            logger.warning(f"Unknown timezone '{timezone_str}', falling back to UTC")
            return pytz.utc

    def _record_connection_event(self, event_type: str, message: str):
        """Record a connection/disconnection event and save state."""
        local_tz = self._get_local_timezone()
        event: ConnectionEvent = {
            "event_datetime": datetime.now(local_tz).isoformat(),
            "event_type": event_type,
            "message": message,
        }
        self._connection_events.append(event)
        self._save_state()

    def get_connected_timeframes(self) -> List[Tuple[datetime, Optional[datetime]]]:
        """Returns a list of timeframes when the camera was connected."""
        timeframes = []
        start_time = None

        # We need to parse the datetime strings from the loaded events
        parsed_events = []
        for event in self._connection_events:
            dt = datetime.fromisoformat(event["event_datetime"])
            if dt.tzinfo is None:
                dt = pytz.utc.localize(dt)
            parsed_events.append((dt, event["event_type"]))

        sorted_events = sorted(parsed_events, key=lambda x: x[0])

        for event_time, event_type in sorted_events:
            if event_type == "connected":
                if start_time is None:
                    start_time = event_time
            else:  # any other event is a disconnection
                if start_time is not None:
                    timeframes.append((start_time, event_time))
                    start_time = None

        # If the last event was a connection, it's still connected.
        if start_time is not None:
            timeframes.append((start_time, None))  # None indicates it's ongoing.

        return timeframes

    async def check_availability(self) -> bool:
        """Check if the camera is available."""
        try:
            auth = httpx.DigestAuth(self.username, self.password)
            url = f"http://{self.device_ip}/cgi-bin/recordManager.cgi?action=getCaps"
            logger.info(f"Checking availability of camera at {self.device_ip}")

            # Use the provided client if available, otherwise create a new one
            if self._client:
                client = self._client
                close_client = False
            else:
                client = httpx.AsyncClient(timeout=CAMERA_HTTP_TIMEOUT)
                close_client = True

            try:
                logger.debug(f"Making request to: {url}")
                response = await client.get(url, auth=auth)
                logger.debug(f"Got response with status {response.status_code}")

                if response.status_code == 200:
                    if not self._is_connected:
                        self._is_connected = True
                        self._record_connection_event(
                            "connected", "Successfully connected to camera."
                        )
                    return True
                else:
                    if self._is_connected:
                        self._is_connected = False
                        self._record_connection_event(
                            "disconnected",
                            f"Connection failed with status code: {response.status_code}",
                        )
                        logger.info(f"Camera is not available at {self.device_ip}")
                    return False
            except httpx.ConnectError as e:
                logger.info(f"Unable to connect to camera at {self.device_ip}: {e}")
                if self._is_connected:
                    self._is_connected = False
                    self._record_connection_event("disconnected", str(e))
                return False
            except httpx.RequestError as e:
                logger.info(f"Request to camera at {self.device_ip} failed")
                if self._is_connected:
                    self._is_connected = False
                    self._record_connection_event("disconnected", str(e))
                return False
            except Exception as e:
                logger.error(f"Request to {url} failed with error: {e}")
                raise
            finally:
                if close_client:
                    await client.aclose()
        except Exception as e:
            logger.error(f"Error checking camera availability: {e}", exc_info=True)
            if self._is_connected:
                self._is_connected = False
                self._record_connection_event("disconnected", str(e))
            return False

    async def get_file_list(
        self, start_time: datetime, end_time: datetime
    ) -> List[Dict[str, Any]]:
        """Get list of recording files from the camera."""
        try:
            auth = httpx.DigestAuth(self.username, self.password)
            url = f"http://{self.device_ip}/cgi-bin/mediaFileFind.cgi?action=factory.create"

            # Use the provided client if available, otherwise create a new one
            if self._client:
                client = self._client
                close_client = False
            else:
                client = httpx.AsyncClient(timeout=CAMERA_HTTP_TIMEOUT)
                close_client = True

            try:
                # First create the media file finder factory
                response = await client.get(url, auth=auth)
                await self._log_http_call(
                    "get_file_list_factory", response.request, response
                )

                if response.status_code != 200:
                    logger.error(
                        f"Failed to create media file finder factory: {response.status_code}"
                    )
                    return []

                object_id = response.text.split("=")[1].strip()

                # Now find files using the object ID
                start_time_str = start_time.strftime("%Y-%m-%d%%20%H:%M:%S")
                end_time_str = end_time.strftime("%Y-%m-%d%%20%H:%M:%S")

                findfile_url = f"http://{self.device_ip}/cgi-bin/mediaFileFind.cgi?action=findFile&object={object_id}&condition.Channel=1&condition.Types[0]=dav&condition.StartTime={start_time_str}&condition.EndTime={end_time_str}&condition.VideoStream=Main"
                response = await client.get(findfile_url, auth=auth)
                await self._log_http_call(
                    "get_file_list_find", response.request, response
                )

                if response.status_code != 200:
                    logger.error(f"Failed to find media files: {response.status_code}")
                    return []

                # Get the next files
                nextfile_url = f"http://{self.device_ip}/cgi-bin/mediaFileFind.cgi?action=findNextFile&object={object_id}&count=100"
                response = await client.get(nextfile_url, auth=auth)
                await self._log_http_call(
                    "get_file_list_next", response.request, response
                )

                if response.status_code == 200:
                    current_file = {}
                    raw_files = []

                    for line in response.text.strip().split("\n"):
                        if line.startswith("items["):
                            key, value = line.split("=", 1)
                            key = key.strip()
                            value = value.strip()

                            file_index = key.split("[")[1].split("]")[0]
                            field = key.split(".")[1]

                            # Initialize dict for this file index if needed
                            if file_index not in current_file:
                                current_file[file_index] = {}

                            # Map the fields to our expected output format
                            if field == "FilePath":
                                current_file[file_index]["path"] = value
                            elif field == "StartTime":
                                current_file[file_index]["startTime"] = value
                            elif field == "EndTime":
                                current_file[file_index]["endTime"] = value

                    # Convert our dict of dicts to a list of dicts
                    for _, file_data in current_file.items():
                        if (
                            "path" in file_data
                            and "startTime" in file_data
                            and "endTime" in file_data
                        ):
                            raw_files.append(file_data)

                    return raw_files
                return []
            except (httpx.RequestError, httpx.HTTPStatusError) as e:
                logger.error(f"Failed to get file list from camera: {e}")
                return []
            finally:
                if close_client:
                    await client.aclose()
        except Exception as e:
            logger.error(f"Error getting file list: {e}")
            return []

    async def get_file_size(self, file_path: str) -> int:
        """Get size of a file on the camera."""
        try:
            auth = httpx.DigestAuth(self.username, self.password)
            url = f"http://{self.device_ip}/cgi-bin/RPC_Loadfile{file_path}"

            # Use the provided client if available, otherwise create a new one
            if self._client:
                client = self._client
                close_client = False
            else:
                client = httpx.AsyncClient(timeout=CAMERA_HTTP_TIMEOUT)
                close_client = True

            try:
                response = await client.head(url, auth=auth)
                await self._log_http_call("get_file_size", response.request, response)
                if response.status_code == 200:
                    return int(response.headers.get("content-length", 0))
                return 0
            finally:
                if close_client:
                    await client.aclose()
        except Exception as e:
            logger.error(f"Error getting file size: {e}")
            return 0

    async def download_file(self, file_path: str, local_path: str) -> bool:
        """Downloads a file from the camera to a local path."""
        try:
            # Create directory if it doesn't exist
            os.makedirs(os.path.dirname(local_path), exist_ok=True)

            # Get file size first for progress tracking
            file_size = await self.get_file_size(file_path)
            if file_size == 0:
                logger.warning(f"File {file_path} is empty, skipping download.")
                return False

            # Get directory name for better logging
            dir_name = os.path.basename(os.path.dirname(local_path))
            file_name = os.path.basename(local_path)

            self.logger.info(
                f"Downloading {file_name} to directory '{dir_name}' ({file_size / 1024 / 1024:.1f}MB)"
            )

            auth = httpx.DigestAuth(self.username, self.password)
            url = f"http://{self.device_ip}/cgi-bin/RPC_Loadfile{file_path}"

            # Use the provided client if available, otherwise create a new one
            if self._client:
                client = self._client
                close_client = False
            else:
                client = httpx.AsyncClient(timeout=CAMERA_HTTP_TIMEOUT)
                close_client = True

            try:
                async with client.stream("GET", url, auth=auth) as response:
                    await self._log_http_call(
                        "download_file",
                        response.request,
                        response,
                        stream_response=True,
                    )
                    if response.status_code != 200:
                        self.logger.error(
                            f"Download failed with status {response.status_code}: {response.text}"
                        )
                        return False

                    # Open file for writing and stream the download
                    async with aiofiles.open(local_path, "wb") as f:
                        downloaded = 0
                        last_update = time.time()
                        last_downloaded = 0

                        async for chunk in response.aiter_bytes():
                            await f.write(chunk)
                            downloaded += len(chunk)

                            # Update progress every second
                            current_time = time.time()
                            if current_time - last_update >= 10.0:
                                speed = (downloaded - last_downloaded) / (
                                    current_time - last_update
                                )
                                progress = downloaded / file_size * 100
                                bar_length = 20
                                filled_length = int(
                                    bar_length * downloaded // file_size
                                )
                                bar = "█" * filled_length + "░" * (
                                    bar_length - filled_length
                                )
                                self.logger.info(
                                    f"Downloading {file_name} to directory '{dir_name}': [{bar}] {progress:.1f}% ({downloaded / 1024 / 1024:.1f}MB/{file_size / 1024 / 1024:.1f}MB) @ {speed / 1024 / 1024:.1f}MB/s"
                                )
                                last_update = current_time
                                last_downloaded = downloaded

                    # Verify the download is complete
                    if downloaded == file_size:
                        self.logger.info(
                            f"Download complete: {os.path.basename(file_path)}"
                        )
                        return True
                    else:
                        self.logger.error(
                            f"Download incomplete: {os.path.basename(file_path)} ({downloaded}/{file_size} bytes)"
                        )
                        return False

            finally:
                if close_client:
                    await client.aclose()
        except Exception as e:
            self.logger.error(f"Error downloading {file_path}: {e}")
            # Clean up partial download if it exists
            if os.path.exists(local_path):
                try:
                    os.remove(local_path)
                    self.logger.info(f"Removed partial download: {local_path}")
                except Exception as e:
                    self.logger.error(
                        f"Failed to remove partial download {local_path}: {e}"
                    )
            return False

    async def start_recording(self):
        """Starts video recording on the camera."""
        try:
            url = f"http://{self.device_ip}/cgi-bin/configManager.cgi?action=setConfig&ManualRec.Enable=true"
            client = self._client or httpx.AsyncClient(timeout=CAMERA_HTTP_TIMEOUT)
            try:
                response = await client.get(
                    url, auth=httpx.DigestAuth(self.username, self.password)
                )
                await self._log_http_call("start_recording", response.request, response)
                return response.status_code == 200
            finally:
                if not self._client:
                    await client.aclose()
        except Exception as e:
            logger.error(f"Error starting recording: {e}")
            return False

    async def stop_recording(self):
        """Stops video recording on the camera."""
        try:
            url = f"http://{self.device_ip}/cgi-bin/configManager.cgi?action=setConfig&RecordMode[0].Mode=2"
            client = self._client or httpx.AsyncClient(timeout=CAMERA_HTTP_TIMEOUT)
            try:
                response = await client.get(
                    url, auth=httpx.DigestAuth(self.username, self.password)
                )
                await self._log_http_call("stop_recording", response.request, response)
                return response.status_code == 200
            finally:
                if not self._client:
                    await client.aclose()
        except Exception as e:
            logger.error(f"Error stopping recording: {e}")
            return False

    async def delete_files(self, file_paths: List[str]) -> int:
        """Delete recording files from the camera. Not yet implemented for Dahua."""
        logger.warning("delete_files not implemented for Dahua cameras")
        return 0

    async def get_recording_status(self) -> bool:
        """Get recording status from the camera."""
        try:
            url = f"http://{self.device_ip}/cgi-bin/configManager.cgi?action=getConfig&name=RecordMode"
            client = self._client or httpx.AsyncClient(timeout=CAMERA_HTTP_TIMEOUT)
            try:
                response = await client.get(
                    url, auth=httpx.DigestAuth(self.username, self.password)
                )
                await self._log_http_call(
                    "get_recording_status", response.request, response
                )
                if response.status_code == 200:
                    return "RecordMode[0].Mode=1" in response.text
                return False
            finally:
                if not self._client:
                    await client.aclose()
        except Exception as e:
            logger.error(f"Error getting recording status: {e}")
            return False

    async def get_device_info(self) -> DeviceInfo:
        """Get device information from the camera."""
        try:
            url = f"http://{self.device_ip}/cgi-bin/magicBox.cgi?action=getSystemInfo"
            client = self._client or httpx.AsyncClient(timeout=CAMERA_HTTP_TIMEOUT)
            try:
                auth = httpx.DigestAuth(self.username, self.password)
                response = await client.get(url, auth=auth)
                await self._log_http_call("get_device_info", response.request, response)

                if response.status_code == 200:
                    # Parse the response text to extract device info
                    lines = response.text.strip().split("\n")
                    device_info = {}

                    for line in lines:
                        if "=" in line:
                            key, value = line.split("=", 1)
                            device_info[key.strip()] = value.strip()

                    # Map the parsed data to our DeviceInfo structure
                    return DeviceInfo(
                        device_name=device_info.get("deviceName", ""),
                        device_type=device_info.get("deviceType", ""),
                        firmware_version=device_info.get("firmwareVersion", ""),
                        serial_number=device_info.get("serialNumber", ""),
                        ip_address=self.device_ip,
                        mac_address=device_info.get("macAddress", ""),
                        model=device_info.get("model", ""),
                        manufacturer=device_info.get("manufacturer", "Dahua"),
                    )
                else:
                    logger.error(f"Failed to get device info: {response.status_code}")
                    # Return empty DeviceInfo with available data
                    return DeviceInfo(
                        device_name="",
                        device_type="",
                        firmware_version="",
                        serial_number="",
                        ip_address=self.device_ip,
                        mac_address="",
                        model="",
                        manufacturer="Dahua",
                    )
            finally:
                if not self._client:
                    await client.aclose()
        except Exception as e:
            logger.error(f"Error getting device info: {e}")
            # Return empty DeviceInfo with available data
            return DeviceInfo(
                device_name="",
                device_type="",
                firmware_version="",
                serial_number="",
                ip_address=self.device_ip,
                mac_address="",
                model="",
                manufacturer="Dahua",
            )

    @property
    def connection_events(self) -> List[Tuple[datetime, str]]:
        """Get list of connection events."""
        # This property might need to be updated or removed if it's used elsewhere,
        # as the internal format has changed. For now, returning an empty list
        # to satisfy the abstract base class. A better approach would be to refactor
        # consumers of this property.
        return []

    @property
    def is_connected(self) -> bool:
        """Get connection status."""
        return self._is_connected

    async def close(self):
        """Close any open resources."""
        logger.info("Closing DahuaCamera resources")
        if self._client:
            try:
                await self._client.aclose()
                logger.info("Closed HTTP client")
            except Exception as e:
                logger.error(f"Error closing HTTP client: {e}")

    async def get_screenshot(self, server_path: str, output_path: str) -> bool:
        """Get a screenshot from a video file on the camera.

        Args:
            server_path: The path to the file on the camera
            output_path: The path to save the screenshot to

        Returns:
            bool: True if successful, False otherwise
        """
        try:
            # Create directory if it doesn't exist
            os.makedirs(os.path.dirname(output_path), exist_ok=True)

            # Use ffmpeg to extract a frame from the video
            # First download a small part of the file
            temp_file = os.path.join(
                os.path.dirname(output_path), "temp_screenshot.dav"
            )

            auth = httpx.DigestAuth(self.username, self.password)
            url = f"http://{self.device_ip}/cgi-bin/RPC_Loadfile{server_path}"

            # Use the provided client if available, otherwise create a new one
            if self._client:
                client = self._client
                close_client = False
            else:
                client = httpx.AsyncClient(timeout=CAMERA_HTTP_TIMEOUT)
                close_client = True

            try:
                # Download just the first 1MB of the file (should be enough for a frame)
                headers = {"Range": "bytes=0-1048576"}
                async with client.stream(
                    "GET", url, auth=auth, headers=headers
                ) as response:
                    await self._log_http_call(
                        "get_screenshot",
                        response.request,
                        response,
                        stream_response=True,
                    )
                    if response.status_code not in [200, 206]:
                        logger.error(
                            f"Screenshot download failed with status {response.status_code}"
                        )
                        return False

                    # Save to temp file
                    async with aiofiles.open(temp_file, "wb") as f:
                        async for chunk in response.aiter_bytes():
                            await f.write(chunk)

                # Use PyAV to extract the first frame
                try:
                    import av

                    with av.open(temp_file) as container:
                        stream = container.streams.video[0]
                        # Seek to ~1 second in
                        target_pts = int(1.0 / stream.time_base)
                        container.seek(target_pts, stream=stream)

                        for frame in container.decode(video=0):
                            image = frame.to_image()
                            image.save(output_path, "JPEG", quality=95)
                            break
                finally:
                    # Clean up temp file
                    if os.path.exists(temp_file):
                        os.remove(temp_file)

                if os.path.exists(output_path):
                    logger.info(f"Successfully created screenshot at {output_path}")
                    return True
                else:
                    logger.error("Failed to create screenshot: no frame decoded")
                    return False

            finally:
                if close_client:
                    await client.aclose()

        except Exception as e:
            logger.error(f"Error getting screenshot: {e}")
            return False
