"""Dahua camera HTTP API emulator handlers.

Implements the Dahua proprietary HTTP API with Digest Auth,
matching the endpoints used by video_grouper/cameras/dahua.py.
"""

import hashlib
import os
import random
import string
import logging

from aiohttp import web

logger = logging.getLogger(__name__)

# MediaFileFind state: track active search sessions
_find_sessions = {}
_find_counter = 1000000


def _random_nonce():
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=32))


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


class DigestAuthMiddleware:
    """HTTP Digest Auth middleware for Dahua emulation."""

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
                "WWW-Authenticate": (
                    f'Digest realm="{self.realm}", nonce="{nonce}", qop="auth"'
                )
            },
        )

    def validate(self, request: web.Request) -> bool:
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Digest "):
            return False

        # Parse digest fields
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

        # Compute expected digest
        ha1 = _md5(f"{self.username}:{self.realm}:{self.password}")
        ha2 = _md5(f"{request.method}:{uri}")
        expected = _md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")

        return response_hash == expected


def setup_routes(app: web.Application, test_files, username: str, password: str):
    """Register all Dahua endpoint routes."""
    auth = DigestAuthMiddleware(username, password)

    def require_auth(handler):
        async def wrapper(request):
            if not auth.validate(request):
                return auth.challenge_response()
            return await handler(request)

        return wrapper

    async def handle_get_caps(request):
        return web.Response(text="table.MediaFileFind.MaxCount=100\n")

    async def handle_system_info(request):
        body = (
            "deviceName=CameraEmulator\n"
            "deviceType=IPC\n"
            "firmwareVersion=2.800.0000.0\n"
            "serialNumber=EMU123456789\n"
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
            _find_sessions[object_id] = {"files": test_files, "searched": False}
            return web.Response(text=f"result={object_id}\n")

        elif action == "findFile":
            object_id = request.query.get("object", "")
            if object_id in _find_sessions:
                _find_sessions[object_id]["searched"] = True
                # Filter files by the requested time range (like a real camera)
                start_str = request.query.get("condition.StartTime", "")
                end_str = request.query.get("condition.EndTime", "")
                if start_str and end_str:
                    from datetime import datetime

                    try:
                        req_start = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S")
                        req_end = datetime.strptime(end_str, "%Y-%m-%d %H:%M:%S")
                        _find_sessions[object_id]["files"] = [
                            f
                            for f in _find_sessions[object_id]["files"]
                            if f["start_time"].replace(tzinfo=None) > req_start
                            and f["end_time"].replace(tzinfo=None) <= req_end
                        ]
                    except ValueError:
                        pass
            return web.Response(text="OK\n")

        elif action == "findNextFile":
            object_id = request.query.get("object", "")
            session = _find_sessions.get(object_id)
            if not session or not session["searched"]:
                return web.Response(text="found=0\n")

            # Consume files: return them once, then empty on subsequent calls
            files = session["files"]
            session["files"] = []

            if not files:
                return web.Response(text="found=0\n")

            lines = [f"found={len(files)}"]
            for i, f in enumerate(files):
                start_str = f["start_time"].strftime("%Y-%m-%d %H:%M:%S")
                end_str = f["end_time"].strftime("%Y-%m-%d %H:%M:%S")
                lines.append(f"items[{i}].Channel=1")
                lines.append(f"items[{i}].FilePath=/mnt/dvr/{f['filename']}")
                lines.append(f"items[{i}].StartTime={start_str}")
                lines.append(f"items[{i}].EndTime={end_str}")
                lines.append(f"items[{i}].Size={f['size']}")
                lines.append(f"items[{i}].Type=dav")
                lines.append(f"items[{i}].VideoStream=Main")
                lines.append(f"items[{i}].UTCOffset=-14400")

            return web.Response(text="\n".join(lines) + "\n")

        elif action == "close":
            return web.Response(text="OK\n")

        elif action == "destroy":
            object_id = request.query.get("object", "")
            _find_sessions.pop(object_id, None)
            return web.Response(text="OK\n")

        return web.Response(status=400, text="Unknown action\n")

    async def handle_rpc_loadfile(request):
        # Extract file path from URL (everything after /cgi-bin/RPC_Loadfile)
        path = request.path.replace("/cgi-bin/RPC_Loadfile", "")
        # Strip leading /mnt/dvr/ prefix that Dahua client prepends
        filename = os.path.basename(path)

        # Find the matching test file
        clip_path = None
        for f in test_files:
            if f["filename"] == filename:
                clip_path = f["clip_path"]
                break

        if not clip_path or not os.path.exists(clip_path):
            return web.Response(status=404, text="File not found\n")

        file_size = os.path.getsize(clip_path)

        if request.method == "HEAD":
            return web.Response(
                headers={
                    "Content-Length": str(file_size),
                    "Content-Type": "application/octet-stream",
                },
            )

        # Stream the file
        response = web.StreamResponse(
            headers={
                "Content-Length": str(file_size),
                "Content-Type": "application/octet-stream",
            },
        )
        await response.prepare(request)

        with open(clip_path, "rb") as fh:
            while True:
                chunk = fh.read(1048576)
                if not chunk:
                    break
                await response.write(chunk)

        await response.write_eof()
        return response

    async def handle_config_manager(request):
        action = request.query.get("action", "")
        if action == "setConfig":
            return web.Response(text="OK\n")
        elif action == "getConfig":
            name = request.query.get("name", "")
            if name == "RecordMode":
                return web.Response(text="table.RecordMode[0].Mode=0\n")
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
    # RPC_Loadfile uses a path suffix, so we need a wildcard route
    app.router.add_route(
        "*", "/cgi-bin/RPC_Loadfile{path:.*}", require_auth(handle_rpc_loadfile)
    )
