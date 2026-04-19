import json
import os
import httpx
import logging
import time
import aiofiles
from datetime import datetime
from typing import List, Tuple, Dict, Any, Optional

import pytz

from .base import Camera, ConfigResult, DeviceInfo
from . import register_camera
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
                    f"Downloading {file_name} to '{dir_name}' "
                    f"({file_size / 1024 / 1024:.1f}MB)"
                )
            else:
                self.logger.info(f"Downloading {file_name} to '{dir_name}'")

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
                http_port=self.config.http_port,
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
        """Disable recording via the SetRecV20 master enable switch."""
        return await self._set_recording_enabled(False)

    async def start_recording(self) -> bool:
        """Enable recording via the SetRecV20 master enable switch."""
        return await self._set_recording_enabled(True)

    async def _set_recording_enabled(self, enabled: bool) -> bool:
        """Toggle the RecV20 master enable switch. Does not touch schedules."""
        try:
            client, close_client = self._get_client()
            try:
                data = await self._api_call(
                    client,
                    "SetRecV20",
                    {
                        "Rec": {
                            "channel": self.channel,
                            "enable": 1 if enabled else 0,
                        }
                    },
                    log_name="set_recording_enabled",
                )
                return data is not None and data[0].get("code") == 0
            finally:
                if close_client:
                    await client.aclose()
        except Exception as e:
            logger.error(f"Error setting recording enabled={enabled}: {e}")
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
        """Check if recording is enabled via the GetRecV20 master switch.

        Returns True if enable=1, False otherwise.
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
                return rec_info.get("enable", 0) == 1
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

    # ── Configuration push ──────────────────────────────────────────

    @staticmethod
    def _get_reolink_tz_offset() -> int | None:
        """Get Reolink timezone offset from the local system clock.

        Reolink uses positive values for west of UTC (inverted sign):
        US Eastern (UTC-5) = 18000, US Central (UTC-6) = 21600, etc.
        Uses stdlib only -- no pytz/tzlocal dependency needed.
        """
        try:
            from datetime import timezone as dt_tz

            offset = datetime.now(dt_tz.utc).astimezone().utcoffset()
            if offset is None:
                return None
            return -int(offset.total_seconds())
        except Exception:
            return None

    async def get_current_settings(self) -> list[ConfigResult]:
        results: list[ConfigResult] = []
        client, close_client = self._get_client()
        try:
            # Recording
            data = await self._api_call(
                client,
                "GetRecV20",
                {"channel": self.channel},
                log_name="get_settings_rec",
            )
            if data and data[0].get("code") == 0:
                rec = data[0].get("value", {}).get("Rec", {})
                enabled = rec.get("enable", 0) == 1
                timing = rec.get("schedule", {}).get("table", {}).get("TIMING", "")
                all_on = timing == "1" * 168
                if enabled and all_on:
                    rec_str = "Always on (24/7)"
                elif enabled:
                    rec_str = "Enabled (partial schedule)"
                else:
                    rec_str = "Disabled"
                results.append(
                    ConfigResult(
                        setting="recording",
                        success=True,
                        current_value=rec_str,
                        applied_value="",
                        error="",
                    )
                )
            else:
                results.append(
                    ConfigResult(
                        setting="recording",
                        success=False,
                        current_value="Unknown",
                        applied_value="",
                        error="Failed to read GetRecV20",
                    )
                )

            # NTP
            data = await self._api_call(
                client,
                "GetNtp",
                {},
                log_name="get_settings_ntp",
            )
            if data and data[0].get("code") == 0:
                ntp = data[0].get("value", {}).get("Ntp", {})
                enabled = ntp.get("enable", 0) == 1
                server = ntp.get("server", "")
                ntp_str = f"{'Enabled' if enabled else 'Disabled'}"
                if server:
                    ntp_str += f", server={server}"
                results.append(
                    ConfigResult(
                        setting="ntp",
                        success=True,
                        current_value=ntp_str,
                        applied_value="",
                        error="",
                    )
                )
            else:
                results.append(
                    ConfigResult(
                        setting="ntp",
                        success=False,
                        current_value="Unknown",
                        applied_value="",
                        error="Failed to read GetNtp",
                    )
                )

            # Encoding
            data = await self._api_call(
                client,
                "GetEnc",
                {"channel": self.channel},
                action=1,
                log_name="get_settings_enc",
            )
            if data and data[0].get("code") == 0:
                enc = data[0].get("value", {}).get("Enc", {})
                main = enc.get("mainStream", {})
                codec = main.get("vType", "?")
                bitrate = main.get("bitRate", "?")
                fps = main.get("frameRate", "?")
                size = main.get("size", "?")
                gop = main.get("gop", "?")
                profile = main.get("profile", "?")
                results.append(
                    ConfigResult(
                        setting="encoding",
                        success=True,
                        current_value=(
                            f"{codec} {size} {bitrate}kbps {fps}fps GOP={gop} {profile}"
                        ),
                        applied_value="",
                        error="",
                    )
                )
            else:
                results.append(
                    ConfigResult(
                        setting="encoding",
                        success=False,
                        current_value="Unknown",
                        applied_value="",
                        error="Failed to read GetEnc",
                    )
                )
            # DST
            data = await self._api_call(
                client,
                "GetTime",
                {},
                log_name="get_settings_time",
            )
            if data and data[0].get("code") == 0:
                dst = data[0].get("value", {}).get("Dst", {})
                dst_enabled = dst.get("enable", 0) == 1
                results.append(
                    ConfigResult(
                        setting="dst",
                        success=True,
                        current_value="Enabled" if dst_enabled else "Disabled",
                        applied_value="",
                        error="",
                    )
                )
            else:
                results.append(
                    ConfigResult(
                        setting="dst",
                        success=False,
                        current_value="Unknown",
                        applied_value="",
                        error="Failed to read GetTime",
                    )
                )

            # Network
            data = await self._api_call(
                client,
                "GetLocalLink",
                {},
                log_name="get_settings_net",
            )
            if data and data[0].get("code") == 0:
                link = data[0].get("value", {}).get("LocalLink", {})
                net_type = link.get("type", "?")
                ip = link.get("static", {}).get("ip", "?")
                net_str = "DHCP" if net_type == "DHCP" else f"Static ({ip})"
                results.append(
                    ConfigResult(
                        setting="network",
                        success=True,
                        current_value=net_str,
                        applied_value="",
                        error="",
                    )
                )
            else:
                results.append(
                    ConfigResult(
                        setting="network",
                        success=False,
                        current_value="Unknown",
                        applied_value="",
                        error="Failed to read GetLocalLink",
                    )
                )
        finally:
            if close_client:
                await client.aclose()

        return results

    async def apply_optimal_settings(self, timezone: str = "") -> list[ConfigResult]:
        results: list[ConfigResult] = []
        client, close_client = self._get_client()
        try:
            # 1. Recording: enable + continuous TIMING schedule
            data = await self._api_call(
                client,
                "SetRecV20",
                {
                    "Rec": {
                        "channel": self.channel,
                        "enable": 1,
                        "schedule": {
                            "channel": self.channel,
                            "table": {"TIMING": "1" * 168},
                        },
                    }
                },
                log_name="apply_settings_rec",
            )
            ok = data is not None and data[0].get("code") == 0
            results.append(
                ConfigResult(
                    setting="recording",
                    success=ok,
                    current_value="",
                    applied_value="Always on (24/7)",
                    error="" if ok else "SetRecV20 failed",
                )
            )

            # 2. NTP + timezone
            data = await self._api_call(
                client,
                "SetNtp",
                {
                    "Ntp": {
                        "enable": 1,
                        "server": "pool.ntp.org",
                        "port": 123,
                        "interval": 1440,
                    }
                },
                log_name="apply_settings_ntp",
            )
            ntp_ok = data is not None and data[0].get("code") == 0

            # Set timezone via SetTime (auto-detect from system clock)
            tz_ok = True
            tz_note = ""
            tz_offset = self._get_reolink_tz_offset()
            if tz_offset is not None:
                data = await self._api_call(
                    client,
                    "SetTime",
                    {"Time": {"timeZone": tz_offset}},
                    log_name="apply_settings_tz",
                )
                tz_ok = data is not None and data[0].get("code") == 0
                tz_note = f", tz offset={tz_offset}s"

            ok = ntp_ok and tz_ok
            results.append(
                ConfigResult(
                    setting="ntp",
                    success=ok,
                    current_value="",
                    applied_value=f"Enabled, pool.ntp.org{tz_note}",
                    error="" if ok else "SetNtp/SetTime failed",
                )
            )

            # 3. Encoding: read-modify-write for optimal soccer recording
            # Target: 20fps, 8192kbps, GOP=40 (2x FPS), High profile
            data = await self._api_call(
                client,
                "GetEnc",
                {"channel": self.channel},
                action=1,
                log_name="apply_settings_get_enc",
            )
            if data and data[0].get("code") == 0:
                enc = data[0]["value"]["Enc"]
                main = enc.get("mainStream", {})
                changes = []
                current_bitrate = main.get("bitRate", 0)
                current_fps = main.get("frameRate", 0)
                current_gop = main.get("gop", 0)
                current_profile = main.get("profile", "")

                if current_fps < 20:
                    main["frameRate"] = 20
                    changes.append(f"FPS {current_fps}->20")
                if current_bitrate < 12288:
                    main["bitRate"] = 12288
                    changes.append(f"bitrate {current_bitrate}->12288kbps")
                if current_gop != 40:
                    main["gop"] = 40
                    changes.append(f"GOP {current_gop}->40")
                if current_profile != "High":
                    main["profile"] = "High"
                    changes.append(f"profile {current_profile}->High")

                if changes:
                    data = await self._api_call(
                        client,
                        "SetEnc",
                        {"Enc": enc},
                        log_name="apply_settings_set_enc",
                    )
                    ok = data is not None and data[0].get("code") == 0
                    results.append(
                        ConfigResult(
                            setting="encoding",
                            success=ok,
                            current_value="",
                            applied_value=", ".join(changes),
                            error="" if ok else "SetEnc failed",
                        )
                    )
                else:
                    results.append(
                        ConfigResult(
                            setting="encoding",
                            success=True,
                            current_value="",
                            applied_value="No changes needed",
                            error="",
                        )
                    )
            else:
                results.append(
                    ConfigResult(
                        setting="encoding",
                        success=False,
                        current_value="",
                        applied_value="",
                        error="Failed to read current encoding",
                    )
                )

            # 4. DST: US rules (2nd Sunday March 2AM -> 1st Sunday Nov 2AM)
            # Read current time settings first, then merge DST fields
            data = await self._api_call(
                client,
                "GetTime",
                {},
                log_name="apply_settings_get_time",
            )
            time_param: dict = {}
            if data and data[0].get("code") == 0:
                time_param = data[0].get("value", {})

            time_param["Dst"] = {
                "enable": 1,
                "offset": 1,
                "startMon": 3,
                "startWeek": 2,
                "startWeekday": 0,
                "startHour": 2,
                "startMin": 0,
                "startSec": 0,
                "endMon": 11,
                "endWeek": 1,
                "endWeekday": 0,
                "endHour": 2,
                "endMin": 0,
                "endSec": 0,
            }
            data = await self._api_call(
                client,
                "SetTime",
                time_param,
                log_name="apply_settings_dst",
            )
            ok = data is not None and data[0].get("code") == 0
            results.append(
                ConfigResult(
                    setting="dst",
                    success=ok,
                    current_value="",
                    applied_value="US DST (Mar 2nd Sun -> Nov 1st Sun)",
                    error="" if ok else "SetTime DST failed",
                )
            )

            # 5. Static IP: lock the current IP so it doesn't change
            data = await self._api_call(
                client,
                "GetLocalLink",
                {},
                log_name="apply_settings_get_net",
            )
            if data and data[0].get("code") == 0:
                link = data[0].get("value", {}).get("LocalLink", {})
                current_type = link.get("type", "DHCP")
                if current_type == "DHCP":
                    # Switch to static with current IP
                    ip_parts = self.device_ip.split(":")[0]
                    static = link.get("static", {})
                    static["ip"] = ip_parts
                    link["type"] = "Static"
                    link["dns"]["auto"] = 0
                    if not link["dns"].get("dns1"):
                        link["dns"]["dns1"] = "8.8.8.8"
                        link["dns"]["dns2"] = "8.8.4.4"
                    data = await self._api_call(
                        client,
                        "SetLocalLink",
                        {"LocalLink": link},
                        log_name="apply_settings_set_net",
                    )
                    ok = data is not None and data[0].get("code") == 0
                    results.append(
                        ConfigResult(
                            setting="network",
                            success=ok,
                            current_value="DHCP",
                            applied_value=f"Static IP {ip_parts}",
                            error="" if ok else "SetLocalLink failed",
                        )
                    )
                else:
                    results.append(
                        ConfigResult(
                            setting="network",
                            success=True,
                            current_value="Static",
                            applied_value="Already static",
                            error="",
                        )
                    )
            else:
                results.append(
                    ConfigResult(
                        setting="network",
                        success=False,
                        current_value="Unknown",
                        applied_value="",
                        error="Failed to read network config",
                    )
                )
        finally:
            if close_client:
                await client.aclose()

        return results

    async def change_camera_password(
        self, current_password: str, new_password: str
    ) -> bool:
        try:
            client, close_client = self._get_client()
            try:
                data = await self._api_call(
                    client,
                    "ModifyUser",
                    {"User": {"userName": self.username, "password": new_password}},
                    log_name="change_password",
                )
                if data is not None and data[0].get("code") == 0:
                    self.password = new_password
                    # Invalidate token so next call re-authenticates with new password
                    self._token = None
                    self._token_expiry = 0
                    return True
                return False
            finally:
                if close_client:
                    await client.aclose()
        except Exception as e:
            logger.error(f"Error changing password: {e}")
            return False

    async def close(self):
        logger.info("Closing ReolinkCamera resources")
        if self._client:
            try:
                await self._client.aclose()
                logger.info("Closed HTTP client")
            except Exception as e:
                logger.error(f"Error closing HTTP client: {e}")


# Register with the camera registry
register_camera("reolink", ReolinkCamera)
