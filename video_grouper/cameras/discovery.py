"""Camera auto-discovery and configuration via ONVIF WS-Discovery and Reolink API."""

from __future__ import annotations

import logging
import select
import socket
import struct
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# WS-Discovery multicast address and port
WS_DISCOVERY_MULTICAST = "239.255.255.250"
WS_DISCOVERY_PORT = 3702

# ONVIF WS-Discovery SOAP probe template
_PROBE_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"
            xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
            xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <s:Header>
    <a:Action s:mustUnderstand="1">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</a:Action>
    <a:MessageID>uuid:{msg_id}</a:MessageID>
    <a:ReplyTo>
      <a:Address>http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous</a:Address>
    </a:ReplyTo>
    <a:To s:mustUnderstand="1">urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To>
  </s:Header>
  <s:Body>
    <d:Probe>
      <d:Types>dn:NetworkVideoTransmitter</d:Types>
    </d:Probe>
  </s:Body>
</s:Envelope>"""


@dataclass
class DiscoveredCamera:
    """Information about a discovered camera."""

    ip: str
    name: str
    model: str
    mac: str
    firmware: str
    serial: str
    manufacturer: str


def _extract_ips_from_probe_match(xml_data: bytes) -> list[str]:
    """Parse WS-Discovery ProbeMatch XML to extract IP addresses from XAddrs."""
    ips = set()
    try:
        root = ET.fromstring(xml_data)
        # Search for XAddrs elements in any namespace
        for elem in root.iter():
            if elem.tag.endswith("XAddrs") and elem.text:
                for addr in elem.text.strip().split():
                    try:
                        parsed = urlparse(addr)
                        if parsed.hostname:
                            ips.add(parsed.hostname)
                    except Exception:
                        continue
    except ET.ParseError:
        logger.debug("Failed to parse WS-Discovery response XML")
    return list(ips)


def discover_onvif_devices(timeout: float = 3.0) -> list[str]:
    """Send WS-Discovery Probe and return list of discovered device IPs.

    Sends a SOAP Probe for NetworkVideoTransmitter devices via UDP multicast
    to 239.255.255.250:3702 and listens for responses.
    """
    msg_id = str(uuid.uuid4())
    probe = _PROBE_TEMPLATE.format(msg_id=msg_id).encode("utf-8")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setblocking(False)

        # Join multicast group on all interfaces (INADDR_ANY)
        mreq = struct.pack(
            "4s4s",
            socket.inet_aton(WS_DISCOVERY_MULTICAST),
            socket.inet_aton("0.0.0.0"),
        )
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

        # Set multicast TTL
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)

        # Send probe
        sock.sendto(probe, (WS_DISCOVERY_MULTICAST, WS_DISCOVERY_PORT))
        logger.debug("Sent WS-Discovery probe")

        all_ips: set[str] = set()
        deadline = time.monotonic() + timeout

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            ready, _, _ = select.select([sock], [], [], min(remaining, 0.5))
            if not ready:
                continue

            try:
                data, addr = sock.recvfrom(65535)
                ips = _extract_ips_from_probe_match(data)
                all_ips.update(ips)
            except BlockingIOError:
                continue
            except Exception as e:
                logger.debug(f"Error receiving WS-Discovery response: {e}")
                continue

        logger.info(f"WS-Discovery found {len(all_ips)} device(s)")
        return list(all_ips)

    except Exception as e:
        logger.error(f"WS-Discovery failed: {e}")
        return []
    finally:
        sock.close()


async def _login(
    client: httpx.AsyncClient, ip: str, username: str, password: str
) -> str | None:
    """Login to a Reolink camera and return the token, or None on failure."""
    url = f"http://{ip}/cgi-bin/api.cgi?cmd=Login&token=null"
    payload = [
        {
            "cmd": "Login",
            "action": 0,
            "param": {
                "User": {
                    "userName": username,
                    "password": password,
                }
            },
        }
    ]
    response = await client.post(url, json=payload)
    if response.status_code != 200:
        return None

    data = response.json()
    if not data or data[0].get("code") != 0:
        return None

    return data[0]["value"]["Token"]["name"]


async def probe_reolink(
    ip: str, username: str, password: str
) -> DiscoveredCamera | None:
    """Probe a Reolink camera at the given IP for device info.

    Attempts login and GetDevInfo. Returns a DiscoveredCamera on success,
    None on failure.
    """
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0)
        ) as client:
            token = await _login(client, ip, username, password)
            if token is None:
                return None

            # Get device info
            url = f"http://{ip}/cgi-bin/api.cgi?cmd=GetDevInfo&token={token}"
            payload = [
                {
                    "cmd": "GetDevInfo",
                    "action": 0,
                    "param": {"DevInfo": {"channel": 0}},
                }
            ]
            response = await client.post(url, json=payload)
            if response.status_code != 200:
                return None

            data = response.json()
            if not data or data[0].get("code") != 0:
                return None

            info = data[0]["value"]["DevInfo"]
            return DiscoveredCamera(
                ip=ip,
                name=info.get("name", ""),
                model=info.get("model", ""),
                mac=info.get("mac", ""),
                firmware=info.get("firmVer", ""),
                serial=info.get("serial", ""),
                manufacturer="Reolink",
            )
    except (httpx.ConnectError, httpx.RequestError) as e:
        logger.debug(f"Could not connect to {ip}: {e}")
        return None
    except Exception as e:
        logger.debug(f"Error probing {ip}: {e}")
        return None


async def configure_always_record(
    ip: str, username: str, password: str, channel: int = 0
) -> bool:
    """Enable always-on recording on a Reolink camera.

    Uses the SetRecV20 API with the TIMING schedule table set to all 1s
    for continuous recording (168 chars = 24h x 7 days).
    """
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0)
        ) as client:
            token = await _login(client, ip, username, password)
            if token is None:
                logger.error(
                    "CONFIGURE RECORDING FAILED: Could not login to camera at %s. "
                    "Check credentials.",
                    ip,
                )
                return False

            url = f"http://{ip}/cgi-bin/api.cgi?cmd=SetRecV20&token={token}"
            payload = [
                {
                    "cmd": "SetRecV20",
                    "action": 0,
                    "param": {
                        "Rec": {
                            "enable": 1,
                            "schedule": {
                                "channel": channel,
                                "table": {"TIMING": "1" * 168},
                            },
                        }
                    },
                }
            ]
            response = await client.post(url, json=payload)
            if response.status_code != 200:
                logger.error(
                    "CONFIGURE RECORDING FAILED: SetRecV20 returned HTTP %d "
                    "for camera at %s. The camera may use an unsupported API version.",
                    response.status_code,
                    ip,
                )
                return False

            data = response.json()
            if not data or data[0].get("code") != 0:
                error_detail = ""
                if data:
                    error_detail = data[0].get("error", {}).get("detail", "")
                logger.error(
                    "CONFIGURE RECORDING FAILED: SetRecV20 rejected by camera at %s. "
                    "Response: %s. The camera may use an unsupported API version.",
                    ip,
                    error_detail or data,
                )
                return False

            logger.info("Configured always-on recording on %s", ip)
            return True
    except Exception as e:
        logger.error(
            "CONFIGURE RECORDING FAILED: Unexpected error for camera at %s: %s",
            ip,
            e,
        )
        return False


async def change_password(
    ip: str, current_user: str, current_pass: str, new_pass: str
) -> bool:
    """Change the password for a user on a Reolink camera."""
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0)
        ) as client:
            token = await _login(client, ip, current_user, current_pass)
            if token is None:
                return False

            url = f"http://{ip}/cgi-bin/api.cgi?cmd=ModifyUser&token={token}"
            payload = [
                {
                    "cmd": "ModifyUser",
                    "action": 0,
                    "param": {
                        "User": {
                            "userName": current_user,
                            "password": new_pass,
                        }
                    },
                }
            ]
            response = await client.post(url, json=payload)
            if response.status_code != 200:
                return False

            data = response.json()
            return bool(data and data[0].get("code") == 0)
    except Exception as e:
        logger.error(f"Error changing password on {ip}: {e}")
        return False
