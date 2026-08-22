"""Tests for network camera discovery in the setup wizard.

Covers the two things the wizard leans on: that a ProbeMatch's advertised
scopes survive parsing (so a camera can be shown as more than a bare address
before anyone types a password), and that the make is worked out by asking the
camera rather than by asking the user.
"""

from unittest.mock import AsyncMock, patch

import pytest

from video_grouper.cameras.discovery import (
    DiscoveredCamera,
    DiscoveredDevice,
    _extract_devices_from_probe_match,
    _parse_scopes,
    _sort_key,
    identify_camera,
)

# A two-camera reply: a Reolink and a Dahua, each with its own scopes. Shaped
# like the real thing -- one datagram can carry several ProbeMatch elements.
TWO_CAMERAS = b"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
  <s:Body>
    <d:ProbeMatches>
      <d:ProbeMatch>
        <d:Scopes>onvif://www.onvif.org/name/RLC-810A onvif://www.onvif.org/hardware/RLC-810A onvif://www.onvif.org/manufacturer/Reolink</d:Scopes>
        <d:XAddrs>http://192.168.1.50/onvif/device_service</d:XAddrs>
      </d:ProbeMatch>
      <d:ProbeMatch>
        <d:Scopes>onvif://www.onvif.org/name/IPC-HDW2431T onvif://www.onvif.org/manufacturer/Dahua</d:Scopes>
        <d:XAddrs>http://192.168.1.60/onvif/device_service</d:XAddrs>
      </d:ProbeMatch>
    </d:ProbeMatches>
  </s:Body>
</s:Envelope>"""


class TestScopeParsing:
    def test_name_hardware_and_vendor_are_extracted(self):
        name, hardware, vendor = _parse_scopes(
            "onvif://www.onvif.org/name/RLC-810A "
            "onvif://www.onvif.org/hardware/IPC-1 "
            "onvif://www.onvif.org/manufacturer/Reolink"
        )
        assert (name, hardware, vendor) == ("RLC-810A", "IPC-1", "Reolink")

    def test_percent_escapes_are_decoded(self):
        """Scopes are URIs, so a name with a space arrives percent-encoded."""
        name, _, _ = _parse_scopes("onvif://www.onvif.org/name/Front%20Door")
        assert name == "Front Door"

    def test_vendor_is_blank_when_nothing_identifies_it(self):
        _, _, vendor = _parse_scopes("onvif://www.onvif.org/Profile/Streaming")
        assert vendor == ""

    def test_amcrest_is_treated_as_dahua(self):
        """Amcrest units speak the Dahua CGI API."""
        _, _, vendor = _parse_scopes("onvif://www.onvif.org/name/Amcrest-IP2M")
        assert vendor == "Dahua"


class TestProbeMatchToDevices:
    def test_each_match_keeps_its_own_scopes(self):
        """The classic bug here is one camera's model landing on another's IP."""
        devices = {d.ip: d for d in _extract_devices_from_probe_match(TWO_CAMERAS)}
        assert set(devices) == {"192.168.1.50", "192.168.1.60"}
        assert devices["192.168.1.50"].vendor == "Reolink"
        assert devices["192.168.1.50"].name == "RLC-810A"
        assert devices["192.168.1.60"].vendor == "Dahua"
        assert devices["192.168.1.60"].name == "IPC-HDW2431T"

    def test_device_without_scopes_still_appears(self):
        """An unrecognised camera is still worth offering, by address."""
        xml = b"""<d:ProbeMatches xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
          <d:ProbeMatch><d:XAddrs>http://10.0.0.7/onvif/device_service</d:XAddrs></d:ProbeMatch>
        </d:ProbeMatches>"""
        devices = _extract_devices_from_probe_match(xml)
        assert [d.ip for d in devices] == ["10.0.0.7"]
        assert devices[0].vendor == ""

    def test_invalid_xml_is_not_fatal(self):
        assert _extract_devices_from_probe_match(b"not xml") == []

    def test_label_falls_back_when_nothing_was_advertised(self):
        assert DiscoveredDevice("10.0.0.7").label == ""
        assert DiscoveredDevice("10.0.0.7", vendor="Dahua").label == "Dahua"
        assert (
            DiscoveredDevice("10.0.0.7", name="RLC-810A", vendor="Reolink").label
            == "Reolink RLC-810A"
        )


class TestAddressSorting:
    def test_addresses_sort_numerically(self):
        """.9 before .10 -- lexical order puts them the wrong way round."""
        ips = ["192.168.1.10", "192.168.1.9", "192.168.1.100"]
        assert sorted(ips, key=_sort_key) == [
            "192.168.1.9",
            "192.168.1.10",
            "192.168.1.100",
        ]

    def test_hostname_does_not_crash_the_sort(self):
        assert _sort_key("camera.local")[0] == 1


def _camera(manufacturer):
    return DiscoveredCamera(
        ip="192.168.1.50",
        name="Field",
        model="RLC-810A",
        mac="aa:bb",
        firmware="1.0",
        serial="123",
        manufacturer=manufacturer,
    )


class TestIdentifyCamera:
    @pytest.mark.asyncio
    async def test_reolink_is_reported_without_asking_the_user(self):
        with (
            patch(
                "video_grouper.cameras.discovery.probe_reolink",
                new=AsyncMock(return_value=_camera("Reolink")),
            ),
            patch(
                "video_grouper.cameras.discovery.probe_dahua", new=AsyncMock()
            ) as dahua,
        ):
            result = await identify_camera("192.168.1.50", "admin", "pw")
        assert result is not None
        assert result[0] == "reolink"
        dahua.assert_not_awaited(), "a Reolink answer must not also probe Dahua"

    @pytest.mark.asyncio
    async def test_falls_through_to_dahua(self):
        with (
            patch(
                "video_grouper.cameras.discovery.probe_reolink",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "video_grouper.cameras.discovery.probe_dahua",
                new=AsyncMock(return_value=_camera("Dahua")),
            ),
        ):
            result = await identify_camera("192.168.1.60", "admin", "pw")
        assert result is not None and result[0] == "dahua"

    @pytest.mark.asyncio
    async def test_neither_answering_is_not_an_exception(self):
        """Wrong password looks the same as no camera; both return None."""
        with (
            patch(
                "video_grouper.cameras.discovery.probe_reolink",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "video_grouper.cameras.discovery.probe_dahua",
                new=AsyncMock(return_value=None),
            ),
        ):
            assert await identify_camera("192.168.1.99", "admin", "wrong") is None
