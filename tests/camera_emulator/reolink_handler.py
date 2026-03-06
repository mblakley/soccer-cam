"""ReoLink camera HTTP API emulator handlers.

Implements the ReoLink JSON API with token auth,
matching the endpoints used by video_grouper/cameras/reolink.py.
"""

import os
import secrets
import time
import logging

from aiohttp import web

logger = logging.getLogger(__name__)

# Token store: token_name -> expiry_timestamp
_tokens: dict[str, float] = {}
LEASE_TIME = 3600


def _make_token():
    token = secrets.token_hex(16)
    _tokens[token] = time.time() + LEASE_TIME
    return token


def _validate_token(request: web.Request) -> bool:
    token = request.query.get("token", "")
    if token == "null":
        return False
    expiry = _tokens.get(token)
    if expiry is None or time.time() > expiry:
        return False
    return True


def _error_json(cmd, detail="invalid token"):
    return web.json_response([{"cmd": cmd, "code": 1, "error": {"detail": detail}}])


def setup_routes(
    app: web.Application, test_files, username: str, password: str, channel: int = 0
):
    """Register all ReoLink endpoint routes."""

    async def handle_api(request: web.Request):
        cmd = request.query.get("cmd", "")

        # Login doesn't require a valid token
        if cmd == "Login":
            return await _handle_login(request, username, password)

        # All other commands require a valid token
        if not _validate_token(request):
            return _error_json(cmd, "please login first")

        if cmd == "GetDevInfo":
            return await _handle_get_dev_info(request)
        elif cmd == "Search":
            return await _handle_search(request, test_files)
        elif cmd == "GetRec":
            return await _handle_get_rec(request, channel)
        elif cmd == "SetRec":
            return await _handle_set_rec(request)
        elif cmd == "Download":
            return await _handle_download(request, test_files)
        else:
            return _error_json(cmd, f"unknown command: {cmd}")

    app.router.add_route("*", "/cgi-bin/api.cgi", handle_api)


async def _handle_login(request: web.Request, username: str, password: str):
    try:
        body = await request.json()
    except Exception:
        return _error_json("Login", "invalid JSON")

    if not body or not isinstance(body, list):
        return _error_json("Login", "expected JSON array")

    param = body[0].get("param", {})
    user = param.get("User", {})

    if user.get("userName") != username or user.get("password") != password:
        return _error_json("Login", "invalid credentials")

    token = _make_token()
    return web.json_response(
        [
            {
                "cmd": "Login",
                "code": 0,
                "value": {"Token": {"name": token, "leaseTime": LEASE_TIME}},
            }
        ]
    )


async def _handle_get_dev_info(request: web.Request):
    return web.json_response(
        [
            {
                "cmd": "GetDevInfo",
                "code": 0,
                "value": {
                    "DevInfo": {
                        "name": "CameraEmulator",
                        "type": "IPC",
                        "firmVer": "v3.1.0.0",
                        "serial": "EMU123456789",
                        "mac": "AA:BB:CC:DD:EE:FF",
                        "model": "RLC-810A",
                    }
                },
            }
        ]
    )


async def _handle_search(request: web.Request, test_files):
    # Build ReoLink-format file list
    reolink_files = []
    for f in test_files:
        st = f["start_time"]
        et = f["end_time"]
        reolink_files.append(
            {
                "name": f["filename"],
                "StartTime": {
                    "year": st.year,
                    "mon": st.month,
                    "day": st.day,
                    "hour": st.hour,
                    "min": st.minute,
                    "sec": st.second,
                },
                "EndTime": {
                    "year": et.year,
                    "mon": et.month,
                    "day": et.day,
                    "hour": et.hour,
                    "min": et.minute,
                    "sec": et.second,
                },
                "size": f["size"],
            }
        )

    return web.json_response(
        [
            {
                "cmd": "Search",
                "code": 0,
                "value": {
                    "SearchResult": {
                        "Status": 0,
                        "File": reolink_files,
                    }
                },
            }
        ]
    )


async def _handle_get_rec(request: web.Request, channel: int):
    return web.json_response(
        [
            {
                "cmd": "GetRec",
                "code": 0,
                "value": {"Rec": {"channel": channel, "schedule": {"enable": 0}}},
            }
        ]
    )


async def _handle_set_rec(request: web.Request):
    return web.json_response([{"cmd": "SetRec", "code": 0, "value": {"rspCode": 200}}])


async def _handle_download(request: web.Request, test_files):
    source = request.query.get("source", "")
    filename = os.path.basename(source)

    clip_path = None
    for f in test_files:
        if f["filename"] == filename:
            clip_path = f["clip_path"]
            break

    if not clip_path or not os.path.exists(clip_path):
        return web.Response(status=404, text="File not found")

    file_size = os.path.getsize(clip_path)

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

    with open(clip_path, "rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            await response.write(chunk)

    await response.write_eof()
    return response
