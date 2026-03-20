"""Dahua HTTP API simulator with Digest Auth.

Implements the Dahua camera HTTP API endpoints used by
video_grouper/cameras/dahua.py: mediaFileFind, RPC_Loadfile,
magicBox, recordManager, configManager.
"""

import hashlib
import logging
import os
import random
import string
from datetime import datetime

from aiohttp import web

logger = logging.getLogger(__name__)

# MediaFileFind session state
_find_sessions: dict[str, dict] = {}
_find_counter = 1000000

# Recording mode state
_record_mode = 0  # 0 = auto/continuous


def _random_nonce():
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=32))


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


class DigestAuthMiddleware:
    """HTTP Digest Auth validation matching Dahua camera behavior."""

    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self.realm = "DahuaCamera"
        self.nonces: set[str] = set()

    def challenge_response(self):
        nonce = _random_nonce()
        self.nonces.add(nonce)
        return web.Response(
            status=401,
            headers={
                "WWW-Authenticate": f'Digest realm="{self.realm}", nonce="{nonce}", qop="auth"'
            },
        )

    def validate(self, request: web.Request) -> bool:
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Digest "):
            return False

        params = {}
        for part in auth_header[7:].split(","):
            part = part.strip()
            if "=" in part:
                key, value = part.split("=", 1)
                params[key.strip()] = value.strip().strip('"')

        username = params.get("username", "")
        nonce = params.get("nonce", "")
        uri = params.get("uri", "")
        nc = params.get("nc", "")
        cnonce = params.get("cnonce", "")
        response_hash = params.get("response", "")

        if username != self.username or nonce not in self.nonces:
            return False

        ha1 = _md5(f"{self.username}:{self.realm}:{self.password}")
        ha2 = _md5(f"{request.method}:{uri}")
        expected = _md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")

        return response_hash == expected


def setup_routes(
    app: web.Application, storage, username: str, password: str, device_name: str
):
    """Register Dahua HTTP API routes."""
    auth = DigestAuthMiddleware(username, password)
    activity_log = app.get("activity_log")

    def _log_activity(msg):
        if activity_log is not None:
            activity_log.append(f"[HTTP] {msg}")

    def require_auth(handler):
        async def wrapper(request):
            if not auth.validate(request):
                _log_activity(f"Auth challenge: {request.path}")
                return auth.challenge_response()
            return await handler(request)

        return wrapper

    async def handle_get_caps(request):
        _log_activity("recordManager getCaps")
        return web.Response(text="table.MediaFileFind.MaxCount=100\n")

    async def handle_system_info(request):
        _log_activity("getSystemInfo")
        body = (
            f"deviceName={device_name}\n"
            "deviceType=IPC\n"
            "firmwareVersion=2.800.0000.0\n"
            "serialNumber=SIM00000000DAHUA\n"
            "macAddress=AA:BB:CC:DD:EE:FF\n"
            "model=IPC-HFW2831T\n"
            "manufacturer=Dahua\n"
        )
        return web.Response(text=body)

    async def handle_media_file_find(request):
        global _find_counter
        action = request.query.get("action", "")

        if action == "factory.create":
            _find_counter += 1
            object_id = str(_find_counter)
            _find_sessions[object_id] = {"recordings": None, "searched": False}
            _log_activity(f"mediaFileFind factory.create -> {object_id}")
            return web.Response(text=f"result={object_id}\n")

        elif action == "findFile":
            object_id = request.query.get("object", "")
            if object_id not in _find_sessions:
                return web.Response(text="Error\n")

            start_str = request.query.get("condition.StartTime", "")
            end_str = request.query.get("condition.EndTime", "")
            _log_activity(f"mediaFileFind findFile {start_str} - {end_str}")

            results = []
            if start_str and end_str:
                try:
                    req_start = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S")
                    req_end = datetime.strptime(end_str, "%Y-%m-%d %H:%M:%S")
                    results = storage.search(req_start, req_end)
                except ValueError:
                    pass

            _find_sessions[object_id]["recordings"] = results
            _find_sessions[object_id]["searched"] = True
            return web.Response(text="OK\n")

        elif action == "findNextFile":
            object_id = request.query.get("object", "")
            session = _find_sessions.get(object_id)
            if not session or not session["searched"]:
                return web.Response(text="found=0\n")

            recordings = session["recordings"] or []
            lines = [f"found={len(recordings)}"]
            for i, rec in enumerate(recordings):
                st = datetime.fromisoformat(rec["start_time"])
                et = datetime.fromisoformat(rec["end_time"])
                start_fmt = st.strftime("%Y-%m-%d %H:%M:%S")
                end_fmt = et.strftime("%Y-%m-%d %H:%M:%S")
                lines.append(f"items[{i}].Channel=1")
                lines.append(f"items[{i}].FilePath={rec['camera_path']}")
                lines.append(f"items[{i}].StartTime={start_fmt}")
                lines.append(f"items[{i}].EndTime={end_fmt}")
                lines.append(f"items[{i}].Length={rec['size']}")
                lines.append(f"items[{i}].Type=dav")
                lines.append(f"items[{i}].VideoStream=Main")
                lines.append(f"items[{i}].UTCOffset=-14400")

            _log_activity(f"mediaFileFind findNextFile -> {len(recordings)} files")
            return web.Response(text="\n".join(lines) + "\n")

        elif action == "destroy":
            object_id = request.query.get("object", "")
            _find_sessions.pop(object_id, None)
            return web.Response(text="OK\n")

        return web.Response(status=400, text="Unknown action\n")

    async def handle_rpc_loadfile(request):
        path = request.path.replace("/cgi-bin/RPC_Loadfile", "")
        _log_activity(f"RPC_Loadfile {path}")

        # Find recording by camera_path
        file_path = storage.get_file(path)
        if not file_path:
            # Try by basename
            basename = os.path.basename(path)
            for rec in storage.recordings:
                if os.path.basename(rec["camera_path"]) == basename:
                    file_path = storage.get_file(rec["camera_path"])
                    break

        if not file_path or not os.path.exists(file_path):
            return web.Response(status=404, text="File not found\n")

        file_size = os.path.getsize(file_path)

        if request.method == "HEAD":
            return web.Response(
                headers={
                    "Content-Length": str(file_size),
                    "Content-Type": "application/octet-stream",
                },
            )

        response = web.StreamResponse(
            headers={
                "Content-Length": str(file_size),
                "Content-Type": "application/octet-stream",
            },
        )
        await response.prepare(request)

        with open(file_path, "rb") as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                await response.write(chunk)

        await response.write_eof()
        return response

    async def handle_config_manager(request):
        global _record_mode
        action = request.query.get("action", "")

        if action == "setConfig":
            _log_activity("configManager setConfig")
            # Parse RecordMode if present
            name = request.query.get("name", "")
            if "RecordMode" in name:
                mode = request.query.get("table.RecordMode[0].Mode", "0")
                _record_mode = int(mode)
            return web.Response(text="OK\n")
        elif action == "getConfig":
            name = request.query.get("name", "")
            if name == "RecordMode":
                _log_activity("configManager getConfig RecordMode")
                return web.Response(text=f"table.RecordMode[0].Mode={_record_mode}\n")
            return web.Response(text="OK\n")
        return web.Response(status=400, text="Unknown action\n")

    # Register routes with auth
    app.router.add_route(
        "*", "/cgi-bin/recordManager.cgi", require_auth(handle_get_caps)
    )
    app.router.add_route("*", "/cgi-bin/magicBox.cgi", require_auth(handle_system_info))
    app.router.add_route(
        "*", "/cgi-bin/mediaFileFind.cgi", require_auth(handle_media_file_find)
    )
    app.router.add_route(
        "*", "/cgi-bin/configManager.cgi", require_auth(handle_config_manager)
    )
    app.router.add_route(
        "*", "/cgi-bin/RPC_Loadfile{path:.*}", require_auth(handle_rpc_loadfile)
    )
