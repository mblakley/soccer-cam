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

    # ------------------------------------------------------------------
    # Schedule & game management
    # ------------------------------------------------------------------

    def get_schedule(
        self,
        team_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Get team schedule within a date range.

        GET {api_base_url}/api/device-link/schedule
        """
        url = f"{self.api_base_url}/api/device-link/schedule"
        params: dict[str, str] = {"team_id": team_id}
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        logger.debug("Fetching schedule for team %s", team_id)
        return self._request("GET", url, params=params)

    def get_roster(self, team_id: str) -> list[dict[str, Any]]:
        """Get team roster.

        GET {api_base_url}/api/device-link/roster
        """
        url = f"{self.api_base_url}/api/device-link/roster"
        params = {"team_id": team_id}
        logger.debug("Fetching roster for team %s", team_id)
        return self._request("GET", url, params=params)

    def auto_match_video(
        self, team_id: str, video_url: str, recorded_at: str
    ) -> dict[str, Any]:
        """Auto-match a video to a game based on recording time.

        POST {api_base_url}/api/device-link/auto-match-video
        """
        url = f"{self.api_base_url}/api/device-link/auto-match-video"
        body = {"team_id": team_id, "video_url": video_url, "recorded_at": recorded_at}
        logger.debug("Auto-matching video for team %s at %s", team_id, recorded_at)
        return self._request("POST", url, json=body)

    # ------------------------------------------------------------------
    # Game sessions
    # ------------------------------------------------------------------

    def get_game_sessions(
        self,
        team_id: str,
        recording_group_dir: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Get game sessions, optionally filtered by recording group dir.

        GET {api_base_url}/api/game-sessions
        """
        url = f"{self.api_base_url}/api/game-sessions"
        params: dict[str, str] = {"team_id": team_id}
        if recording_group_dir:
            params["recording_group_dir"] = recording_group_dir
        logger.debug("Fetching game sessions for team %s", team_id)
        return self._request("GET", url, params=params)

    def create_game_session(
        self,
        team_id: str,
        recording_group_dir: str,
        game_date: str,
        opponent_name: str,
        video_youtube_id: Optional[str] = None,
        status: str = "recording_complete",
    ) -> dict[str, Any]:
        """Create a new game session.

        POST {api_base_url}/api/game-sessions
        """
        url = f"{self.api_base_url}/api/game-sessions"
        body: dict[str, Any] = {
            "team_id": team_id,
            "recording_group_dir": recording_group_dir,
            "game_date": game_date,
            "opponent_name": opponent_name,
            "status": status,
        }
        if video_youtube_id is not None:
            body["video_youtube_id"] = video_youtube_id
        logger.debug("Creating game session for team %s vs %s", team_id, opponent_name)
        return self._request("POST", url, json=body)

    def update_game_session(self, session_id: str, **fields: Any) -> dict[str, Any]:
        """Update a game session.

        PATCH {api_base_url}/api/game-sessions/{session_id}
        """
        url = f"{self.api_base_url}/api/game-sessions/{session_id}"
        logger.debug("Updating game session %s", session_id)
        return self._request("PATCH", url, json=fields)

    # ------------------------------------------------------------------
    # Time sync
    # ------------------------------------------------------------------

    def get_sync_anchors(self, game_session_id: str) -> list[dict[str, Any]]:
        """Get sync anchors for a game session.

        GET {api_base_url}/api/sync-anchors
        """
        url = f"{self.api_base_url}/api/sync-anchors"
        params = {"game_session_id": game_session_id}
        logger.debug("Fetching sync anchors for session %s", game_session_id)
        return self._request("GET", url, params=params)

    def update_sync_anchor(self, anchor_id: str, **fields: Any) -> dict[str, Any]:
        """Update sync anchor detection results.

        PATCH {api_base_url}/api/sync-anchors/{anchor_id}
        Fields: detected_at_video_time, detected_in_file,
                detection_confidence, computed_offset_ms, status
        """
        url = f"{self.api_base_url}/api/sync-anchors/{anchor_id}"
        logger.debug("Updating sync anchor %s", anchor_id)
        return self._request("PATCH", url, json=fields)

    def reconcile_game_session(self, session_id: str) -> dict[str, Any]:
        """Trigger time sync reconciliation for a game session.

        POST {api_base_url}/api/game-sessions/{session_id}/reconcile
        Returns sync_status, true_recording_start, and tags_updated.
        """
        url = f"{self.api_base_url}/api/game-sessions/{session_id}/reconcile"
        logger.debug("Reconciling game session %s", session_id)
        return self._request("POST", url)

    # ------------------------------------------------------------------
    # Moment tags & clips
    # ------------------------------------------------------------------

    def get_pending_moment_tags(self, game_session_id: str) -> list[dict[str, Any]]:
        """Get moment tags that need offset calculation.

        GET {api_base_url}/api/moment-tags?pending_offset=true
        """
        url = f"{self.api_base_url}/api/moment-tags"
        params = {"game_session_id": game_session_id, "pending_offset": "true"}
        logger.debug("Fetching pending moment tags for session %s", game_session_id)
        return self._request("GET", url, params=params)

    def update_moment_tag(self, tag_id: str, **fields: Any) -> dict[str, Any]:
        """Update moment tag offsets.

        PATCH {api_base_url}/api/moment-tags/{tag_id}
        Fields: video_offset_seconds, trimmed_offset_seconds
        """
        url = f"{self.api_base_url}/api/moment-tags/{tag_id}"
        logger.debug("Updating moment tag %s", tag_id)
        return self._request("PATCH", url, json=fields)

    def create_moment_clip(
        self,
        moment_tag_id: str,
        game_session_id: str,
        clip_start_offset: float,
        clip_end_offset: float,
        clip_duration: float = 30.0,
    ) -> dict[str, Any]:
        """Create a moment clip record.

        POST {api_base_url}/api/moment-clips
        """
        url = f"{self.api_base_url}/api/moment-clips"
        body = {
            "moment_tag_id": moment_tag_id,
            "game_session_id": game_session_id,
            "clip_start_offset": clip_start_offset,
            "clip_end_offset": clip_end_offset,
            "clip_duration": clip_duration,
        }
        logger.debug("Creating moment clip for tag %s", moment_tag_id)
        return self._request("POST", url, json=body)

    def update_moment_clip(self, clip_id: str, **fields: Any) -> dict[str, Any]:
        """Update moment clip status or file path.

        PATCH {api_base_url}/api/moment-clips/{clip_id}
        Fields: status, file_path, youtube_video_id
        """
        url = f"{self.api_base_url}/api/moment-clips/{clip_id}"
        logger.debug("Updating moment clip %s", clip_id)
        return self._request("PATCH", url, json=fields)

    # ------------------------------------------------------------------
    # Plugins & capabilities
    # ------------------------------------------------------------------

    def get_capabilities(self) -> dict[str, Any]:
        """Get feature flags and entitlements for the current user.

        GET {api_base_url}/api/users/me/capabilities
        """
        url = f"{self.api_base_url}/api/users/me/capabilities"
        logger.debug("Fetching capabilities from %s", url)
        return self._request("GET", url)

    def get_available_plugins(self) -> list[dict[str, Any]]:
        """Get list of plugins the current user is entitled to.

        GET {api_base_url}/api/plugins
        """
        url = f"{self.api_base_url}/api/plugins"
        logger.debug("Fetching available plugins from %s", url)
        return self._request("GET", url)

    # ------------------------------------------------------------------
    # Camera status & config
    # ------------------------------------------------------------------

    def update_camera_status(
        self,
        camera_id: str,
        status: str,
        firmware_version: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Report camera status to TTT.

        PATCH {api_base_url}/api/device-link/camera-status
        """
        url = f"{self.api_base_url}/api/device-link/camera-status"
        data: dict[str, Any] = {"camera_id": camera_id, "status": status}
        if firmware_version:
            data["firmware_version"] = firmware_version
        if error_message:
            data["error_message"] = error_message
        logger.debug("Updating camera status for %s: %s", camera_id, status)
        return self._request("PATCH", url, json=data)

    def get_camera_config(self, camera_id: str) -> Optional[dict[str, Any]]:
        """Fetch camera config from TTT.

        GET {api_base_url}/api/device-link/camera-config
        """
        url = f"{self.api_base_url}/api/device-link/camera-config"
        logger.debug("Fetching camera config for %s", camera_id)
        return self._request("GET", url, params={"camera_id": camera_id})

    def push_camera_config(
        self, camera_id: str, config: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        """Push local config to TTT for backup/transfer.

        PUT {api_base_url}/api/cameras/{camera_id}
        """
        url = f"{self.api_base_url}/api/cameras/{camera_id}"
        logger.debug("Pushing camera config for %s", camera_id)
        return self._request("PUT", url, json=config)

    # ------------------------------------------------------------------
    # Recording pipeline reporting
    # ------------------------------------------------------------------

    def register_recordings(
        self, camera_id: str, team_id: str, files: list[dict]
    ) -> list[dict] | None:
        """Register newly discovered recording files with TTT.

        POST {api_base_url}/api/device-link/recordings?camera_id={camera_id}&team_id={team_id}

        files is a list of dicts with optional keys:
        file_name, file_group, file_size_bytes, duration_seconds,
        recording_start, recording_end
        """
        url = f"{self.api_base_url}/api/device-link/recordings"
        params = {"camera_id": camera_id, "team_id": team_id}
        logger.debug("Registering %d recording(s) for camera %s", len(files), camera_id)
        return self._request("POST", url, params=params, json=files)

    def update_recording_status(
        self,
        recording_id: str,
        stage: str,
        status: str,
        error_message: Optional[str] = None,
        youtube_url: Optional[str] = None,
        youtube_video_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Update pipeline stage status for a recording.

        PATCH {api_base_url}/api/device-link/recordings/{recording_id}/status
        """
        url = f"{self.api_base_url}/api/device-link/recordings/{recording_id}/status"
        body: dict[str, Any] = {"stage": stage, "status": status}
        if error_message is not None:
            body["error_message"] = error_message
        if youtube_url is not None:
            body["youtube_url"] = youtube_url
        if youtube_video_id is not None:
            body["youtube_video_id"] = youtube_video_id
        logger.debug("Updating recording %s: %s=%s", recording_id, stage, status)
        return self._request("PATCH", url, json=body)

    def get_high_water_mark(self, camera_id: str) -> Optional[str]:
        """Get the latest recording timestamp TTT knows about for this camera.

        GET {api_base_url}/api/device-link/high-water-mark?camera_id={camera_id}
        Returns ISO datetime string or None.
        """
        url = f"{self.api_base_url}/api/device-link/high-water-mark"
        params = {"camera_id": camera_id}
        logger.debug("Fetching high-water mark for camera %s", camera_id)
        result = self._request("GET", url, params=params)
        if isinstance(result, dict):
            return result.get("high_water_mark")
        return result

    def download_plugin(self, key: str, dest_path: Path) -> None:
        """Download a plugin zip and save it to dest_path.

        GET {api_base_url}/api/plugins/{key}/download
        """
        url = f"{self.api_base_url}/api/plugins/{key}/download"
        logger.debug("Downloading plugin %s from %s", key, url)
        self._ensure_auth()
        resp = self._http.get(url, headers=self._auth_headers())
        if resp.status_code == 403:
            raise TTTApiError(
                f"Not entitled to plugin '{key}'",
                status_code=403,
                response_body=resp.text,
            )
        if resp.status_code >= 400:
            raise TTTApiError(
                f"Plugin download failed (HTTP {resp.status_code})",
                status_code=resp.status_code,
                response_body=resp.text,
            )
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(resp.content)
