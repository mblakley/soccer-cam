"""Tests for camera auto-discovery and Reolink configuration."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from video_grouper.cameras.discovery import (
    DiscoveredCamera,
    _extract_ips_from_probe_match,
    discover_onvif_devices,
    probe_reolink,
    configure_always_record,
    change_password,
)


# ── Helpers ───────────────────────────────────────────────────────────


def _login_response(token="abc123"):
    """Successful login JSON response."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = [
        {
            "cmd": "Login",
            "code": 0,
            "value": {
                "Token": {"name": token, "leaseTime": 3600},
            },
        }
    ]
    return resp


def _login_failure_response():
    """Failed login JSON response."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = [
        {
            "cmd": "Login",
            "code": 1,
            "error": {"detail": "bad credentials"},
        }
    ]
    return resp


def _success_response(cmd, value):
    """Generic successful API JSON response."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = [{"cmd": cmd, "code": 0, "value": value}]
    return resp


def _error_response(cmd, code=1):
    """Generic error API JSON response."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = [
        {"cmd": cmd, "code": code, "error": {"detail": "some error"}}
    ]
    return resp


def _dev_info_value():
    """Standard GetDevInfo response value."""
    return {
        "DevInfo": {
            "name": "Front Camera",
            "model": "RLC-810A",
            "mac": "AA:BB:CC:DD:EE:FF",
            "firmVer": "v3.1.0",
            "serial": "SN12345",
        }
    }


# ── WS-Discovery XML parsing ─────────────────────────────────────────

PROBE_MATCH_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"
            xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
            xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <s:Header>
    <a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/ProbeMatches</a:Action>
  </s:Header>
  <s:Body>
    <d:ProbeMatches>
      <d:ProbeMatch>
        <d:XAddrs>http://192.168.1.100:8080/onvif/device_service http://192.168.1.100:80/onvif/device_service</d:XAddrs>
      </d:ProbeMatch>
      <d:ProbeMatch>
        <d:XAddrs>http://10.0.0.50/onvif/device_service</d:XAddrs>
      </d:ProbeMatch>
    </d:ProbeMatches>
  </s:Body>
</s:Envelope>"""


class TestWSDiscoveryParsing:
    def test_extract_ips_from_probe_match(self):
        """Parsing a ProbeMatch response extracts unique IPs from XAddrs."""
        ips = _extract_ips_from_probe_match(PROBE_MATCH_XML)
        assert set(ips) == {"192.168.1.100", "10.0.0.50"}

    def test_extract_ips_empty_xml(self):
        """Empty or minimal XML returns empty list."""
        ips = _extract_ips_from_probe_match(b"<root/>")
        assert ips == []

    def test_extract_ips_invalid_xml(self):
        """Invalid XML returns empty list without crashing."""
        ips = _extract_ips_from_probe_match(b"not xml at all")
        assert ips == []


class TestDiscoverOnvifDevices:
    @patch("video_grouper.cameras.discovery.time.monotonic")
    @patch("video_grouper.cameras.discovery.socket.socket")
    @patch("video_grouper.cameras.discovery.select.select")
    def test_discover_parses_responses(
        self, mock_select, mock_socket_cls, mock_monotonic
    ):
        """discover_onvif_devices returns IPs from received probe matches."""
        mock_sock = MagicMock()
        mock_socket_cls.return_value = mock_sock

        # Timeline: deadline calc=0, loop1 remaining=0.01, loop2 remaining=0.02, loop3=past deadline
        mock_monotonic.side_effect = [0.0, 0.01, 0.02, 1.0]

        # First select returns data, second returns empty, then loop exits via monotonic
        mock_select.side_effect = [
            ([mock_sock], [], []),
            ([], [], []),
        ]
        mock_sock.recvfrom.return_value = (PROBE_MATCH_XML, ("192.168.1.100", 3702))

        ips = discover_onvif_devices(timeout=0.1)

        assert "192.168.1.100" in ips
        assert "10.0.0.50" in ips
        mock_sock.sendto.assert_called_once()
        mock_sock.close.assert_called_once()

    @patch("video_grouper.cameras.discovery.time.monotonic")
    @patch("video_grouper.cameras.discovery.socket.socket")
    @patch("video_grouper.cameras.discovery.select.select")
    def test_discover_timeout_no_responses(
        self, mock_select, mock_socket_cls, mock_monotonic
    ):
        """No responses within timeout returns empty list."""
        mock_sock = MagicMock()
        mock_socket_cls.return_value = mock_sock
        # start=0, first check=1.0 (past deadline)
        mock_monotonic.side_effect = [0.0, 1.0]
        mock_select.return_value = ([], [], [])

        ips = discover_onvif_devices(timeout=0.1)

        assert ips == []
        mock_sock.close.assert_called_once()


# ── probe_reolink ────────────────────────────────────────────────────


class TestProbeReolink:
    @pytest.mark.asyncio
    @patch("video_grouper.cameras.discovery.httpx.AsyncClient")
    async def test_probe_success(self, mock_client_cls):
        """Successful login + GetDevInfo returns DiscoveredCamera."""
        mock_client = AsyncMock()
        mock_client.post.side_effect = [
            _login_response(),
            _success_response("GetDevInfo", _dev_info_value()),
        ]
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        result = await probe_reolink("192.168.1.100", "admin", "password123")

        assert result is not None
        assert isinstance(result, DiscoveredCamera)
        assert result.ip == "192.168.1.100"
        assert result.name == "Front Camera"
        assert result.model == "RLC-810A"
        assert result.mac == "AA:BB:CC:DD:EE:FF"
        assert result.firmware == "v3.1.0"
        assert result.serial == "SN12345"
        assert result.manufacturer == "Reolink"

    @pytest.mark.asyncio
    @patch("video_grouper.cameras.discovery.httpx.AsyncClient")
    async def test_probe_login_failure(self, mock_client_cls):
        """Login failure returns None."""
        mock_client = AsyncMock()
        mock_client.post.return_value = _login_failure_response()
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        result = await probe_reolink("192.168.1.100", "admin", "wrong")

        assert result is None

    @pytest.mark.asyncio
    @patch("video_grouper.cameras.discovery.httpx.AsyncClient")
    async def test_probe_connection_error(self, mock_client_cls):
        """Connection error returns None."""
        mock_client = AsyncMock()
        mock_client.post.side_effect = httpx.ConnectError("Connection refused")
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        result = await probe_reolink("192.168.1.100", "admin", "")

        assert result is None


# ── configure_always_record ──────────────────────────────────────────


class TestConfigureAlwaysRecord:
    @pytest.mark.asyncio
    @patch("video_grouper.cameras.discovery.httpx.AsyncClient")
    async def test_configure_success(self, mock_client_cls):
        """Successful login + SetRecV20 returns True."""
        mock_client = AsyncMock()
        mock_client.post.side_effect = [
            _login_response(),
            _success_response("SetRecV20", {"rspCode": 200}),
        ]
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        result = await configure_always_record("192.168.1.100", "admin", "pass")

        assert result is True
        # Verify SetRecV20 was called with TIMING table
        call_args = mock_client.post.call_args_list[1]
        payload = call_args[1]["json"] if "json" in call_args[1] else call_args[0][1]
        assert payload[0]["cmd"] == "SetRecV20"
        rec_param = payload[0]["param"]["Rec"]
        assert rec_param["schedule"]["table"]["TIMING"] == "1" * 168

    @pytest.mark.asyncio
    @patch("video_grouper.cameras.discovery.httpx.AsyncClient")
    async def test_configure_failure(self, mock_client_cls):
        """SetRecV20 error returns False."""
        mock_client = AsyncMock()
        mock_client.post.side_effect = [
            _login_response(),
            _error_response("SetRecV20"),
        ]
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        result = await configure_always_record("192.168.1.100", "admin", "pass")

        assert result is False


# ── change_password ──────────────────────────────────────────────────


class TestChangePassword:
    @pytest.mark.asyncio
    @patch("video_grouper.cameras.discovery.httpx.AsyncClient")
    async def test_change_password_success(self, mock_client_cls):
        """Successful login + ModifyUser returns True."""
        mock_client = AsyncMock()
        mock_client.post.side_effect = [
            _login_response(),
            _success_response("ModifyUser", {"rspCode": 200}),
        ]
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        result = await change_password("192.168.1.100", "admin", "old", "new")

        assert result is True

    @pytest.mark.asyncio
    @patch("video_grouper.cameras.discovery.httpx.AsyncClient")
    async def test_change_password_failure(self, mock_client_cls):
        """ModifyUser error returns False."""
        mock_client = AsyncMock()
        mock_client.post.side_effect = [
            _login_response(),
            _error_response("ModifyUser"),
        ]
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        result = await change_password("192.168.1.100", "admin", "old", "bad")

        assert result is False
