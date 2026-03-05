"""
TTT (Team Tech Tools) API client.

Authenticates with TTT's Supabase backend using email/password,
then calls TTT API endpoints to discover team assignments and manage clip requests.
"""

import base64
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


class TTTApiError(Exception):
    """Raised when a TTT API call fails."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response_body: Optional[str] = None,
    ):
        self.status_code = status_code
        self.response_body = response_body
        super().__init__(message)


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    """Decode a JWT payload without signature verification.

    Only used to read the `exp` claim for token expiry checks.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Invalid JWT: expected 3 parts")
    # Add padding for base64url decoding
    payload_b64 = parts[1]
    padding = 4 - len(payload_b64) % 4
    if padding != 4:
        payload_b64 += "=" * padding
    payload_bytes = base64.urlsafe_b64decode(payload_b64)
    return json.loads(payload_bytes)


class TTTApiClient:
    """Client for the Team Tech Tools API.

    Authenticates via Supabase email/password, stores tokens on disk,
    and auto-refreshes expired tokens before API calls.
    """

    def __init__(
        self,
        supabase_url: str,
        anon_key: str,
        api_base_url: str,
        storage_path: str,
    ) -> None:
        self.supabase_url = supabase_url.rstrip("/")
        self.anon_key = anon_key
        self.api_base_url = api_base_url.rstrip("/")
        self.storage_path = Path(storage_path)

        self._token_dir = self.storage_path / "ttt"
        self._token_file = self._token_dir / "tokens.json"

        self._access_token: Optional[str] = None
        self._refresh_token_value: Optional[str] = None
        self._expires_at: Optional[float] = None

        self._http = httpx.Client(timeout=30.0)

        self._load_tokens()

    # ------------------------------------------------------------------
    # Token persistence
    # ------------------------------------------------------------------

    def _load_tokens(self) -> None:
        """Load stored tokens from disk, if available."""
        if not self._token_file.exists():
            logger.debug("No stored TTT tokens found at %s", self._token_file)
            return
        try:
            data = json.loads(self._token_file.read_text(encoding="utf-8"))
            self._access_token = data.get("access_token")
            self._refresh_token_value = data.get("refresh_token")
            self._expires_at = data.get("expires_at")
            logger.debug("Loaded TTT tokens from %s", self._token_file)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load TTT tokens: %s", exc)

    def _save_tokens(self) -> None:
        """Persist current tokens to disk."""
        self._token_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "access_token": self._access_token,
            "refresh_token": self._refresh_token_value,
            "expires_at": self._expires_at,
        }
        self._token_file.write_text(json.dumps(data), encoding="utf-8")
        logger.debug("Saved TTT tokens to %s", self._token_file)

    def _store_auth_response(self, data: dict[str, Any]) -> None:
        """Extract tokens from a Supabase auth response and persist them."""
        self._access_token = data["access_token"]
        self._refresh_token_value = data["refresh_token"]
        try:
            payload = _decode_jwt_payload(self._access_token)
            self._expires_at = float(payload["exp"])
        except (ValueError, KeyError) as exc:
            logger.warning("Could not decode JWT exp, using fallback expiry: %s", exc)
            self._expires_at = time.time() + data.get("expires_in", 3600)
        self._save_tokens()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def login(self, email: str, password: str) -> None:
        """Authenticate with Supabase using email and password."""
        url = f"{self.supabase_url}/auth/v1/token?grant_type=password"
        headers = {
            "apikey": self.anon_key,
            "Content-Type": "application/json",
        }
        body = {"email": email, "password": password}

        logger.info("Logging in to TTT as %s", email)
        resp = self._http.post(url, headers=headers, json=body)
        if resp.status_code != 200:
            raise TTTApiError(
                f"Login failed (HTTP {resp.status_code}): {resp.text}",
                status_code=resp.status_code,
                response_body=resp.text,
            )

        self._store_auth_response(resp.json())
        logger.info("TTT login successful")

    def refresh_token(self) -> None:
        """Refresh the access token using the stored refresh token."""
        if not self._refresh_token_value:
            raise TTTApiError("No refresh token available; login first")

        url = f"{self.supabase_url}/auth/v1/token?grant_type=refresh_token"
        headers = {
            "apikey": self.anon_key,
            "Content-Type": "application/json",
        }
        body = {"refresh_token": self._refresh_token_value}

        logger.debug("Refreshing TTT access token")
        resp = self._http.post(url, headers=headers, json=body)
        if resp.status_code != 200:
            raise TTTApiError(
                f"Token refresh failed (HTTP {resp.status_code}): {resp.text}",
                status_code=resp.status_code,
                response_body=resp.text,
            )

        self._store_auth_response(resp.json())
        logger.debug("TTT token refreshed successfully")

    def is_authenticated(self) -> bool:
        """Return True if we have tokens that are not yet expired."""
        if not self._access_token or not self._expires_at:
            return False
        return time.time() < self._expires_at

    def _ensure_auth(self) -> None:
        """Ensure we have a valid access token, refreshing if necessary."""
        if not self._access_token:
            raise TTTApiError("Not authenticated; call login() first")
        if self._expires_at and (time.time() > self._expires_at - 60):
            logger.debug("Access token expiring soon, refreshing")
            self.refresh_token()

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        """Return headers with the current Bearer token."""
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        """Make an authenticated API request, returning parsed JSON."""
        self._ensure_auth()
        resp = self._http.request(method, url, headers=self._auth_headers(), **kwargs)
        if resp.status_code >= 400:
            raise TTTApiError(
                f"TTT API error {method} {url} (HTTP {resp.status_code}): {resp.text}",
                status_code=resp.status_code,
                response_body=resp.text,
            )
        if resp.status_code == 204:
            return None
        return resp.json()

    # ------------------------------------------------------------------
    # Public API methods
    # ------------------------------------------------------------------

    def get_team_assignments(self) -> Any:
        """Get team assignments for the current device/user.

        GET {api_base_url}/api/device-link/me
        """
        url = f"{self.api_base_url}/api/device-link/me"
        logger.debug("Fetching team assignments from %s", url)
        return self._request("GET", url)

    def get_pending_clip_requests(self) -> Any:
        """Get pending clip requests for linked teams.

        GET {api_base_url}/api/device-link/clip-requests
        """
        url = f"{self.api_base_url}/api/device-link/clip-requests"
        logger.debug("Fetching pending clip requests from %s", url)
        return self._request("GET", url)

    def start_clip_request(self, request_id: str) -> Any:
        """Mark a clip request as started.

        PATCH {api_base_url}/api/clip-requests/{request_id}/start
        """
        url = f"{self.api_base_url}/api/clip-requests/{request_id}/start"
        logger.debug("Starting clip request %s", request_id)
        return self._request("PATCH", url)

    def fulfill_clip_request(
        self,
        request_id: str,
        url: str,
        notes: Optional[str] = None,
    ) -> Any:
        """Mark a clip request as fulfilled with the result URL.

        PATCH {api_base_url}/api/clip-requests/{request_id}/fulfill
        """
        endpoint = f"{self.api_base_url}/api/clip-requests/{request_id}/fulfill"
        body: dict[str, Any] = {"fulfilled_url": url}
        if notes is not None:
            body["fulfilled_notes"] = notes
        logger.debug("Fulfilling clip request %s", request_id)
        return self._request("PATCH", endpoint, json=body)
