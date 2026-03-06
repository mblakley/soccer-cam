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


class ReolinkCamera(Camera):
    """ReoLink camera implementation using the ReoLink HTTP JSON API."""

    def __init__(self, config: CameraConfig, storage_path: str, client=None):
        self.config = config
        self.storage_path = storage_path
        self.device_ip = config.device_ip
        self.username = config.username
        self.password = config.password
        self.channel = config.channel
        self._state_file = get_camera_state_path(storage_path)
        self._connection_events: List[ConnectionEvent] = []
        self._is_connected = False
        self._log_dir = os.path.join(self.storage_path, "camera_http_logs")
        os.makedirs(self._log_dir, exist_ok=True)
        self._client = client
        self.logger = logging.getLogger(__name__)
        self._token: Optional[str] = None
        self._token_expiry: float = 0
        self._load_state()

    # ── Token management ──────────────────────────────────────────────

    async def _login(self, client: httpx.AsyncClient) -> bool:
        """Authenticate with the camera and obtain an API token."""
        url = f"http://{self.device_ip}/cgi-bin/api.cgi?cmd=Login&token=null"
        payload = [
            {
                "cmd": "Login",
                "action": 0,
                "param": {
                    "User": {
                        "userName": self.username,
                        "password": self.password,
                    }
                },
            }
        ]
        response = await client.post(url, json=payload)
        await self._log_http_call("login", response.request, response)

        if response.status_code != 200:
            logger.error(f"Login failed with status {response.status_code}")
            return False

        data = response.json()
        if not data or data[0].get("code") != 0:
            error = data[0].get("error") if data else "empty response"
            logger.error(f"Login failed: {error}")
            return False

        token_info = data[0]["value"]["Token"]
        self._token = token_info["name"]
        lease_time = token_info.get("leaseTime", 3600)
        # Refresh 60 seconds before expiry
        self._token_expiry = time.time() + lease_time - 60
        logger.debug("Successfully obtained ReoLink API token")
        return True

    async def _ensure_token(self, client: httpx.AsyncClient) -> bool:
        """Ensure we have a valid token, refreshing if needed."""
        if self._token and time.time() < self._token_expiry:
            return True
        return await self._login(client)

    def _api_url(self, cmd: str) -> str:
        """Build an API URL with the current token."""
        return f"http://{self.device_ip}/cgi-bin/api.cgi?cmd={cmd}&token={self._token}"

    # ── API helpers ───────────────────────────────────────────────────

    async def _api_call(
        self,
        client: httpx.AsyncClient,
        cmd: str,
        param: Dict[str, Any],
        action: int = 0,
        log_name: str = "",
    ) -> Optional[List[Dict]]:
        """Make an authenticated API call. Returns the JSON response array or None."""
        if not await self._ensure_token(client):
            return None

        url = self._api_url(cmd)
        payload = [{"cmd": cmd, "action": action, "param": param}]
        response = await client.post(url, json=payload)
        await self._log_http_call(log_name or cmd, response.request, response)

        if response.status_code != 200:
            logger.error(f"API call {cmd} failed with status {response.status_code}")
            return None

        data = response.json()
        if not data:
            logger.error(f"API call {cmd} returned empty response")
            return None

        return data

    @staticmethod
    def _datetime_to_reolink(dt: datetime) -> Dict[str, int]:
        """Convert a datetime to ReoLink's time dict format."""
        return {
            "year": dt.year,
            "mon": dt.month,
            "day": dt.day,
            "hour": dt.hour,
            "min": dt.minute,
            "sec": dt.second,
        }

    @staticmethod
    def _reolink_to_datetime_str(t: Dict[str, int]) -> str:
        """Convert ReoLink's time dict to an ISO-style datetime string."""
        return (
            f"{t['year']:04d}-{t['mon']:02d}-{t['day']:02d} "
            f"{t['hour']:02d}:{t['min']:02d}:{t['sec']:02d}"
        )

    # ── State persistence (same pattern as Dahua) ─────────────────────

    def _load_state(self):
        try:
            if os.path.exists(self._state_file):
                with open(self._state_file, "r") as f:
                    state = json.load(f)
                    self._connection_events = state.get("connection_events", [])
                    self._is_connected = state.get("is_connected", False)
        except Exception as e:
            logger.error(f"Error loading camera state: {e}")

    def _save_state(self):
        try:
            state = {
                "connection_events": self._connection_events,
                "is_connected": self._is_connected,
            }
            os.makedirs(os.path.dirname(self._state_file), exist_ok=True)
            with open(self._state_file, "w") as f:
                json.dump(state, f, indent=4)
        except Exception as e:
            logger.error(f"Error saving camera state: {e}")

    def _get_local_timezone(self):
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
        local_tz = self._get_local_timezone()
        event: ConnectionEvent = {
            "event_datetime": datetime.now(local_tz).isoformat(),
            "event_type": event_type,
            "message": message,
        }
        self._connection_events.append(event)
        self._save_state()

    def get_connected_timeframes(self) -> List[Tuple[datetime, Optional[datetime]]]:
        timeframes = []
        start_time = None

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
            else:
                if start_time is not None:
                    timeframes.append((start_time, event_time))
                    start_time = None

        if start_time is not None:
            timeframes.append((start_time, None))

        return timeframes

    # ── HTTP logging (same pattern as Dahua) ──────────────────────────

    async def _log_http_call(
        self,
        name: str,
        request: httpx.Request,
        response: httpx.Response = None,
        error: Exception = None,
        stream_response: bool = False,
    ):
        if os.environ.get("LOG_LEVEL", "").lower() != "debug":
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")

        req_filename = os.path.join(self._log_dir, f"{timestamp}_{name}_request.log")
        async with aiofiles.open(req_filename, "w") as f:
            await f.write(f"URL: {request.method} {request.url}\n")
            await f.write("Headers:\n")
            for key, value in request.headers.items():
                await f.write(f"  {key}: {value}\n")
            if request.content:
                await f.write("\nBody:\n")
                await f.write(request.content.decode("utf-8", errors="ignore"))

        res_filename = os.path.join(self._log_dir, f"{timestamp}_{name}_response.log")
        async with aiofiles.open(res_filename, "w") as f:
            if response:
                await f.write(f"Status Code: {response.status_code}\n")
                await f.write("Headers:\n")
                for key, value in response.headers.items():
                    await f.write(f"  {key}: {value}\n")
                await f.write("\nBody:\n")
                if stream_response:
                    await f.write("[Streamed content not logged]")
                else:
                    await f.write(response.text)
            elif error:
                await f.write(f"Error: {type(error).__name__}\n")
                await f.write(str(error))

    # ── Client helper ─────────────────────────────────────────────────

    def _get_client(self) -> Tuple[httpx.AsyncClient, bool]:
        """Return (client, should_close) tuple."""
        if self._client:
            return self._client, False
        return httpx.AsyncClient(timeout=CAMERA_HTTP_TIMEOUT), True

    # ── Camera interface implementation ───────────────────────────────

    async def check_availability(self) -> bool:
        try:
            client, close_client = self._get_client()
            try:
                data = await self._api_call(
                    client,
                    "GetDevInfo",
                    {"DevInfo": {"channel": self.channel}},
                    log_name="check_availability",
                )
                available = data is not None and data[0].get("code") == 0

                if available and not self._is_connected:
                    self._is_connected = True
                    self._record_connection_event(
                        "connected", "Successfully connected to camera."
                    )
                elif not available and self._is_connected:
                    self._is_connected = False
                    self._record_connection_event(
                        "disconnected", "Camera became unavailable."
                    )

                return available
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
        try:
            client, close_client = self._get_client()
            try:
                data = await self._api_call(
                    client,
                    "Search",
                    {
                        "Search": {
                            "channel": self.channel,
                            "onlyStatus": 0,
                            "streamType": "main",
                            "StartTime": self._datetime_to_reolink(start_time),
                            "EndTime": self._datetime_to_reolink(end_time),
                        }
                    },
                    action=1,
                    log_name="get_file_list",
                )

                if data is None:
                    return []

                resp = data[0]
                if resp.get("code") != 0:
                    logger.error(f"Search failed: {resp.get('error', 'unknown error')}")
                    return []

                search_result = resp.get("value", {}).get("SearchResult", {})
                status_only = search_result.get("Status")
                if status_only is not None and not search_result.get("File"):
                    logger.info(f"Search returned status only: {status_only}")
                    return []

                raw_files = search_result.get("File", [])
                files = []
                for f in raw_files:
                    start = f.get("StartTime", {})
                    end = f.get("EndTime", {})
                    file_entry = {
                        "path": f.get("name", ""),
                        "startTime": self._reolink_to_datetime_str(start),
                        "endTime": self._reolink_to_datetime_str(end),
                    }
                    if "size" in f:
                        file_entry["size"] = f["size"]
                    files.append(file_entry)

                logger.info(f"Found {len(files)} recording files from ReoLink camera")
                return files
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
        try:
            client, close_client = self._get_client()
            try:
                if not await self._ensure_token(client):
                    return 0

                url = (
                    f"http://{self.device_ip}/cgi-bin/api.cgi"
                    f"?cmd=Download&source={file_path}"
                    f"&output={file_path}&token={self._token}"
                )
                response = await client.head(url)
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
        try:
            os.makedirs(os.path.dirname(local_path), exist_ok=True)

            file_size = await self.get_file_size(file_path)
            if file_size == 0:
                logger.warning(
                    f"File {file_path} reported size 0, attempting download anyway."
                )

            dir_name = os.path.basename(os.path.dirname(local_path))
            file_name = os.path.basename(local_path)

            if file_size > 0:
                self.logger.info(
                    f"Downloading {file_name} to directory '{dir_name}' "
                    f"({file_size / 1024 / 1024:.1f}MB)"
                )

            client, close_client = self._get_client()
            try:
                if not await self._ensure_token(client):
                    return False

                url = (
                    f"http://{self.device_ip}/cgi-bin/api.cgi"
                    f"?cmd=Download&source={file_path}"
                    f"&output={file_path}&token={self._token}"
                )

                async with client.stream("GET", url) as response:
                    await self._log_http_call(
                        "download_file",
                        response.request,
                        response,
                        stream_response=True,
                    )
                    if response.status_code != 200:
                        self.logger.error(
                            f"Download failed with status {response.status_code}"
                        )
                        return False

                    # Get content-length from the streaming response if we didn't have it
                    if file_size == 0:
                        file_size = int(response.headers.get("content-length", 0))

                    async with aiofiles.open(local_path, "wb") as f:
                        downloaded = 0
                        last_update = time.time()
                        last_downloaded = 0

                        async for chunk in response.aiter_bytes():
                            await f.write(chunk)
                            downloaded += len(chunk)

                            current_time = time.time()
                            if current_time - last_update >= 10.0 and file_size > 0:
                                speed = (downloaded - last_downloaded) / (
                                    current_time - last_update
                                )
                                progress = downloaded / file_size * 100
                                bar_length = 20
                                filled_length = int(
                                    bar_length * downloaded // file_size
                                )
                                bar = "#" * filled_length + "-" * (
                                    bar_length - filled_length
                                )
                                self.logger.info(
                                    f"Downloading {file_name} to directory "
                                    f"'{dir_name}': [{bar}] {progress:.1f}% "
                                    f"({downloaded / 1024 / 1024:.1f}MB/"
                                    f"{file_size / 1024 / 1024:.1f}MB) "
                                    f"@ {speed / 1024 / 1024:.1f}MB/s"
                                )
                                last_update = current_time
                                last_downloaded = downloaded

                    if file_size > 0 and downloaded != file_size:
                        self.logger.error(
                            f"Download incomplete: {os.path.basename(file_path)} "
                            f"({downloaded}/{file_size} bytes)"
                        )
                        return False

                    self.logger.info(
                        f"Download complete: {os.path.basename(file_path)} "
                        f"({downloaded} bytes)"
                    )
                    return True
            finally:
                if close_client:
                    await client.aclose()
        except Exception as e:
            self.logger.error(f"Error downloading {file_path}: {e}")
            if os.path.exists(local_path):
                try:
                    os.remove(local_path)
                    self.logger.info(f"Removed partial download: {local_path}")
                except Exception as cleanup_err:
                    self.logger.error(
                        f"Failed to remove partial download {local_path}: {cleanup_err}"
                    )
            return False

    async def stop_recording(self) -> bool:
        try:
            client, close_client = self._get_client()
            try:
                data = await self._api_call(
                    client,
                    "SetRec",
                    {"Rec": {"channel": self.channel, "schedule": {"enable": 0}}},
                    log_name="stop_recording",
                )
                return data is not None and data[0].get("code") == 0
            finally:
                if close_client:
                    await client.aclose()
        except Exception as e:
            logger.error(f"Error stopping recording: {e}")
            return False

    async def get_recording_status(self) -> bool:
        try:
            client, close_client = self._get_client()
            try:
                data = await self._api_call(
                    client,
                    "GetRec",
                    {"Rec": {"channel": self.channel}},
                    log_name="get_recording_status",
                )
                if data is None or data[0].get("code") != 0:
                    return False
                rec_info = data[0].get("value", {}).get("Rec", {})
                schedule = rec_info.get("schedule", {})
                return schedule.get("enable", 0) == 1
            finally:
                if close_client:
                    await client.aclose()
        except Exception as e:
            logger.error(f"Error getting recording status: {e}")
            return False

    async def get_device_info(self) -> DeviceInfo:
        try:
            client, close_client = self._get_client()
            try:
                data = await self._api_call(
                    client,
                    "GetDevInfo",
                    {"DevInfo": {"channel": self.channel}},
                    log_name="get_device_info",
                )
                if data is not None and data[0].get("code") == 0:
                    info = data[0]["value"]["DevInfo"]
                    return DeviceInfo(
                        device_name=info.get("name", ""),
                        device_type=info.get("type", ""),
                        firmware_version=info.get("firmVer", ""),
                        serial_number=info.get("serial", ""),
                        ip_address=self.device_ip,
                        mac_address=info.get("mac", ""),
                        model=info.get("model", ""),
                        manufacturer="Reolink",
                    )
                return DeviceInfo(
                    device_name="",
                    device_type="",
                    firmware_version="",
                    serial_number="",
                    ip_address=self.device_ip,
                    mac_address="",
                    model="",
                    manufacturer="Reolink",
                )
            finally:
                if close_client:
                    await client.aclose()
        except Exception as e:
            logger.error(f"Error getting device info: {e}")
            return DeviceInfo(
                device_name="",
                device_type="",
                firmware_version="",
                serial_number="",
                ip_address=self.device_ip,
                mac_address="",
                model="",
                manufacturer="Reolink",
            )

    @property
    def connection_events(self) -> List[Tuple[datetime, str]]:
        return []

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    async def close(self):
        logger.info("Closing ReolinkCamera resources")
        if self._client:
            try:
                await self._client.aclose()
                logger.info("Closed HTTP client")
            except Exception as e:
                logger.error(f"Error closing HTTP client: {e}")
