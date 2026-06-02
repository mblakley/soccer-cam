import logging
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from video_grouper.cameras.reolink import ReolinkCamera
from video_grouper.utils.config import CameraConfig

logging.basicConfig(level=logging.INFO)


# ── Helpers ───────────────────────────────────────────────────────────


def _make_config():
    return CameraConfig(
        name="default",
        type="reolink",
        device_ip="192.168.1.200",
        username="admin",
        password="admin",
        channel=0,
    )


def _login_response():
    """Successful login JSON response."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = [
        {
            "cmd": "Login",
            "code": 0,
            "value": {
                "Token": {"name": "abc123", "leaseTime": 3600},
            },
        }
    ]
    return resp


def _success_response(cmd, value):
    """Generic successful API JSON response."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = [{"cmd": cmd, "code": 0, "value": value}]
    resp.text = str(resp.json.return_value)
    return resp


def _error_response(cmd, code=1, detail="some error"):
    """Generic error API JSON response."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = [{"cmd": cmd, "code": code, "error": {"detail": detail}}]
    resp.text = str(resp.json.return_value)
    return resp


def _file_dict(name, start_tuple, end_tuple, size):
    """Build a Reolink SearchResult.File entry from (Y,M,D,h,m,s) tuples."""
    sy, sm, sd, sh, smin, ss = start_tuple
    ey, em, ed, eh, emin, es = end_tuple
    return {
        "name": name,
        "StartTime": {
            "year": sy,
            "mon": sm,
            "day": sd,
            "hour": sh,
            "min": smin,
            "sec": ss,
        },
        "EndTime": {
            "year": ey,
            "mon": em,
            "day": ed,
            "hour": eh,
            "min": emin,
            "sec": es,
        },
        "size": size,
    }


def _month_bitmap(year, month, active_days):
    """31-char bitmap (1 per day, day 1 at index 0) marking the given days."""
    bits = ["0"] * 31
    for d in active_days:
        bits[d - 1] = "1"
    return {"year": year, "mon": month, "table": "".join(bits)}


# ── Initialization ────────────────────────────────────────────────────


class TestReolinkCameraInitialization:
    def test_init_with_config(self, tmp_path):
        config = _make_config()
        camera = ReolinkCamera(config=config, storage_path=str(tmp_path))
        assert camera.device_ip == "192.168.1.200"
        assert camera.username == "admin"
        assert camera.password == "admin"
        assert camera.channel == 0
        assert camera._token is None

    def test_properties(self, tmp_path):
        camera = ReolinkCamera(config=_make_config(), storage_path=str(tmp_path))
        assert isinstance(camera.connection_events, list)
        assert isinstance(camera.is_connected, bool)


# ── Token management ──────────────────────────────────────────────────


class TestReolinkTokenManagement:
    @pytest.mark.asyncio
    @patch(
        "video_grouper.cameras.reolink.ReolinkCamera._log_http_call",
        new_callable=AsyncMock,
    )
    async def test_login_success(self, mock_log):
        mock_client = AsyncMock()
        mock_client.post.return_value = _login_response()

        camera = ReolinkCamera(
            config=_make_config(), storage_path="test_path", client=mock_client
        )

        result = await camera._login(mock_client)
        assert result is True
        assert camera._token == "abc123"
        assert camera._token_expiry > 0

    @pytest.mark.asyncio
    @patch(
        "video_grouper.cameras.reolink.ReolinkCamera._log_http_call",
        new_callable=AsyncMock,
    )
    async def test_login_failure(self, mock_log):
        mock_client = AsyncMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = [
            {"cmd": "Login", "code": 1, "error": {"detail": "bad credentials"}}
        ]
        mock_client.post.return_value = resp

        camera = ReolinkCamera(
            config=_make_config(), storage_path="test_path", client=mock_client
        )

        result = await camera._login(mock_client)
        assert result is False
        assert camera._token is None

    @pytest.mark.asyncio
    @patch(
        "video_grouper.cameras.reolink.ReolinkCamera._log_http_call",
        new_callable=AsyncMock,
    )
    async def test_login_http_error(self, mock_log):
        mock_client = AsyncMock()
        resp = MagicMock()
        resp.status_code = 500
        mock_client.post.return_value = resp

        camera = ReolinkCamera(
            config=_make_config(), storage_path="test_path", client=mock_client
        )

        result = await camera._login(mock_client)
        assert result is False


# ── Availability ──────────────────────────────────────────────────────


class TestReolinkCameraAvailability:
    @pytest.mark.asyncio
    @patch(
        "video_grouper.cameras.reolink.ReolinkCamera._log_http_call",
        new_callable=AsyncMock,
    )
    async def test_check_availability_success(self, mock_log):
        mock_client = AsyncMock()
        mock_client.post.side_effect = [
            _login_response(),
            _success_response(
                "GetDevInfo",
                {"DevInfo": {"name": "Reolink", "model": "RLC-810A"}},
            ),
        ]

        camera = ReolinkCamera(
            config=_make_config(), storage_path="test_path", client=mock_client
        )
        camera._is_connected = False
        camera._connection_events = []

        result = await camera.check_availability()
        assert result is True
        assert camera._is_connected is True
        assert len(camera._connection_events) == 1
        assert camera._connection_events[0]["event_type"] == "connected"

    @pytest.mark.asyncio
    @patch(
        "video_grouper.cameras.reolink.ReolinkCamera._log_http_call",
        new_callable=AsyncMock,
    )
    async def test_check_availability_connection_error(self, mock_log):
        mock_client = AsyncMock()
        mock_client.post.side_effect = httpx.ConnectError("Connection refused")

        camera = ReolinkCamera(
            config=_make_config(), storage_path="test_path", client=mock_client
        )
        camera._is_connected = True
        camera._connection_events = []

        result = await camera.check_availability()
        assert result is False
        assert camera._is_connected is False
        assert len(camera._connection_events) == 1
        assert camera._connection_events[0]["event_type"] == "disconnected"

    @pytest.mark.asyncio
    @patch(
        "video_grouper.cameras.reolink.ReolinkCamera._log_http_call",
        new_callable=AsyncMock,
    )
    async def test_check_availability_no_state_change(self, mock_log):
        mock_client = AsyncMock()
        mock_client.post.side_effect = [
            _login_response(),
            _success_response(
                "GetDevInfo",
                {"DevInfo": {"name": "Reolink"}},
            ),
        ]

        camera = ReolinkCamera(
            config=_make_config(), storage_path="test_path", client=mock_client
        )
        camera._is_connected = True
        camera._connection_events = []

        result = await camera.check_availability()
        assert result is True
        assert camera._is_connected is True
        assert len(camera._connection_events) == 0


# ── File operations ───────────────────────────────────────────────────


class TestReolinkCameraFileOperations:
    @pytest.mark.asyncio
    @patch(
        "video_grouper.cameras.reolink.ReolinkCamera._log_http_call",
        new_callable=AsyncMock,
    )
    async def test_get_file_list_success(self, mock_log):
        mock_client = AsyncMock()
        search_value = {
            "SearchResult": {
                "Status": 0,
                "File": [
                    {
                        "name": "Rec/20240101/Rec_00_20240101120000.mp4",
                        "StartTime": {
                            "year": 2024,
                            "mon": 1,
                            "day": 1,
                            "hour": 12,
                            "min": 0,
                            "sec": 0,
                        },
                        "EndTime": {
                            "year": 2024,
                            "mon": 1,
                            "day": 1,
                            "hour": 12,
                            "min": 30,
                            "sec": 0,
                        },
                        "size": 320256446,
                    },
                ],
            }
        }
        mock_client.post.side_effect = [
            _login_response(),
            _success_response("Search", search_value),
        ]

        camera = ReolinkCamera(
            config=_make_config(), storage_path="test_path", client=mock_client
        )

        start = datetime(2024, 1, 1, 12, 0, 0)
        end = datetime(2024, 1, 1, 13, 0, 0)
        files = await camera.get_file_list(start, end)

        assert len(files) == 1
        assert files[0]["path"] == "Rec/20240101/Rec_00_20240101120000.mp4"
        assert files[0]["startTime"] == "2024-01-01 12:00:00"
        assert files[0]["endTime"] == "2024-01-01 12:30:00"
        assert files[0]["size"] == 320256446

    @pytest.mark.asyncio
    @patch(
        "video_grouper.cameras.reolink.ReolinkCamera._log_http_call",
        new_callable=AsyncMock,
    )
    async def test_get_file_list_empty(self, mock_log):
        mock_client = AsyncMock()
        mock_client.post.side_effect = [
            _login_response(),
            _success_response("Search", {"SearchResult": {"Status": 0}}),
        ]

        camera = ReolinkCamera(
            config=_make_config(), storage_path="test_path", client=mock_client
        )

        files = await camera.get_file_list(datetime(2024, 1, 1), datetime(2024, 1, 2))
        assert files == []

    @pytest.mark.asyncio
    @patch(
        "video_grouper.cameras.reolink.ReolinkCamera._log_http_call",
        new_callable=AsyncMock,
    )
    async def test_get_file_list_login_failure(self, mock_log):
        mock_client = AsyncMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = [{"cmd": "Login", "code": 1, "error": {}}]
        mock_client.post.return_value = resp

        camera = ReolinkCamera(
            config=_make_config(), storage_path="test_path", client=mock_client
        )

        files = await camera.get_file_list(datetime(2024, 1, 1), datetime(2024, 1, 2))
        assert files == []

    @pytest.mark.asyncio
    @patch(
        "video_grouper.cameras.reolink.ReolinkCamera._log_http_call",
        new_callable=AsyncMock,
    )
    async def test_get_file_list_cross_month_uses_per_day_search(self, mock_log):
        """A cross-month range surfaces the camera's partial-File quirk:
        Search returns a multi-month Status bitmap *and* a single 'teaser'
        File from the start day, dropping every later day. The code must
        detect the multi-day query + bitmap and re-issue per-day Searches.

        Regression: HWM stuck at 2026-05-31 17:32:57 caused every poll on
        2026-06-01 to receive just that one stale file, so the 20 game
        segments recorded after the rollover were invisible to the poller.
        """
        teaser = _file_dict(
            "/mnt/sda/Mp4Record/2026-05-31/teaser.mp4",
            (2026, 5, 31, 17, 32, 22),
            (2026, 5, 31, 17, 32, 57),
            21023848,
        )
        cross_month_initial = {
            "SearchResult": {
                "channel": 0,
                "Status": [
                    _month_bitmap(2026, 5, [31]),
                    _month_bitmap(2026, 6, [1]),
                ],
                "File": [teaser],
            }
        }
        may_31_per_day = {"SearchResult": {"channel": 0, "File": [teaser]}}
        jun_1_per_day = {
            "SearchResult": {
                "channel": 0,
                "File": [
                    _file_dict(
                        "/mnt/sda/Mp4Record/2026-06-01/seg1.mp4",
                        (2026, 6, 1, 18, 25, 38),
                        (2026, 6, 1, 18, 30, 37),
                        768916546,
                    ),
                    _file_dict(
                        "/mnt/sda/Mp4Record/2026-06-01/seg2.mp4",
                        (2026, 6, 1, 18, 30, 38),
                        (2026, 6, 1, 18, 35, 38),
                        771701914,
                    ),
                    _file_dict(
                        "/mnt/sda/Mp4Record/2026-06-01/seg_last.mp4",
                        (2026, 6, 1, 19, 55, 39),
                        (2026, 6, 1, 20, 0, 39),
                        776953087,
                    ),
                ],
            }
        }

        mock_client = AsyncMock()
        mock_client.post.side_effect = [
            _login_response(),
            _success_response("Search", cross_month_initial),
            _success_response("Search", may_31_per_day),
            _success_response("Search", jun_1_per_day),
        ]

        camera = ReolinkCamera(
            config=_make_config(), storage_path="test_path", client=mock_client
        )

        files = await camera.get_file_list(
            datetime(2026, 5, 31, 17, 31, 57),
            datetime(2026, 6, 1, 21, 30, 0),
        )

        paths = [f["path"] for f in files]
        assert len(files) == 4, (
            "Expected 4 files (1 from May 31 + 3 from Jun 1) after per-day "
            f"re-search, but got {len(files)}: {paths}"
        )
        assert any("seg1" in p for p in paths)
        assert any("seg_last" in p for p in paths)
        # Verify the per-day Searches were issued (login + 3 Searches)
        assert mock_client.post.call_count == 4

    @pytest.mark.asyncio
    @patch(
        "video_grouper.cameras.reolink.ReolinkCamera._log_http_call",
        new_callable=AsyncMock,
    )
    async def test_get_file_list_cross_day_same_month_uses_per_day_search(
        self, mock_log
    ):
        """Cross-day within a single month: camera returns one-month bitmap
        and an empty File array. Must route to per-day search."""
        bitmap_initial = {
            "SearchResult": {
                "channel": 0,
                "Status": [_month_bitmap(2026, 5, [30, 31])],
                "File": [],
            }
        }
        may_30 = {
            "SearchResult": {
                "channel": 0,
                "File": [
                    _file_dict(
                        "/mnt/sda/Mp4Record/2026-05-30/a.mp4",
                        (2026, 5, 30, 12, 9, 22),
                        (2026, 5, 30, 12, 14, 21),
                        768596555,
                    ),
                ],
            }
        }
        may_31 = {
            "SearchResult": {
                "channel": 0,
                "File": [
                    _file_dict(
                        "/mnt/sda/Mp4Record/2026-05-31/b.mp4",
                        (2026, 5, 31, 9, 42, 44),
                        (2026, 5, 31, 9, 47, 43),
                        768906876,
                    ),
                ],
            }
        }

        mock_client = AsyncMock()
        mock_client.post.side_effect = [
            _login_response(),
            _success_response("Search", bitmap_initial),
            _success_response("Search", may_30),
            _success_response("Search", may_31),
        ]

        camera = ReolinkCamera(
            config=_make_config(), storage_path="test_path", client=mock_client
        )

        files = await camera.get_file_list(
            datetime(2026, 5, 30, 0, 0, 0),
            datetime(2026, 5, 31, 23, 59, 59),
        )

        assert len(files) == 2
        paths = [f["path"] for f in files]
        assert any("2026-05-30" in p for p in paths)
        assert any("2026-05-31" in p for p in paths)

    @pytest.mark.asyncio
    @patch(
        "video_grouper.cameras.reolink.ReolinkCamera._log_http_call",
        new_callable=AsyncMock,
    )
    async def test_get_file_size_success(self, mock_log):
        mock_client = AsyncMock()

        # Login response
        mock_client.post.return_value = _login_response()

        # HEAD response for file size
        head_resp = MagicMock()
        head_resp.status_code = 200
        head_resp.headers = {"content-length": "1048576"}
        mock_client.head.return_value = head_resp

        camera = ReolinkCamera(
            config=_make_config(), storage_path="test_path", client=mock_client
        )

        size = await camera.get_file_size("Rec/test.mp4")
        assert size == 1048576


# ── Recording control ─────────────────────────────────────────────────


class TestReolinkCameraRecording:
    @pytest.mark.asyncio
    @patch(
        "video_grouper.cameras.reolink.ReolinkCamera._log_http_call",
        new_callable=AsyncMock,
    )
    async def test_stop_recording_disables_enable(self, mock_log):
        """stop_recording sends SetRecV20 with enable=0."""
        mock_client = AsyncMock()
        mock_client.post.side_effect = [
            _login_response(),
            _success_response("SetRecV20", {}),
        ]

        camera = ReolinkCamera(
            config=_make_config(), storage_path="test_path", client=mock_client
        )

        result = await camera.stop_recording()
        assert result is True
        # Verify the payload sent has enable=0
        call_args = mock_client.post.call_args_list[-1]
        payload = call_args[1]["json"][0]["param"]
        assert payload["Rec"]["enable"] == 0

    @pytest.mark.asyncio
    @patch(
        "video_grouper.cameras.reolink.ReolinkCamera._log_http_call",
        new_callable=AsyncMock,
    )
    async def test_start_recording_enables(self, mock_log):
        """start_recording sends SetRecV20 with enable=1."""
        mock_client = AsyncMock()
        mock_client.post.side_effect = [
            _login_response(),
            _success_response("SetRecV20", {}),
        ]

        camera = ReolinkCamera(
            config=_make_config(), storage_path="test_path", client=mock_client
        )

        result = await camera.start_recording()
        assert result is True
        call_args = mock_client.post.call_args_list[-1]
        payload = call_args[1]["json"][0]["param"]
        assert payload["Rec"]["enable"] == 1

    @pytest.mark.asyncio
    @patch(
        "video_grouper.cameras.reolink.ReolinkCamera._log_http_call",
        new_callable=AsyncMock,
    )
    async def test_get_recording_status_timing_active(self, mock_log):
        """Returns True when TIMING schedule has active slots."""
        mock_client = AsyncMock()
        mock_client.post.side_effect = [
            _login_response(),
            _success_response(
                "GetRecV20",
                {
                    "Rec": {
                        "channel": 0,
                        "enable": 1,
                        "schedule": {"table": {"TIMING": "1" * 168}},
                    }
                },
            ),
        ]

        camera = ReolinkCamera(
            config=_make_config(), storage_path="test_path", client=mock_client
        )

        assert await camera.get_recording_status() is True

    @pytest.mark.asyncio
    @patch(
        "video_grouper.cameras.reolink.ReolinkCamera._log_http_call",
        new_callable=AsyncMock,
    )
    async def test_get_recording_status_disabled(self, mock_log):
        """Returns False when enable=0."""
        mock_client = AsyncMock()
        mock_client.post.side_effect = [
            _login_response(),
            _success_response(
                "GetRecV20",
                {
                    "Rec": {
                        "channel": 0,
                        "enable": 0,
                    }
                },
            ),
        ]

        camera = ReolinkCamera(
            config=_make_config(), storage_path="test_path", client=mock_client
        )

        assert await camera.get_recording_status() is False


# ── File deletion ────────────────────────────────────────────────────


class TestReolinkCameraDeleteFiles:
    def test_supports_file_deletion_is_false(self, tmp_path):
        """Reolink cameras do not support programmatic file deletion."""
        camera = ReolinkCamera(config=_make_config(), storage_path=str(tmp_path))
        assert camera.supports_file_deletion is False

    @pytest.mark.asyncio
    async def test_delete_files_returns_zero(self):
        """delete_files always returns 0 (unsupported by Reolink API)."""
        camera = ReolinkCamera(
            config=_make_config(), storage_path="test_path", client=AsyncMock()
        )
        result = await camera.delete_files(["Rec/file1.mp4", "Rec/file2.mp4"])
        assert result == 0

    @pytest.mark.asyncio
    async def test_delete_files_empty_list(self):
        camera = ReolinkCamera(
            config=_make_config(), storage_path="test_path", client=AsyncMock()
        )
        result = await camera.delete_files([])
        assert result == 0


# ── Device info ───────────────────────────────────────────────────────


class TestReolinkCameraDeviceInfo:
    @pytest.mark.asyncio
    @patch(
        "video_grouper.cameras.reolink.ReolinkCamera._log_http_call",
        new_callable=AsyncMock,
    )
    async def test_get_device_info_success(self, mock_log):
        mock_client = AsyncMock()
        mock_client.post.side_effect = [
            _login_response(),
            _success_response(
                "GetDevInfo",
                {
                    "DevInfo": {
                        "name": "Front Camera",
                        "type": "IPC",
                        "firmVer": "v3.1.0",
                        "serial": "SN12345",
                        "mac": "AA:BB:CC:DD:EE:FF",
                        "model": "RLC-810A",
                    }
                },
            ),
        ]

        camera = ReolinkCamera(
            config=_make_config(), storage_path="test_path", client=mock_client
        )

        info = await camera.get_device_info()
        assert info["device_name"] == "Front Camera"
        assert info["device_type"] == "IPC"
        assert info["firmware_version"] == "v3.1.0"
        assert info["serial_number"] == "SN12345"
        assert info["mac_address"] == "AA:BB:CC:DD:EE:FF"
        assert info["model"] == "RLC-810A"
        assert info["manufacturer"] == "Reolink"
        assert info["ip_address"] == "192.168.1.200"

    @pytest.mark.asyncio
    @patch(
        "video_grouper.cameras.reolink.ReolinkCamera._log_http_call",
        new_callable=AsyncMock,
    )
    async def test_get_device_info_failure(self, mock_log):
        mock_client = AsyncMock()
        mock_client.post.side_effect = [
            _login_response(),
            _error_response("GetDevInfo"),
        ]

        camera = ReolinkCamera(
            config=_make_config(), storage_path="test_path", client=mock_client
        )

        info = await camera.get_device_info()
        assert info["device_name"] == ""
        assert info["ip_address"] == "192.168.1.200"
        assert info["manufacturer"] == "Reolink"


# ── Connection state ──────────────────────────────────────────────────


class TestReolinkConnectionState:
    def test_get_connected_timeframes_empty(self, tmp_path):
        camera = ReolinkCamera(config=_make_config(), storage_path=str(tmp_path))
        assert camera.get_connected_timeframes() == []

    def test_get_connected_timeframes_with_events(self, tmp_path):
        camera = ReolinkCamera(config=_make_config(), storage_path=str(tmp_path))
        camera._connection_events = [
            {
                "event_datetime": "2024-01-01T12:00:00-05:00",
                "event_type": "connected",
                "message": "Connected",
            },
            {
                "event_datetime": "2024-01-01T14:00:00-05:00",
                "event_type": "disconnected",
                "message": "Disconnected",
            },
        ]
        timeframes = camera.get_connected_timeframes()
        assert len(timeframes) == 1
        assert timeframes[0][0].hour == 12
        assert timeframes[0][1].hour == 14


# ── Datetime conversion helpers ───────────────────────────────────────


class TestReolinkHelpers:
    def test_datetime_to_reolink(self):
        dt = datetime(2024, 3, 15, 14, 30, 45)
        result = ReolinkCamera._datetime_to_reolink(dt)
        assert result == {
            "year": 2024,
            "mon": 3,
            "day": 15,
            "hour": 14,
            "min": 30,
            "sec": 45,
        }

    def test_reolink_to_datetime_str(self):
        t = {"year": 2024, "mon": 1, "day": 5, "hour": 8, "min": 3, "sec": 7}
        result = ReolinkCamera._reolink_to_datetime_str(t)
        assert result == "2024-01-05 08:03:07"


# ── File size from search metadata ───────────────────────────────────


class TestReolinkFileSizeCache:
    @pytest.mark.asyncio
    @patch(
        "video_grouper.cameras.reolink.ReolinkCamera._log_http_call",
        new_callable=AsyncMock,
    )
    async def test_get_file_size_from_cache(self, mock_log):
        """get_file_size returns cached size from search results."""
        camera = ReolinkCamera(
            config=_make_config(), storage_path="test_path", client=AsyncMock()
        )
        camera._file_sizes = {"Rec/test.mp4": 450_000_000}
        size = await camera.get_file_size("Rec/test.mp4")
        assert size == 450_000_000

    @pytest.mark.asyncio
    @patch(
        "video_grouper.cameras.reolink.ReolinkCamera._log_http_call",
        new_callable=AsyncMock,
    )
    async def test_file_sizes_populated_by_search(self, mock_log):
        """get_file_list populates the file size cache."""
        mock_client = AsyncMock()
        search_value = {
            "SearchResult": {
                "Status": 0,
                "File": [
                    {
                        "name": "Rec/20240101/clip.mp4",
                        "StartTime": {
                            "year": 2024,
                            "mon": 1,
                            "day": 1,
                            "hour": 12,
                            "min": 0,
                            "sec": 0,
                        },
                        "EndTime": {
                            "year": 2024,
                            "mon": 1,
                            "day": 1,
                            "hour": 12,
                            "min": 30,
                            "sec": 0,
                        },
                        "size": 320_256_446,
                    },
                ],
            }
        }
        mock_client.post.side_effect = [
            _login_response(),
            _success_response("Search", search_value),
        ]

        camera = ReolinkCamera(
            config=_make_config(), storage_path="test_path", client=mock_client
        )
        await camera.get_file_list(datetime(2024, 1, 1), datetime(2024, 1, 2))

        assert camera._file_sizes["Rec/20240101/clip.mp4"] == 320_256_446


# ── Download via Baichuan ────────────────────────────────────────────


class TestReolinkCameraDownload:
    @pytest.mark.asyncio
    @patch("video_grouper.cameras.reolink.download_and_mux", new_callable=AsyncMock)
    async def test_download_file_success(self, mock_download, tmp_path):
        mock_download.return_value = True

        camera = ReolinkCamera(
            config=_make_config(), storage_path=str(tmp_path), client=AsyncMock()
        )
        local_path = str(tmp_path / "video" / "file.mp4")

        result = await camera.download_file("Rec/test.mp4", local_path)

        assert result is True
        mock_download.assert_awaited_once()
        call_kwargs = mock_download.call_args[1]
        assert call_kwargs["host"] == "192.168.1.200"
        assert call_kwargs["port"] == 9000
        assert call_kwargs["username"] == "admin"
        assert call_kwargs["password"] == "admin"
        assert call_kwargs["file_path"] == "Rec/test.mp4"
        assert call_kwargs["output_mp4"] == local_path
        assert call_kwargs["channel"] == 0

    @pytest.mark.asyncio
    @patch("video_grouper.cameras.reolink.download_and_mux", new_callable=AsyncMock)
    async def test_download_file_failure(self, mock_download, tmp_path):
        mock_download.return_value = False

        camera = ReolinkCamera(
            config=_make_config(), storage_path=str(tmp_path), client=AsyncMock()
        )
        local_path = str(tmp_path / "video" / "file.mp4")

        result = await camera.download_file("Rec/test.mp4", local_path)
        assert result is False

    @pytest.mark.asyncio
    @patch("video_grouper.cameras.reolink.download_and_mux", new_callable=AsyncMock)
    async def test_download_file_exception(self, mock_download, tmp_path):
        mock_download.side_effect = Exception("network error")

        camera = ReolinkCamera(
            config=_make_config(), storage_path=str(tmp_path), client=AsyncMock()
        )
        local_path = str(tmp_path / "video" / "file.mp4")

        result = await camera.download_file("Rec/test.mp4", local_path)
        assert result is False

    @pytest.mark.asyncio
    @patch("video_grouper.cameras.reolink.download_and_mux", new_callable=AsyncMock)
    async def test_download_uses_baichuan_port(self, mock_download, tmp_path):
        """Verify the configured baichuan_port is used."""
        mock_download.return_value = True
        config = CameraConfig(
            name="default",
            type="reolink",
            device_ip="10.0.0.1",
            username="user",
            password="pass",
            channel=1,
            baichuan_port=9001,
        )

        camera = ReolinkCamera(
            config=config, storage_path=str(tmp_path), client=AsyncMock()
        )
        await camera.download_file("Rec/test.mp4", str(tmp_path / "out.mp4"))

        call_kwargs = mock_download.call_args[1]
        assert call_kwargs["port"] == 9001
        assert call_kwargs["channel"] == 1
