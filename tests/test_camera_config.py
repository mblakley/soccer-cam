"""Tests for camera configuration push methods (get/apply settings, password change)."""

import pytest
import httpx

from video_grouper.utils.config import CameraConfig


# ── Helpers ────────────────────────────────────────────────────────


def _dahua_camera(tmp_path, responses=None):
    """Create a DahuaCamera with a mock httpx client."""
    from video_grouper.cameras.dahua import DahuaCamera

    config = CameraConfig(
        name="test-dahua",
        type="dahua",
        device_ip="192.168.1.100",
        username="admin",
        password="admin",
    )
    client = MockHttpxClient(responses or {})
    return DahuaCamera(config, str(tmp_path), client=client)


def _reolink_camera(tmp_path, responses=None):
    """Create a ReolinkCamera with a mock httpx client."""
    from video_grouper.cameras.reolink import ReolinkCamera

    config = CameraConfig(
        name="test-reolink",
        type="reolink",
        device_ip="192.168.1.101",
        username="admin",
        password="admin",
    )
    client = MockHttpxClient(responses or {})
    cam = ReolinkCamera(config, str(tmp_path), client=client)
    # Pre-set a valid token to skip login
    cam._token = "mock_token"
    cam._token_expiry = 99999999999
    return cam


class MockResponse:
    """Minimal mock for httpx.Response."""

    def __init__(self, status_code=200, text="", json_data=None):
        self.status_code = status_code
        self._text = text
        self._json_data = json_data
        self.request = type("Req", (), {"method": "GET", "url": "", "headers": {}})()

    @property
    def text(self):
        return self._text

    def json(self):
        return self._json_data


class MockHttpxClient:
    """Mock httpx.AsyncClient that returns canned responses based on URL patterns."""

    def __init__(self, responses: dict):
        self._responses = responses
        self._calls: list[tuple[str, str]] = []  # (method, url)

    async def get(self, url, **kwargs):
        self._calls.append(("GET", url))
        for pattern, response in self._responses.items():
            if pattern in url:
                return response
        return MockResponse(status_code=404, text="Not found")

    async def post(self, url, **kwargs):
        self._calls.append(("POST", url))
        json_body = kwargs.get("json", [])
        cmd = ""
        if json_body and isinstance(json_body, list):
            cmd = json_body[0].get("cmd", "")
        for pattern, response in self._responses.items():
            if pattern in url or pattern == cmd:
                return response
        return MockResponse(status_code=404, text="Not found")

    async def aclose(self):
        pass

    async def stream(self, *args, **kwargs):
        raise NotImplementedError


# ── Dahua Tests ────────────────────────────────────────────────────


class TestDahuaGetSettings:
    @pytest.mark.asyncio
    async def test_reads_all_settings(self, tmp_path):
        cam = _dahua_camera(
            tmp_path,
            {
                "name=RecordMode": MockResponse(text="table.RecordMode[0].Mode=1\n"),
                "name=NTP": MockResponse(
                    text=(
                        "table.NTP.Enable=true\n"
                        "table.NTP.Address=pool.ntp.org\n"
                        "table.NTP.TimeZoneDesc=Easterntime\n"
                    )
                ),
                "name=Encode": MockResponse(
                    text=(
                        "table.Encode[0].MainFormat[0].Video.Compression=H.264\n"
                        "table.Encode[0].MainFormat[0].Video.BitRate=8192\n"
                        "table.Encode[0].MainFormat[0].Video.FPS=25\n"
                        "table.Encode[0].MainFormat[0].Video.resolution=4096x1800\n"
                    )
                ),
                "name=Locales": MockResponse(text="table.Locales.DSTEnable=true\n"),
                "name=Network": MockResponse(
                    text=(
                        "table.Network.eth0.DhcpEnable=false\n"
                        "table.Network.eth0.IPAddress=192.168.1.100\n"
                    )
                ),
            },
        )
        results = await cam.get_current_settings()
        assert len(results) == 5

        rec = next(r for r in results if r["setting"] == "recording")
        assert rec["success"] is True
        assert "Always on" in rec["current_value"]

        ntp = next(r for r in results if r["setting"] == "ntp")
        assert ntp["success"] is True
        assert "Enabled" in ntp["current_value"]
        assert "pool.ntp.org" in ntp["current_value"]

        enc = next(r for r in results if r["setting"] == "encoding")
        assert enc["success"] is True
        assert "H.264" in enc["current_value"]

        dst = next(r for r in results if r["setting"] == "dst")
        assert dst["success"] is True
        assert "Enabled" in dst["current_value"]

        net = next(r for r in results if r["setting"] == "network")
        assert net["success"] is True
        assert "Static" in net["current_value"]

    @pytest.mark.asyncio
    async def test_handles_failure(self, tmp_path):
        cam = _dahua_camera(tmp_path, {})  # No responses -> 404
        results = await cam.get_current_settings()
        assert len(results) == 5
        assert all(not r["success"] for r in results)


class TestDahuaApplySettings:
    @pytest.mark.asyncio
    async def test_applies_all_settings(self, tmp_path):
        cam = _dahua_camera(
            tmp_path,
            {
                "setConfig": MockResponse(text="OK\n"),
                "name=Encode": MockResponse(
                    text=(
                        "table.Encode[0].MainFormat[0].Video.FPS=15\n"
                        "table.Encode[0].MainFormat[0].Video.BitRate=4096\n"
                    )
                ),
                "name=Network": MockResponse(
                    text="table.Network.eth0.DhcpEnable=true\n"
                ),
            },
        )
        results = await cam.apply_optimal_settings(timezone="America/New_York")
        assert len(results) == 5

        rec = next(r for r in results if r["setting"] == "recording")
        assert rec["success"] is True
        assert "Always on" in rec["applied_value"]

        ntp = next(r for r in results if r["setting"] == "ntp")
        assert ntp["success"] is True
        assert "pool.ntp.org" in ntp["applied_value"]

        enc = next(r for r in results if r["setting"] == "encoding")
        assert enc["success"] is True
        assert "FPS" in enc["applied_value"]
        assert "bitrate" in enc["applied_value"]

    @pytest.mark.asyncio
    async def test_encoding_no_changes_needed(self, tmp_path):
        cam = _dahua_camera(
            tmp_path,
            {
                "setConfig": MockResponse(text="OK\n"),
                "name=Encode": MockResponse(
                    text=(
                        "table.Encode[0].MainFormat[0].Video.FPS=25\n"
                        "table.Encode[0].MainFormat[0].Video.BitRate=8192\n"
                        "table.Encode[0].MainFormat[0].Video.GOP=50\n"
                        "table.Encode[0].MainFormat[0].Video.BitRateControl=CBR\n"
                        "table.Encode[0].MainFormat[0].Video.Profile=High\n"
                        "table.Encode[0].MainFormat[0].Video.Compression=H.264\n"
                    )
                ),
            },
        )
        results = await cam.apply_optimal_settings()
        enc = next(r for r in results if r["setting"] == "encoding")
        assert enc["success"] is True
        assert "No changes needed" in enc["applied_value"]


class TestDahuaPasswordChange:
    @pytest.mark.asyncio
    async def test_success(self, tmp_path):
        cam = _dahua_camera(
            tmp_path,
            {"modifyPassword": MockResponse(text="OK\n")},
        )
        assert await cam.change_camera_password("admin", "newpass123") is True
        assert cam.password == "newpass123"

    @pytest.mark.asyncio
    async def test_failure(self, tmp_path):
        cam = _dahua_camera(
            tmp_path,
            {"modifyPassword": MockResponse(text="Error: bad password\n")},
        )
        assert await cam.change_camera_password("wrong", "newpass") is False
        assert cam.password == "admin"  # unchanged


# ── Reolink Tests ──────────────────────────────────────────────────


def _reolink_ok(cmd, value=None):
    """Build a standard Reolink success response."""
    return MockResponse(json_data=[{"cmd": cmd, "code": 0, "value": value or {}}])


def _reolink_error(cmd):
    """Build a standard Reolink error response."""
    return MockResponse(json_data=[{"cmd": cmd, "code": 1}])


class TestReolinkGetSettings:
    @pytest.mark.asyncio
    async def test_reads_all_settings(self, tmp_path):
        cam = _reolink_camera(
            tmp_path,
            {
                "GetRecV20": _reolink_ok(
                    "GetRecV20",
                    {
                        "Rec": {
                            "enable": 1,
                            "schedule": {"table": {"TIMING": "1" * 168}},
                        }
                    },
                ),
                "GetNtp": _reolink_ok(
                    "GetNtp",
                    {"Ntp": {"enable": 1, "server": "pool.ntp.org"}},
                ),
                "GetEnc": _reolink_ok(
                    "GetEnc",
                    {
                        "Enc": {
                            "mainStream": {
                                "vType": "h265",
                                "bitRate": 6144,
                                "frameRate": 20,
                                "size": "7680*2160",
                            }
                        }
                    },
                ),
                "GetTime": _reolink_ok(
                    "GetTime",
                    {"Dst": {"enable": 1}, "Time": {"timeZone": 18000}},
                ),
                "GetLocalLink": _reolink_ok(
                    "GetLocalLink",
                    {
                        "LocalLink": {
                            "type": "Static",
                            "static": {"ip": "192.168.1.101"},
                        }
                    },
                ),
            },
        )
        results = await cam.get_current_settings()
        assert len(results) == 5

        rec = next(r for r in results if r["setting"] == "recording")
        assert rec["success"] is True
        assert "Always on" in rec["current_value"]

        ntp = next(r for r in results if r["setting"] == "ntp")
        assert ntp["success"] is True
        assert "Enabled" in ntp["current_value"]

        enc = next(r for r in results if r["setting"] == "encoding")
        assert enc["success"] is True
        assert "h265" in enc["current_value"]

        dst = next(r for r in results if r["setting"] == "dst")
        assert dst["success"] is True
        assert "Enabled" in dst["current_value"]

        net = next(r for r in results if r["setting"] == "network")
        assert net["success"] is True
        assert "Static" in net["current_value"]

    @pytest.mark.asyncio
    async def test_partial_schedule(self, tmp_path):
        cam = _reolink_camera(
            tmp_path,
            {
                "GetRecV20": _reolink_ok(
                    "GetRecV20",
                    {
                        "Rec": {
                            "enable": 1,
                            "schedule": {"table": {"TIMING": "1" * 100 + "0" * 68}},
                        }
                    },
                ),
                "GetNtp": _reolink_ok("GetNtp", {"Ntp": {"enable": 0}}),
                "GetEnc": _reolink_ok(
                    "GetEnc",
                    {
                        "Enc": {
                            "mainStream": {
                                "vType": "h264",
                                "bitRate": 4096,
                                "frameRate": 15,
                                "size": "3840*2160",
                            }
                        }
                    },
                ),
            },
        )
        results = await cam.get_current_settings()
        rec = next(r for r in results if r["setting"] == "recording")
        assert "partial" in rec["current_value"].lower()


class TestReolinkApplySettings:
    @pytest.mark.asyncio
    async def test_applies_all_settings(self, tmp_path):
        cam = _reolink_camera(
            tmp_path,
            {
                "SetRecV20": _reolink_ok("SetRecV20"),
                "SetNtp": _reolink_ok("SetNtp"),
                "SetTime": _reolink_ok("SetTime"),
                "GetEnc": _reolink_ok(
                    "GetEnc",
                    {
                        "Enc": {
                            "mainStream": {
                                "vType": "h265",
                                "bitRate": 4096,
                                "frameRate": 15,
                            }
                        }
                    },
                ),
                "SetEnc": _reolink_ok("SetEnc"),
                "GetTime": _reolink_ok(
                    "GetTime",
                    {"Dst": {"enable": 0}, "Time": {"timeZone": 18000}},
                ),
                "GetLocalLink": _reolink_ok(
                    "GetLocalLink",
                    {
                        "LocalLink": {
                            "type": "DHCP",
                            "mac": "AA:BB:CC:DD:EE:FF",
                            "static": {
                                "ip": "",
                                "mask": "255.255.255.0",
                                "gateway": "192.168.1.1",
                            },
                            "dns": {"auto": 1, "dns1": "", "dns2": ""},
                        }
                    },
                ),
                "SetLocalLink": _reolink_ok("SetLocalLink"),
            },
        )
        results = await cam.apply_optimal_settings()
        assert len(results) == 5
        assert all(r["success"] for r in results)

        enc = next(r for r in results if r["setting"] == "encoding")
        assert "FPS" in enc["applied_value"]
        assert "bitrate" in enc["applied_value"]

        dst = next(r for r in results if r["setting"] == "dst")
        assert dst["success"] is True
        assert "DST" in dst["applied_value"]

        net = next(r for r in results if r["setting"] == "network")
        assert net["success"] is True
        assert "Static" in net["applied_value"]

    @pytest.mark.asyncio
    async def test_encoding_no_changes_needed(self, tmp_path):
        cam = _reolink_camera(
            tmp_path,
            {
                "SetRecV20": _reolink_ok("SetRecV20"),
                "SetNtp": _reolink_ok("SetNtp"),
                "GetEnc": _reolink_ok(
                    "GetEnc",
                    {
                        "Enc": {
                            "mainStream": {
                                "vType": "h265",
                                "bitRate": 12288,
                                "frameRate": 20,
                                "gop": 40,
                                "profile": "High",
                            }
                        }
                    },
                ),
            },
        )
        results = await cam.apply_optimal_settings()
        enc = next(r for r in results if r["setting"] == "encoding")
        assert "No changes needed" in enc["applied_value"]


class TestReolinkPasswordChange:
    @pytest.mark.asyncio
    async def test_success(self, tmp_path):
        cam = _reolink_camera(
            tmp_path,
            {"ModifyUser": _reolink_ok("ModifyUser", {"rspCode": 200})},
        )
        assert await cam.change_camera_password("admin", "newpass123") is True
        assert cam.password == "newpass123"
        assert cam._token is None  # token invalidated

    @pytest.mark.asyncio
    async def test_failure(self, tmp_path):
        cam = _reolink_camera(
            tmp_path,
            {"ModifyUser": _reolink_error("ModifyUser")},
        )
        assert await cam.change_camera_password("admin", "newpass") is False
        assert cam.password == "admin"


# ── Partial Failure Tests ──────────────────────────────────────────


class TestPartialFailure:
    @pytest.mark.asyncio
    async def test_dahua_partial_failure(self, tmp_path):
        """Recording succeeds, NTP fails, encoding fails."""
        cam = _dahua_camera(
            tmp_path,
            {
                "RecordMode[0].Mode=1": MockResponse(text="OK\n"),
                "NTP.Enable": MockResponse(status_code=500, text="Error"),
            },
        )
        results = await cam.apply_optimal_settings()
        rec = next(r for r in results if r["setting"] == "recording")
        assert rec["success"] is True

        ntp = next(r for r in results if r["setting"] == "ntp")
        assert ntp["success"] is False

    @pytest.mark.asyncio
    async def test_reolink_partial_failure(self, tmp_path):
        """Recording succeeds, NTP fails."""
        cam = _reolink_camera(
            tmp_path,
            {
                "SetRecV20": _reolink_ok("SetRecV20"),
                "SetNtp": _reolink_error("SetNtp"),
                "GetEnc": _reolink_ok(
                    "GetEnc",
                    {
                        "Enc": {
                            "mainStream": {
                                "bitRate": 12288,
                                "frameRate": 20,
                                "gop": 40,
                                "profile": "High",
                            }
                        }
                    },
                ),
            },
        )
        results = await cam.apply_optimal_settings()
        rec = next(r for r in results if r["setting"] == "recording")
        assert rec["success"] is True

        ntp = next(r for r in results if r["setting"] == "ntp")
        assert ntp["success"] is False


# ── probe_dahua Tests ──────────────────────────────────────────────


class TestProbeDahua:
    @pytest.mark.asyncio
    async def test_success(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock, patch
        from video_grouper.cameras import discovery

        response_text = (
            "deviceName=TestCam\n"
            "model=IPC-HFW2831T\n"
            "macAddress=AA:BB:CC:DD:EE:FF\n"
            "firmwareVersion=2.800.0000\n"
            "serialNumber=SN12345\n"
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = response_text

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "video_grouper.cameras.discovery.httpx.AsyncClient",
            return_value=mock_client,
        ):
            result = await discovery.probe_dahua("192.168.1.100", "admin", "admin")

        assert result is not None
        assert result.model == "IPC-HFW2831T"
        assert result.mac == "AA:BB:CC:DD:EE:FF"
        assert result.manufacturer == "Dahua"

    @pytest.mark.asyncio
    async def test_connection_refused(self, monkeypatch):
        from unittest.mock import AsyncMock, patch
        from video_grouper.cameras import discovery

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            side_effect=httpx.ConnectError("Connection refused")
        )
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "video_grouper.cameras.discovery.httpx.AsyncClient",
            return_value=mock_client,
        ):
            result = await discovery.probe_dahua("192.168.1.100", "admin", "admin")

        assert result is None

    @pytest.mark.asyncio
    async def test_bad_credentials(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock, patch
        from video_grouper.cameras import discovery

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "Unauthorized"

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "video_grouper.cameras.discovery.httpx.AsyncClient",
            return_value=mock_client,
        ):
            result = await discovery.probe_dahua("192.168.1.100", "admin", "wrong")

        assert result is None
