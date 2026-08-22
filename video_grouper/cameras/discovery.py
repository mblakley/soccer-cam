"""Camera auto-discovery and configuration via ONVIF WS-Discovery and Reolink API."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import select
import socket
import struct
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

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


@dataclass
class DiscoveredDevice:
    """A device that answered WS-Discovery, before we have credentials.

    Everything here comes out of the ProbeMatch itself, so it is available
    without logging in. ``vendor`` is a *guess* from the advertised scopes and
    may be ``""`` -- only :func:`identify_camera` can confirm it, because only
    a successful authenticated probe proves what the device actually is.
    """

    ip: str
    name: str = ""
    hardware: str = ""
    vendor: str = ""

    @property
    def label(self) -> str:
        """A human-readable one-liner, falling back to the address."""
        return " ".join(p for p in (self.vendor, self.name or self.hardware) if p)


#: Scope paths ONVIF devices use to advertise themselves, e.g.
#: ``onvif://www.onvif.org/name/RLC-810A``.
_SCOPE_NAME = "/name/"
_SCOPE_HARDWARE = "/hardware/"

#: Substrings that identify a vendor in an advertised scope. Only used for a
#: pre-credential hint in the UI -- never to decide how to talk to a device.
_VENDOR_HINTS = (("reolink", "Reolink"), ("dahua", "Dahua"), ("amcrest", "Dahua"))


def _sort_key(ip: str) -> tuple:
    """Sort addresses numerically, so .9 comes before .10 in the picker."""
    try:
        return (0, tuple(int(p) for p in ip.split(".")))
    except ValueError:
        return (1, ip)


def _parse_scopes(text: str) -> tuple[str, str, str]:
    """Return ``(name, hardware, vendor)`` from a Scopes element's text."""
    name = hardware = vendor = ""
    for scope in text.split():
        lowered = scope.lower()
        if _SCOPE_NAME in lowered and not name:
            name = unquote(scope.rsplit("/", 1)[-1])
        elif _SCOPE_HARDWARE in lowered and not hardware:
            hardware = unquote(scope.rsplit("/", 1)[-1])
        for needle, label in _VENDOR_HINTS:
            if needle in lowered:
                vendor = label
    return name, hardware, vendor


def _extract_devices_from_probe_match(xml_data: bytes) -> list[DiscoveredDevice]:
    """Parse a ProbeMatch into devices, keeping the advertised scopes.

    Each ``ProbeMatch`` carries its own XAddrs and Scopes, so they are read per
    match -- reading them document-wide would attach one device's model to
    another's address when several answer in one datagram.
    """
    devices: dict[str, DiscoveredDevice] = {}
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError:
        logger.debug("Failed to parse WS-Discovery response XML")
        return []

    matches = [e for e in root.iter() if e.tag.endswith("ProbeMatch")]
    # Some devices reply with a bare envelope; fall back to the whole document
    # so a non-conforming camera is still found.
    for match in matches or [root]:
        ips: list[str] = []
        name = hardware = vendor = ""
        for elem in match.iter():
            if elem.tag.endswith("XAddrs") and elem.text:
                for addr in elem.text.strip().split():
                    try:
                        host = urlparse(addr).hostname
                    except Exception:
                        continue
                    if host:
                        ips.append(host)
            elif elem.tag.endswith("Scopes") and elem.text:
                name, hardware, vendor = _parse_scopes(elem.text)
        for ip in ips:
            # First answer for an address wins; later ones only fill blanks.
            existing = devices.get(ip)
            if existing is None:
                devices[ip] = DiscoveredDevice(ip, name, hardware, vendor)
            else:
                existing.name = existing.name or name
                existing.hardware = existing.hardware or hardware
                existing.vendor = existing.vendor or vendor
    return list(devices.values())


def _extract_ips_from_probe_match(xml_data: bytes) -> list[str]:
    """Parse WS-Discovery ProbeMatch XML to extract IP addresses from XAddrs."""
    return [d.ip for d in _extract_devices_from_probe_match(xml_data)]


def discover_onvif_devices(timeout: float = 3.0) -> list[str]:
    """Send WS-Discovery Probe and return list of discovered device IPs."""
    return [d.ip for d in discover_onvif_details(timeout)]


def discover_onvif_details(timeout: float = 3.0) -> list[DiscoveredDevice]:
    """Send WS-Discovery Probe and return what each device advertised.

    Sends a SOAP Probe for NetworkVideoTransmitter devices via UDP multicast
    to 239.255.255.250:3702 and listens for responses. Link-local by design:
    it finds cameras on the same network segment as this machine, which is
    where a camera plugged in next to the recorder will be.

    Blocking. Call it off the event loop (``asyncio.to_thread``) -- it sits in
    ``select`` for the whole timeout, and a web request that does that inline
    stalls every other request for the duration.
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

        found: dict[str, DiscoveredDevice] = {}
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
                for device in _extract_devices_from_probe_match(data):
                    existing = found.get(device.ip)
                    if existing is None:
                        found[device.ip] = device
                    else:
                        # A camera answers more than once; keep the richest.
                        existing.name = existing.name or device.name
                        existing.hardware = existing.hardware or device.hardware
                        existing.vendor = existing.vendor or device.vendor
            except BlockingIOError:
                continue
            except Exception as e:
                logger.debug(f"Error receiving WS-Discovery response: {e}")
                continue

        logger.info("WS-Discovery found %d device(s)", len(found))
        return sorted(found.values(), key=lambda d: _sort_key(d.ip))

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


async def probe_dahua(ip: str, username: str, password: str) -> DiscoveredCamera | None:
    """Probe a Dahua camera at the given IP for device info.

    Attempts Digest-authenticated getSystemInfo. Returns a DiscoveredCamera
    on success, None on failure.
    """
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0)
        ) as client:
            url = f"http://{ip}/cgi-bin/magicBox.cgi?action=getSystemInfo"
            response = await client.get(url, auth=httpx.DigestAuth(username, password))
            if response.status_code != 200:
                return None

            info: dict[str, str] = {}
            for line in response.text.strip().split("\n"):
                if "=" in line:
                    key, value = line.split("=", 1)
                    info[key.strip()] = value.strip()

            return DiscoveredCamera(
                ip=ip,
                name=info.get("deviceName", ""),
                model=info.get("model", ""),
                mac=info.get("macAddress", ""),
                firmware=info.get("firmwareVersion", ""),
                serial=info.get("serialNumber", ""),
                manufacturer="Dahua",
            )
    except (httpx.ConnectError, httpx.RequestError) as e:
        logger.debug(f"Could not connect to {ip}: {e}")
        return None
    except Exception as e:
        logger.debug(f"Error probing Dahua at {ip}: {e}")
        return None


#: Ports worth knocking on. 80 is the management UI on both vendors.
_PROBE_PORT = 80

#: How many hosts to sweep at once, and how long to wait for a TCP handshake.
#: A camera on the same switch answers in single-digit milliseconds; anything
#: that has not answered in half a second is not on this segment. 256 at a
#: time keeps a multi-homed box (this one has five sweepable networks, so
#: ~1270 addresses) inside the ONVIF probe's own 3s, since the two run
#: concurrently and the scan costs whichever is slower.
_SWEEP_CONCURRENCY = 256
_CONNECT_TIMEOUT = 0.5

#: Refuse to sweep anything larger than a /22. A /16 is 65k hosts, which is
#: not a scan, it is an outage.
_MIN_PREFIX = 22


def local_ipv4_networks() -> list[ipaddress.IPv4Network]:
    """Return the IPv4 networks this machine is directly attached to.

    Link-local (169.254/16) and loopback are dropped -- nothing is reachable
    there. Virtual adapters (WSL, Hyper-V, Docker) are left in on purpose: a
    camera on a bridged network would otherwise be invisible, and sweeping an
    empty subnet costs a few hundred milliseconds.
    """
    networks: list[ipaddress.IPv4Network] = []
    for _family, _type, _proto, _canon, sockaddr in socket.getaddrinfo(
        socket.gethostname(), None, socket.AF_INET
    ):
        addr = ipaddress.IPv4Address(sockaddr[0])
        if addr.is_loopback or addr.is_link_local:
            continue
        # getaddrinfo does not give a netmask; assume the common /24, which is
        # what home and small-office networks use.
        net = ipaddress.IPv4Network(f"{addr}/24", strict=False)
        if net.prefixlen >= _MIN_PREFIX and net not in networks:
            networks.append(net)
    return networks


async def _fingerprint(ip: str, client: httpx.AsyncClient) -> DiscoveredDevice | None:
    """Ask an address what it is, without credentials.

    Both vendors identify themselves in their rejection of an unauthenticated
    request, which is enough to label the device in the picker before anyone
    types a password:

      Reolink  POST /cgi-bin/api.cgi -> JSON body carrying rspCode -6
               ("please login first")
      Dahua    GET  /cgi-bin/magicBox.cgi -> 401 with a Digest challenge
    """
    try:
        resp = await client.post(
            f"http://{ip}/cgi-bin/api.cgi?cmd=GetDevInfo&token=null",
            json=[{"cmd": "GetDevInfo", "action": 0, "param": {}}],
        )
        if resp.status_code == 200 and "rspCode" in resp.text:
            return DiscoveredDevice(ip=ip, vendor="Reolink")
    except Exception:
        pass

    try:
        resp = await client.get(
            f"http://{ip}/cgi-bin/magicBox.cgi?action=getSystemInfo"
        )
        if (
            resp.status_code == 401
            and "digest" in resp.headers.get("www-authenticate", "").lower()
        ):
            return DiscoveredDevice(ip=ip, vendor="Dahua")
    except Exception:
        pass

    return None


async def _reachable(ip: str, sem: asyncio.Semaphore) -> str | None:
    """Return ``ip`` if something accepts a connection on the probe port."""
    async with sem:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, _PROBE_PORT), timeout=_CONNECT_TIMEOUT
            )
        except (TimeoutError, OSError):
            return None
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return ip


async def discover_on_lan(
    networks: list[ipaddress.IPv4Network] | None = None,
) -> list[DiscoveredDevice]:
    """Find cameras by sweeping the local network and fingerprinting hosts.

    This exists because WS-Discovery is not enough on its own: Reolink ships
    with ONVIF **off** (``GetNetPort`` reports ``onvifEnable: 0`` out of the
    box), so an ONVIF-only scan finds nothing for most Reolink owners. A
    Reolink Duo 3 PoE on the test bench is invisible to a probe and obvious to
    a fingerprint.

    Two passes so it stays quick: a wide TCP knock to find what is even there,
    then an HTTP fingerprint of only the hosts that answered.
    """
    nets = local_ipv4_networks() if networks is None else networks
    hosts = [str(h) for net in nets for h in net.hosts()]
    if not hosts:
        return []

    sem = asyncio.Semaphore(_SWEEP_CONCURRENCY)
    reachable = [
        ip
        for ip in await asyncio.gather(*(_reachable(h, sem) for h in hosts))
        if ip is not None
    ]
    logger.info(
        "LAN sweep: %d host(s) answered on port %d across %d network(s)",
        len(reachable),
        _PROBE_PORT,
        len(nets),
    )
    if not reachable:
        return []

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(4.0, connect=2.0), verify=False
    ) as client:
        results = await asyncio.gather(
            *(_fingerprint(ip, client) for ip in reachable), return_exceptions=True
        )

    devices = [r for r in results if isinstance(r, DiscoveredDevice)]
    logger.info("LAN sweep identified %d camera(s)", len(devices))
    return devices


async def discover_cameras(timeout: float = 3.0) -> list[DiscoveredDevice]:
    """Find cameras by every means available, and merge the answers.

    ONVIF gives a model name without credentials but only when the owner has
    turned it on; the LAN sweep works regardless but knows only the vendor.
    Running both and merging means a camera shows up either way, with whatever
    detail could be had.
    """
    onvif, swept = await asyncio.gather(
        asyncio.to_thread(discover_onvif_details, timeout),
        discover_on_lan(),
        return_exceptions=True,
    )
    if isinstance(onvif, BaseException):
        logger.warning("ONVIF discovery failed: %s", onvif)
        onvif = []
    if isinstance(swept, BaseException):
        logger.warning("LAN sweep failed: %s", swept)
        swept = []

    merged: dict[str, DiscoveredDevice] = {d.ip: d for d in onvif}
    for device in swept:
        existing = merged.get(device.ip)
        if existing is None:
            merged[device.ip] = device
        else:
            # The sweep's vendor is evidence from the device's own API, so it
            # outranks a vendor guessed from an advertised ONVIF scope.
            existing.vendor = device.vendor or existing.vendor
    return sorted(merged.values(), key=lambda d: _sort_key(d.ip))


async def identify_camera(
    ip: str, username: str, password: str
) -> tuple[str, DiscoveredCamera] | None:
    """Work out what kind of camera is at ``ip`` by talking to it.

    Returns ``(camera_type, info)`` where ``camera_type`` is ``"reolink"`` or
    ``"dahua"``, or ``None`` if neither answered. This is what makes the type
    dropdown unnecessary: the device tells us what it is, and a successful
    authenticated probe is proof, where an advertised ONVIF scope is only a
    hint.

    Reolink is tried first because its probe is a single JSON POST that fails
    fast on a Dahua, whereas the Dahua probe negotiates Digest auth.
    """
    reolink = await probe_reolink(ip, username, password)
    if reolink is not None:
        return "reolink", reolink

    dahua = await probe_dahua(ip, username, password)
    if dahua is not None:
        return "dahua", dahua

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
