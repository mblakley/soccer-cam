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
from .reolink_download import download_and_mux
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
        self._file_sizes: dict[str, int] = {}
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

    def _parse_file_list(self, raw_files: list) -> List[Dict[str, Any]]:
        """Parse raw file entries from SearchResult into our file list format."""
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
                size_val = int(f["size"])
                file_entry["size"] = size_val
                self._file_sizes[file_entry["path"]] = size_val
            files.append(file_entry)
        logger.info(f"Found {len(files)} recording files from ReoLink camera")
        return files

    async def _search_by_active_days(
        self,
        client,
        status_entries: list,
        start_time: datetime,
        end_time: datetime,
    ) -> List[Dict[str, Any]]:
        """Search day-by-day for files on days marked active in the status bitmap.

        Reolink's Search API returns a status bitmap (31-char string, one per day)
        instead of file results when the time range spans multiple months.
        """
        active_days = []
        for entry in status_entries:
            year = entry.get("year", 0)
            mon = entry.get("mon", 0)
            table = entry.get("table", "")
            for day_idx, char in enumerate(table):
                if char == "1":
                    day = day_idx + 1  # 0-indexed to 1-indexed
                    try:
                        dt = datetime(year, mon, day)
                        if start_time.date() <= dt.date() <= end_time.date():
                            active_days.append(dt)
                    except ValueError:
                        continue

        if not active_days:
            logger.info("No active recording days found in status bitmap")
            return []

        logger.info(
            f"Status bitmap shows {len(active_days)} active days, "
            f"searching each for files"
        )

        all_files = []
        for day in active_days:
            day_start = day.replace(hour=0, minute=0, second=0)
            day_end = day.replace(hour=23, minute=59, second=59)

            data = await self._api_call(
                client,
                "Search",
                {
                    "Search": {
                        "channel": self.channel,
                        "onlyStatus": 0,
                        "streamType": "main",
                        "StartTime": self._datetime_to_reolink(day_start),
                        "EndTime": self._datetime_to_reolink(day_end),
                    }
                },
                action=1,
                log_name="get_file_list_day",
            )

            if data is None:
                continue

            resp = data[0]
            if resp.get("code") != 0:
                continue

            sr = resp.get("value", {}).get("SearchResult", {})
            raw_files = sr.get("File", [])
            if raw_files:
                all_files.extend(self._parse_file_list(raw_files))

        return all_files

    # ── State persistence (same pattern as Dahua) ─────────────────────

    def _load_state(self):
        try:
            if os.path.exists(self._state_file):
                with open(self._state_file, "r") as f:
                    all_state = json.load(f)
                    state = all_state.get(self.config.name, {})
                    self._connection_events = state.get("connection_events", [])
                    self._is_connected = state.get("is_connected", False)
        except Exception as e:
            logger.error(f"Error loading camera state: {e}")

    def _save_state(self):
        try:
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
                    # Wide time range: camera returns day-level status bitmap
                    # instead of file list. Search day-by-day for active days.
                    return await self._search_by_active_days(
                        client, status_only, start_time, end_time
                    )

                return self._parse_file_list(search_result.get("File", []))
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
        """Get file size from search metadata or HTTP HEAD.

        Prefers the 'size' field from get_file_list() search results (available
        in the SearchResult).  Falls back to an HTTP HEAD request.
        """
        # Try cached search metadata first (populated by get_file_list)
        size = self._file_sizes.get(file_path, 0)
        if size > 0:
            return size

        # Fallback: HTTP HEAD (broken on some firmware, may return 0)
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
        """Download a recording via Baichuan protocol (port 9000).

        The Reolink HTTP Download API is broken on some firmware versions.
        This uses the native Baichuan binary protocol to stream the recording
        to disk, then remuxes to MP4 via PyAV.
        """
        try:
            os.makedirs(os.path.dirname(local_path), exist_ok=True)

            dir_name = os.path.basename(os.path.dirname(local_path))
            file_name = os.path.basename(local_path)
            file_size = await self.get_file_size(file_path)

            if file_size > 0:
                self.logger.info(
                    f"Downloading {file_name} to '{dir_name}' via Baichuan "
                    f"({file_size / 1024 / 1024:.1f}MB)"
                )
            else:
                self.logger.info(
                    f"Downloading {file_name} to '{dir_name}' via Baichuan"
                )

            def _progress(bytes_written, elapsed):
                if file_size > 0:
                    pct = bytes_written / file_size * 100
                    speed = bytes_written / elapsed if elapsed > 0 else 0
                    self.logger.info(
                        f"Downloading {file_name}: {pct:.0f}% "
                        f"({bytes_written / 1024 / 1024:.1f}MB) "
                        f"@ {speed / 1024 / 1024:.1f}MB/s"
                    )

            # Extract hostname without port (device_ip may be "host:port")
            baichuan_host = self.device_ip.split(":")[0]

            success = await download_and_mux(
                host=baichuan_host,
                port=self.config.baichuan_port,
                username=self.username,
                password=self.password,
                file_path=file_path,
                output_mp4=local_path,
                channel=self.channel,
                on_progress=_progress,
            )

            if success:
                self.logger.info(f"Download complete: {os.path.basename(file_path)}")
            else:
                self.logger.error(f"Download failed: {os.path.basename(file_path)}")
                if os.path.exists(local_path):
                    try:
                        os.remove(local_path)
                    except OSError:
                        pass
            return success

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
        """Suppress recording by switching from TIMING to MD schedule.

        Swaps the recording schedule from continuous (TIMING=all 1s) to
        motion-detection only (MD=all 1s, TIMING=all 0s).  The master
        enable stays at 1, so this change is safe across reboots -- if
        the camera is unplugged before restore, it still records via
        motion detection at the field.

        Uses SetRecV20 (the older SetRec returns "not support" on newer
        Reolink models like the Duo 3 PoE).
        """
        try:
            client, close_client = self._get_client()
            try:
                all_zeros = "0" * 168
                all_ones = "1" * 168
                data = await self._api_call(
                    client,
                    "SetRecV20",
                    {
                        "Rec": {
                            "channel": self.channel,
                            "schedule": {
                                "channel": self.channel,
                                "table": {
                                    "TIMING": all_zeros,
                                    "MD": all_ones,
                                },
                            },
                        }
                    },
                    log_name="stop_recording",
                )
                return data is not None and data[0].get("code") == 0
            finally:
                if close_client:
                    await client.aclose()
        except Exception as e:
            logger.error(f"Error stopping recording: {e}")
            return False

    async def start_recording(self) -> bool:
        """Restore continuous recording by switching from MD back to TIMING.

        Reverses stop_recording() by setting TIMING=all 1s, MD=all 0s.
        Called before sending the unplug notification so the camera is
        ready for continuous recording at the field.
        """
        try:
            client, close_client = self._get_client()
            try:
                all_zeros = "0" * 168
                all_ones = "1" * 168
                data = await self._api_call(
                    client,
                    "SetRecV20",
                    {
                        "Rec": {
                            "channel": self.channel,
                            "schedule": {
                                "channel": self.channel,
                                "table": {
                                    "TIMING": all_ones,
                                    "MD": all_zeros,
                                },
                            },
                        }
                    },
                    log_name="start_recording",
                )
                return data is not None and data[0].get("code") == 0
            finally:
                if close_client:
                    await client.aclose()
        except Exception as e:
            logger.error(f"Error starting recording: {e}")
            return False

    async def delete_files(self, file_paths: List[str]) -> int:
        """Delete recording files from the camera.

        Most Reolink models (including Duo 3 PoE) do not support
        programmatic file deletion via the HTTP API. The Remove command
        returns "not support". Only a handful of newer models (Atlas,
        Elite Floodlight WiFi, Home Hub) support deletion via the
        mobile app. Recordings auto-overwrite when the SD card fills.
        """
        if not file_paths:
            return 0
        logger.info(
            f"Reolink cameras do not support file deletion via API. "
            f"{len(file_paths)} file(s) will remain on the SD card "
            f"until overwritten."
        )
        return 0

    async def get_recording_status(self) -> bool:
        """Check if continuous (TIMING) recording is active via GetRecV20.

        Returns True if the TIMING schedule has any active slots,
        indicating the camera is in continuous recording mode.
        Returns False if TIMING is all zeros (suppressed to MD-only).
        """
        try:
            client, close_client = self._get_client()
            try:
                data = await self._api_call(
                    client,
                    "GetRecV20",
                    {"channel": self.channel},
                    log_name="get_recording_status",
                )
                if data is None or data[0].get("code") != 0:
                    return False
                rec_info = data[0].get("value", {}).get("Rec", {})
                timing = rec_info.get("schedule", {}).get("table", {}).get("TIMING", "")
                return "1" in timing
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
