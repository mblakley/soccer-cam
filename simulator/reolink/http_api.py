"""Reolink HTTP JSON API simulator.

Implements the Reolink camera HTTP API on /cgi-bin/api.cgi,
matching the endpoints used by video_grouper/cameras/reolink.py.
Token auth with 3600s lease, device info, search, recording control.
HTTP Download intentionally returns 404 (matching real firmware bug).
"""

import logging
import secrets
import time
from datetime import datetime

from aiohttp import web

logger = logging.getLogger(__name__)

LEASE_TIME = 3600

# Recording schedule state: 168-char binary strings (7 days x 24 hours)
_schedule_state = {
    "TIMING": "1" * 168,
    "MD": "0" * 168,
}


class TokenStore:
    def __init__(self):
        self._tokens: dict[str, float] = {}

    def create(self) -> str:
        token = secrets.token_hex(16)
        self._tokens[token] = time.time() + LEASE_TIME
        return token

    def validate(self, token: str) -> bool:
        if token == "null":
            return False
        expiry = self._tokens.get(token)
        return expiry is not None and time.time() < expiry


_token_store = TokenStore()


def _error_json(cmd, detail="invalid token"):
    return web.json_response([{"cmd": cmd, "code": 1, "error": {"detail": detail}}])


def _reolink_time_to_datetime(t: dict) -> datetime:
    return datetime(t["year"], t["mon"], t["day"], t["hour"], t["min"], t["sec"])


def setup_routes(
    app: web.Application, storage, username: str, password: str, device_name: str
):
    """Register Reolink HTTP API routes."""
    channel = 0
    activity_log = app.get("activity_log")

    def _log_activity(msg):
        if activity_log is not None:
            activity_log.append(f"[HTTP] {msg}")

    async def handle_api(request: web.Request):
        cmd = request.query.get("cmd", "")
        _log_activity(f"{request.method} cmd={cmd}")

        if cmd == "Login":
            return await _handle_login(request, username, password)

        if not _token_store.validate(request.query.get("token", "")):
            return _error_json(cmd, "please login first")

        handlers = {
            "GetDevInfo": _handle_get_dev_info,
            "Search": _handle_search,
            "GetRecV20": _handle_get_rec_v20,
            "SetRecV20": _handle_set_rec_v20,
            "GetRec": _handle_get_rec,
            "SetRec": _handle_set_rec,
            "Download": _handle_download,
        }

        handler = handlers.get(cmd)
        if handler:
            return await handler(request)
        return _error_json(cmd, f"unknown command: {cmd}")

    async def _handle_login(request, uname, passwd):
        try:
            body = await request.json()
        except Exception:
            return _error_json("Login", "invalid JSON")

        if not body or not isinstance(body, list):
            return _error_json("Login", "expected JSON array")

        param = body[0].get("param", {})
        user = param.get("User", {})

        if user.get("userName") != uname or user.get("password") != passwd:
            _log_activity("Login FAILED (bad credentials)")
            return _error_json("Login", "invalid credentials")

        token = _token_store.create()
        _log_activity(f"Login OK, token={token[:8]}...")
        return web.json_response(
            [
                {
                    "cmd": "Login",
                    "code": 0,
                    "value": {"Token": {"name": token, "leaseTime": LEASE_TIME}},
                }
            ]
        )

    async def _handle_get_dev_info(request):
        return web.json_response(
            [
                {
                    "cmd": "GetDevInfo",
                    "code": 0,
                    "value": {
                        "DevInfo": {
                            "name": device_name,
                            "type": "IPC",
                            "firmVer": "v3.0.0.4867_25010804",
                            "serial": "SIM00000000REOLINK",
                            "mac": "ec:71:db:44:56:12",
                            "model": "Reolink Duo 3 PoE",
                            "resolution": {"width": 7680, "height": 2160},
                        }
                    },
                }
            ]
        )

    async def _handle_search(request):
        try:
            body = await request.json()
        except Exception:
            return _error_json("Search", "invalid JSON")

        search_param = body[0].get("param", {}).get("Search", {})
        req_start = _reolink_time_to_datetime(search_param["StartTime"])
        req_end = _reolink_time_to_datetime(search_param["EndTime"])

        results = storage.search(req_start, req_end)
        _log_activity(f"Search {req_start} - {req_end}: {len(results)} files")

        reolink_files = []
        for rec in results:
            st = datetime.fromisoformat(rec["start_time"])
            et = datetime.fromisoformat(rec["end_time"])
            reolink_files.append(
                {
                    "name": rec["camera_path"],
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
                    "size": rec["size"],
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

    async def _handle_get_rec_v20(request):
        return web.json_response(
            [
                {
                    "cmd": "GetRecV20",
                    "code": 0,
                    "value": {
                        "Rec": {
                            "channel": channel,
                            "schedule": {
                                "channel": channel,
                                "table": {
                                    "TIMING": _schedule_state["TIMING"],
                                    "MD": _schedule_state["MD"],
                                },
                            },
                        }
                    },
                }
            ]
        )

    async def _handle_set_rec_v20(request):
        try:
            body = await request.json()
            table = body[0]["param"]["Rec"]["schedule"]["table"]
            if "TIMING" in table:
                _schedule_state["TIMING"] = table["TIMING"]
            if "MD" in table:
                _schedule_state["MD"] = table["MD"]
            _log_activity(
                f"SetRecV20: TIMING={'1' in _schedule_state['TIMING']}, "
                f"MD={'1' in _schedule_state['MD']}"
            )
        except Exception as e:
            logger.warning(f"SetRecV20 parse error: {e}")

        return web.json_response(
            [{"cmd": "SetRecV20", "code": 0, "value": {"rspCode": 200}}]
        )

    async def _handle_get_rec(request):
        return web.json_response(
            [
                {
                    "cmd": "GetRec",
                    "code": 0,
                    "value": {"Rec": {"channel": channel, "schedule": {"enable": 1}}},
                }
            ]
        )

    async def _handle_set_rec(request):
        return web.json_response(
            [{"cmd": "SetRec", "code": 0, "value": {"rspCode": 200}}]
        )

    async def _handle_download(request):
        # HTTP Download is intentionally broken on Reolink Duo 3 PoE firmware.
        # This forces the client to use Baichuan protocol download (port 9000).
        _log_activity("Download 404 (firmware bug simulation)")
        return web.Response(status=404, text="Not Found")

    app.router.add_route("*", "/cgi-bin/api.cgi", handle_api)
