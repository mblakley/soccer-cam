"""A real TTTApiClient whose saved access token has expired (as after an hour
signed in, or a restart) but whose refresh token still holds, talking to a TTT
served by ``httpx.MockTransport``. Each TTT port must refresh it and carry on
rather than read the expiry as "signed out". Used through the ``expired_ttt``
fixture (tests/integrations/conftest.py) or directly.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import httpx

from video_grouper.api_integrations.ttt_api import TTTApiClient

SUPABASE = "https://sb.example"
API = "https://ttt.example"


def make_jwt(exp: float, sub: str = "user-1") -> str:
    def part(obj: dict[str, Any]) -> str:
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{part({'alg': 'none'})}.{part({'exp': int(exp), 'sub': sub})}.sig"


class ExpiredTTTSession:
    def __init__(self, storage_path: str, *, refresh_ok: bool = True):
        self.refresh_ok = refresh_ok
        self.requests: list[httpx.Request] = []
        # (method, path) -> JSON body returned with 200; anything else is 404
        self.routes: dict[tuple[str, str], Any] = {}
        self.client = TTTApiClient(SUPABASE, "anon", API, storage_path)
        self.client._http = httpx.Client(transport=httpx.MockTransport(self._handle))
        expired = time.time() - 600
        self.client._access_token = make_jwt(expired)
        self.client._expires_at = expired
        self.client._refresh_token_value = "refresh-1"

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/auth/v1/token":
            if not self.refresh_ok:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(
                200,
                json={
                    "access_token": make_jwt(time.time() + 3600),
                    "refresh_token": "refresh-2",
                    "expires_in": 3600,
                },
            )
        key = (request.method, request.url.path)
        if key in self.routes:
            return httpx.Response(200, json=self.routes[key])
        return httpx.Response(404, json={"detail": "not found"})

    def calls(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == path]

    @property
    def refreshes(self) -> int:
        return len(self.calls("POST", "/auth/v1/token"))

    def bearer(self, request: httpx.Request) -> str:
        return request.headers.get("Authorization", "")
