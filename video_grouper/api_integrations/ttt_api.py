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
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class TTTApiError(Exception):
    """Raised when a TTT API call fails."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response_body: str | None = None,
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

        self._access_token: str | None = None
        self._refresh_token_value: str | None = None
        self._expires_at: float | None = None

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

    def send_magic_link(self, email: str, redirect_to: str) -> None:
        """Ask Supabase to email a magic-link / OTP to the user.

        Mirrors the supabase-js ``signInWithOtp`` flow: the email contains a
        link that, when clicked, lands on ``redirect_to`` with an access
        token in the URL fragment. The headless auth server's ``/callback``
        page extracts that fragment the same way the OAuth flow does.
        """
        url = f"{self.supabase_url}/auth/v1/otp"
        headers = {
            "apikey": self.anon_key,
            "Content-Type": "application/json",
        }
        body = {
            "email": email,
            "create_user": True,
            "data": {},
            "options": {"email_redirect_to": redirect_to},
        }

        logger.info("Requesting magic link for %s", email)
        resp = self._http.post(url, headers=headers, json=body)
        if resp.status_code not in (200, 201):
            raise TTTApiError(
                f"Magic-link request failed (HTTP {resp.status_code}): {resp.text}",
                status_code=resp.status_code,
                response_body=resp.text,
            )

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

    def set_session_from_token(self, access_token: str) -> None:
        """Establish a session from an existing access token (e.g. OAuth).

        Decodes the JWT to extract expiry, stores the token, and persists
        it to disk.  There is no refresh token in this flow -- the caller
        must re-authenticate when the token expires.
        """
        self._access_token = access_token
        self._refresh_token_value = None
        try:
            payload = _decode_jwt_payload(access_token)
            self._expires_at = float(payload["exp"])
        except (ValueError, KeyError) as exc:
            logger.warning(
                "Could not decode JWT exp from OAuth token, using 1-hour fallback: %s",
                exc,
            )
            self._expires_at = time.time() + 3600
        self._save_tokens()
        logger.info("TTT session established from OAuth token")

    def is_authenticated(self) -> bool:
        """Return True if we have tokens that are not yet expired."""
        if not self._access_token or not self._expires_at:
            return False
        return time.time() < self._expires_at

    def current_user_id(self) -> str | None:
        """Return the authenticated user's id (JWT `sub` claim), or None."""
        if not self._access_token:
            return None
        try:
            payload = _decode_jwt_payload(self._access_token)
        except Exception:
            return None
        return payload.get("sub")

    def check_entitlement(self, entitlement_key: str) -> bool:
        """Return True if the current user holds the named entitlement.

        Reads from the capabilities endpoint. Returns False on any error so
        callers fail closed.
        """
        try:
            caps = self.get_capabilities()
        except Exception:
            return False
        if not isinstance(caps, dict):
            return False
        entitlements = caps.get("entitlements") or {}
        return bool(entitlements.get(entitlement_key))

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

    def list_model_versions(
        self,
        model_key: str,
        channel: str | None = None,
        pipeline_version: str | None = None,
    ) -> list[dict[str, Any]]:
        """List model versions the current user is entitled to.

        GET {api_base_url}/api/models/{model_key}/versions
        """
        url = f"{self.api_base_url}/api/models/{model_key}/versions"
        params: dict[str, str] = {}
        if channel:
            params["channel"] = channel
        if pipeline_version:
            params["pipeline_version"] = pipeline_version
        logger.debug("Listing model versions for %s (channel=%s)", model_key, channel)
        return self._request("GET", url, params=params or None)

    def acquire_model_license(
        self,
        model_key: str,
        channel: str | None = None,
        pipeline_version: str | None = None,
    ) -> dict[str, Any]:
        """Request a license for the named model.

        POST {api_base_url}/api/models/{model_key}/license

        Server picks the highest tier the user is entitled to (premium > free).
        Response shape: license_id, license_token (b64 manifest), license_signature
        (hex Ed25519), wrapped_key (hex AES-256), model_key, version, tier, expires_at.
        """
        url = f"{self.api_base_url}/api/models/{model_key}/license"
        body: dict[str, Any] = {}
        if channel:
            body["channel"] = channel
        if pipeline_version:
            body["pipeline_version"] = pipeline_version
        logger.debug("Acquiring model license for %s", model_key)
        return self._request("POST", url, json=body)

    def redeem_support_grant(self, code: str) -> dict[str, Any]:
        """Redeem a support-issued grant code.

        POST {api_base_url}/api/grants/redeem

        No auth header — the code itself is the credential. The grant is
        applied to the user_id locked at issuance time, regardless of who
        calls this. Response shape: grant_id, target_user_id,
        entitlement_key, expires_at.
        """
        url = f"{self.api_base_url}/api/grants/redeem"
        body = {"code": code}
        logger.debug("Redeeming support grant code")
        # Bypass _request() — that helper attaches Bearer auth + auto-refresh,
        # which we don't want here (the redeem endpoint is intentionally public).
        resp = self._http.request(
            "POST",
            url,
            headers={"Content-Type": "application/json"},
            json=body,
        )
        if resp.status_code >= 400:
            raise TTTApiError(
                f"Support grant redemption failed (HTTP {resp.status_code}): {resp.text}",
                status_code=resp.status_code,
                response_body=resp.text,
            )
        return resp.json()

    def get_team_assignments(self) -> Any:
        """Get team assignments for the current device/user.

        GET {api_base_url}/api/device-link/me
        """
        url = f"{self.api_base_url}/api/device-link/me"
        logger.debug("Fetching team assignments from %s", url)
        return self._request("GET", url)

    def register_as_camera_manager(self) -> list[dict[str, Any]]:
        """Auto-claim camera-manager status for every team the user belongs to.

        POST {api_base_url}/api/device-link/register-camera-manager

        Idempotent: the server iterates approved team_members rows for the
        caller, creates the missing camera_managers rows, and returns the
        union of existing + newly-created rows. Each entry has
        ``id``, ``team_id``, ``user_id``, ``email``, ``name``, ``created_at``.

        Returns ``[]`` when the user has zero approved team memberships.
        """
        url = f"{self.api_base_url}/api/device-link/register-camera-manager"
        logger.debug("Registering as camera manager via %s", url)
        result = self._request("POST", url)
        # 204 maps to None upstream; coerce so callers always get a list.
        return result if isinstance(result, list) else []

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
        notes: str | None = None,
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
    # Highlight reels (Phase 2 — soccer-cam renders + uploads + reports back)
    # ------------------------------------------------------------------

    def get_pending_highlights(
        self, camera_id: str | None = None
    ) -> list[dict[str, Any]]:
        """List the current user's highlight reels with status=pending.

        GET {api_base_url}/api/highlights?status=pending[&camera_id=<id>]

        ``camera_id`` is this install's identity from ``config.ttt.camera_id``.
        Sent as a query param so TTT can scope the response to reels this
        install is positioned to fulfill. v1 TTT accepts-and-ignores it.
        """
        url = f"{self.api_base_url}/api/highlights"
        params: dict[str, str] = {"status": "pending"}
        if camera_id:
            params["camera_id"] = camera_id
        logger.debug("Fetching pending highlight reels (camera_id=%s)", camera_id)
        return self._request("GET", url, params=params)

    def get_highlight_game_clips(self, reel_id: str) -> list[dict[str, Any]]:
        """Get the game clips linked to a highlight reel, ordered by sequence.

        GET {api_base_url}/api/highlights/{reel_id}/game-clips

        Response items include ``recording_group_dir`` + ``camera_id`` so this
        install can resolve the source ``combined.mp4`` locally.
        """
        url = f"{self.api_base_url}/api/highlights/{reel_id}/game-clips"
        logger.debug("Fetching game clips for highlight reel %s", reel_id)
        return self._request("GET", url)

    def get_highlight_moment_clips(self, reel_id: str) -> list[dict[str, Any]]:
        """Get the moment clips linked to a moment-tagger highlight reel.

        GET {api_base_url}/api/highlights/{reel_id}/moment-clips

        Used when the polled reel has ``source='moment_tagger'``. Each item
        carries ``clip_start_offset`` / ``clip_end_offset`` (absolute offsets
        into the source ``combined.mp4``) plus ``recording_group_dir`` so this
        install can locate the source file. Ordered by the junction
        ``sequence_order``.
        """
        url = f"{self.api_base_url}/api/highlights/{reel_id}/moment-clips"
        logger.debug("Fetching moment clips for highlight reel %s", reel_id)
        return self._request("GET", url)

    def get_highlight(self, reel_id: str) -> dict[str, Any]:
        """Fetch the current state of a single highlight reel.

        GET {api_base_url}/api/highlights/{reel_id}

        Used for the idempotent-upload check: if a previous run uploaded to
        YouTube but failed to PATCH complete, the reel may already carry a
        ``youtube_video_id`` that we can reuse rather than uploading again.
        """
        url = f"{self.api_base_url}/api/highlights/{reel_id}"
        logger.debug("Fetching highlight reel %s", reel_id)
        return self._request("GET", url)

    def claim_highlight(self, reel_id: str, camera_id: str) -> dict[str, Any] | None:
        """Atomically transition a highlight reel from pending → generating.

        POST {api_base_url}/api/highlights/{reel_id}/claim
        body: {"camera_id": <camera_id>}

        Returns the updated reel dict on 200.
        Returns None on 409 (reel already claimed by another camera-manager or
        no longer pending) — callers should check for None and skip rendering.
        """
        url = f"{self.api_base_url}/api/highlights/{reel_id}/claim"
        body = {"camera_id": camera_id}
        logger.debug("Claiming highlight reel %s", reel_id)
        try:
            return self._request("POST", url, json=body)
        except TTTApiError as exc:
            if exc.status_code == 409:
                logger.info(
                    "reel %s already claimed by another camera-manager (409)", reel_id
                )
                return None
            raise

    def report_blocker(
        self, reel_id: str, camera_id: str, reason: str
    ) -> dict[str, Any] | None:
        """Report that this install cannot render the reel (e.g. missing source video).

        POST {api_base_url}/api/highlights/{reel_id}/report-blocker
        body: {"camera_id": <camera_id>, "reason": <text up to 500 chars>}

        Returns the response dict on success. On non-2xx, logs at WARNING and
        returns None — a failed blocker report must not kill the render loop.
        """
        url = f"{self.api_base_url}/api/highlights/{reel_id}/report-blocker"
        body = {"camera_id": camera_id, "reason": reason[:500]}
        logger.debug("Reporting blocker for reel %s: %s", reel_id, reason)
        try:
            return self._request("POST", url, json=body)
        except Exception as exc:
            logger.warning(
                "HIGHLIGHT_REEL: failed to report blocker for reel %s: %s", reel_id, exc
            )
            return None

    def update_highlight_progress(
        self,
        reel_id: str,
        *,
        stage: str,
        percent: int,
    ) -> dict[str, Any]:
        """Report per-stage render progress while the reel is generating.

        Stage is one of: 'trimming', 'concatenating', 'uploading'.
        Percent is clamped to 0-100 by the API.
        """
        url = f"{self.api_base_url}/api/highlights/{reel_id}"
        body = {
            "progress_stage": stage,
            "progress_percent": int(max(0, min(100, percent))),
        }
        logger.debug(
            "Highlight reel %s progress: %s %d%%",
            reel_id,
            stage,
            body["progress_percent"],
        )
        return self._request("PATCH", url, json=body)

    def complete_highlight(
        self,
        reel_id: str,
        *,
        file_path: str,
        youtube_video_id: str | None,
    ) -> dict[str, Any]:
        """Mark a highlight reel as rendered + uploaded (status='ready')."""
        url = f"{self.api_base_url}/api/highlights/{reel_id}"
        body: dict[str, Any] = {
            "status": "ready",
            "file_path": file_path,
            "youtube_video_id": youtube_video_id,
        }
        logger.debug(
            "Completing highlight reel %s (youtube_video_id=%s)",
            reel_id,
            youtube_video_id,
        )
        return self._request("PATCH", url, json=body)

    def fail_highlight(self, reel_id: str, error_message: str) -> dict[str, Any]:
        """Mark a highlight reel as failed with a human-readable error message."""
        url = f"{self.api_base_url}/api/highlights/{reel_id}"
        body = {"status": "failed", "error_message": error_message}
        logger.debug("Failing highlight reel %s: %s", reel_id, error_message)
        return self._request("PATCH", url, json=body)

    # ------------------------------------------------------------------
    # Schedule & game management
    # ------------------------------------------------------------------

    def get_schedule(
        self,
        team_id: str,
        start_date: str | None = None,
        end_date: str | None = None,
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
        recording_group_dir: str | None = None,
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

    def get_game_session_by_dir(
        self, recording_group_dir: str
    ) -> dict[str, Any] | None:
        """Find a single game session by its recording_group_dir (no team_id needed).

        GET {api_base_url}/api/game-sessions?recording_group_dir=... -> first match or None.
        The sync-client counterpart of ``MomentApiClient.get_game_session_by_dir`` — used by
        the phase-detect TTT push, which has the recording dir but not the team id.
        """
        url = f"{self.api_base_url}/api/game-sessions"
        sessions = self._request(
            "GET", url, params={"recording_group_dir": recording_group_dir}
        )
        return sessions[0] if sessions else None

    def create_game_session(
        self,
        team_id: str,
        recording_group_dir: str,
        game_date: str,
        opponent_name: str,
        video_youtube_id: str | None = None,
        status: str = "processing",
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

    def update_game_session_phases(
        self, session_id: str, **fields: Any
    ) -> dict[str, Any]:
        """Push detected/verified game-phase fields to a game session.

        Uses the camera-manager-authorized worker route, NOT the team-admin
        gated user route: soccer-cam runs on the camera manager's hardware and
        a camera manager need not be a team admin. Only phase_* fields are
        accepted (TTT ``GameSessionPhaseUpdate``).

        PATCH {api_base_url}/api/internal/game-sessions/{session_id}/phases
        """
        url = f"{self.api_base_url}/api/internal/game-sessions/{session_id}/phases"
        logger.debug("Pushing phases to game session %s", session_id)
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

        PATCH {api_base_url}/api/internal/moment-tags/{tag_id}

        Worker endpoint under the /api/internal namespace (no feature-flag
        gate). Auth: user JWT — TTT verifies the camera-manager is on the
        game's team. Fields: video_offset_seconds, trimmed_offset_seconds.
        """
        url = f"{self.api_base_url}/api/internal/moment-tags/{tag_id}"
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
        firmware_version: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any] | None:
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

    def get_camera_config(self, camera_id: str) -> dict[str, Any] | None:
        """Fetch camera config from TTT.

        GET {api_base_url}/api/device-link/camera-config
        """
        url = f"{self.api_base_url}/api/device-link/camera-config"
        logger.debug("Fetching camera config for %s", camera_id)
        return self._request("GET", url, params={"camera_id": camera_id})

    def push_camera_config(
        self, camera_id: str, config: dict[str, Any]
    ) -> dict[str, Any] | None:
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

    def update_recording_step(
        self,
        recording_id: str,
        *,
        step_id: str,
        step_type: str,
        label: str,
        status: str,
        started_at: str | None = None,
        completed_at: str | None = None,
        error: str | None = None,
        config: dict[str, Any] | None = None,
        artifacts: dict[str, Any] | None = None,
        pipeline_preset: str | None = None,
    ) -> dict[str, Any] | None:
        """Upsert one pipeline step for a recording.

        PATCH {api_base_url}/api/device-link/recordings/{recording_id}/status

        TTT appends a new step if step_id is unknown, or updates in-place if
        already present, preserving insertion order. When step_type=="upload"
        and status is "complete", TTT reads youtube_url/youtube_video_id from
        artifacts.
        """
        url = f"{self.api_base_url}/api/device-link/recordings/{recording_id}/status"
        body: dict[str, Any] = {
            "step_id": step_id,
            "type": step_type,
            "label": label,
            "status": status,
        }
        if started_at is not None:
            body["started_at"] = started_at
        if completed_at is not None:
            body["completed_at"] = completed_at
        if error is not None:
            body["error"] = error
        if config is not None:
            body["config"] = config
        if artifacts is not None:
            body["artifacts"] = artifacts
        if pipeline_preset is not None:
            body["pipeline_preset"] = pipeline_preset
        logger.debug("Updating recording %s: step %s=%s", recording_id, step_id, status)
        return self._request("PATCH", url, json=body)

    def enhanced_heartbeat(
        self, service_id: str, metrics: dict
    ) -> dict[str, Any] | None:
        """Send enhanced heartbeat with system metrics.

        PATCH {api_base_url}/api/device-link/heartbeat-enhanced
        """
        url = f"{self.api_base_url}/api/device-link/heartbeat-enhanced"
        data = {"service_id": service_id, **metrics}
        logger.debug("Sending enhanced heartbeat for service %s", service_id)
        return self._request("PATCH", url, json=data)

    def get_auto_record_rules(self, camera_id: str) -> dict[str, Any] | None:
        """Get auto-record rules for a camera.

        GET {api_base_url}/api/device-link/auto-record-rules?camera_id=
        """
        url = f"{self.api_base_url}/api/device-link/auto-record-rules"
        logger.debug("Fetching auto-record rules for camera %s", camera_id)
        return self._request("GET", url, params={"camera_id": camera_id})

    def get_pending_commands(self, camera_id: str) -> list[dict[str, Any]] | None:
        """Get pending commands for a camera.

        GET {api_base_url}/api/device-link/pending-commands?camera_id=
        """
        url = f"{self.api_base_url}/api/device-link/pending-commands"
        logger.debug("Fetching pending commands for camera %s", camera_id)
        return self._request("GET", url, params={"camera_id": camera_id})

    def acknowledge_command(self, command_id: str) -> dict[str, Any] | None:
        """Acknowledge receipt of a command.

        PATCH {api_base_url}/api/device-link/commands/{command_id}/acknowledge
        """
        url = f"{self.api_base_url}/api/device-link/commands/{command_id}/acknowledge"
        logger.debug("Acknowledging command %s", command_id)
        return self._request("PATCH", url)

    def complete_command(self, command_id: str, result: dict) -> dict[str, Any] | None:
        """Report command completion.

        PATCH {api_base_url}/api/device-link/commands/{command_id}/complete
        """
        url = f"{self.api_base_url}/api/device-link/commands/{command_id}/complete"
        logger.debug("Completing command %s", command_id)
        return self._request("PATCH", url, json=result)

    def get_high_water_mark(self, camera_id: str) -> str | None:
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

    # ------------------------------------------------------------------
    # Service registration & heartbeat
    # ------------------------------------------------------------------

    def register_service(self, machine_name: str, capabilities: dict) -> Any:
        """Register this service instance with TTT.

        POST {api_base_url}/api/device-link/register-service
        """
        url = f"{self.api_base_url}/api/device-link/register-service"
        body = {"machine_name": machine_name, "capabilities": capabilities}
        logger.debug("Registering service '%s' at %s", machine_name, url)
        return self._request("POST", url, json=body)

    def send_heartbeat(self, service_id: str, status: str = "online") -> Any:
        """Send heartbeat to TTT.

        PATCH {api_base_url}/api/device-link/heartbeat?service_id={service_id}
        """
        url = f"{self.api_base_url}/api/device-link/heartbeat"
        params = {"service_id": service_id}
        logger.debug("Sending heartbeat for service %s", service_id)
        return self._request("PATCH", url, params=params)

    # ------------------------------------------------------------------
    # Processing jobs
    # ------------------------------------------------------------------

    def get_pending_jobs(self) -> Any:
        """Get pending processing jobs assigned to this service.

        GET {api_base_url}/api/device-link/processing-jobs
        """
        url = f"{self.api_base_url}/api/device-link/processing-jobs"
        logger.debug("Fetching pending processing jobs from %s", url)
        return self._request("GET", url)

    def claim_job(self, job_id: str) -> Any:
        """Claim a processing job.

        PATCH {api_base_url}/api/processing-jobs/{job_id}/claim
        """
        url = f"{self.api_base_url}/api/processing-jobs/{job_id}/claim"
        logger.debug("Claiming processing job %s", job_id)
        return self._request("PATCH", url)

    def update_job_progress(self, job_id: str, status: str, progress: dict) -> Any:
        """Update job progress.

        PATCH {api_base_url}/api/processing-jobs/{job_id}/progress
        """
        url = f"{self.api_base_url}/api/processing-jobs/{job_id}/progress"
        body = {"status": status, "progress": progress}
        logger.debug("Updating progress for job %s: %s", job_id, status)
        return self._request("PATCH", url, json=body)

    def complete_job(self, job_id: str, result: dict) -> Any:
        """Mark job as complete with result.

        PATCH {api_base_url}/api/processing-jobs/{job_id}/complete
        """
        url = f"{self.api_base_url}/api/processing-jobs/{job_id}/complete"
        body = {"result": result}
        logger.debug("Completing job %s", job_id)
        return self._request("PATCH", url, json=body)

    def fail_job(self, job_id: str, error: str) -> Any:
        """Mark job as failed.

        PATCH {api_base_url}/api/processing-jobs/{job_id}/fail
        """
        url = f"{self.api_base_url}/api/processing-jobs/{job_id}/fail"
        body = {"error": error}
        logger.debug("Failing job %s: %s", job_id, error)
        return self._request("PATCH", url, json=body)

    # ------------------------------------------------------------------
    # Device configuration (onboarding)
    # ------------------------------------------------------------------

    def get_device_config(self) -> dict[str, Any] | None:
        """Retrieve stored device config for this camera manager.

        GET {api_base_url}/api/device-link/config
        Returns None if no config has been saved yet (HTTP 404).
        """
        url = f"{self.api_base_url}/api/device-link/config"
        logger.debug("Fetching device config from %s", url)
        try:
            return self._request("GET", url)
        except TTTApiError as exc:
            if exc.status_code == 404:
                return None
            raise

    def save_device_config(self, data: dict[str, Any]) -> dict[str, Any]:
        """Create or update device config for this camera manager.

        PUT {api_base_url}/api/device-link/config
        """
        url = f"{self.api_base_url}/api/device-link/config"
        logger.debug("Saving device config to %s", url)
        return self._request("PUT", url, json=data)

    # ------------------------------------------------------------------
    # Machine management
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Schedule providers
    # ------------------------------------------------------------------

    def list_schedule_providers(self, team_id: str) -> list[dict[str, Any]]:
        """List schedule providers for a team.

        GET {api_base_url}/api/device-link/schedule-providers?team_id=...
        """
        url = f"{self.api_base_url}/api/device-link/schedule-providers"
        params = {"team_id": team_id}
        logger.debug("Fetching schedule providers for team %s", team_id)
        return self._request("GET", url, params=params)

    def create_schedule_provider(
        self, data: dict[str, Any], dry_run: bool = False
    ) -> dict[str, Any]:
        """Create a schedule provider.

        POST {api_base_url}/api/device-link/schedule-providers

        When ``dry_run=True``, the backend validates the credentials and
        returns the discoverable teams without persisting. The response
        shape is ``{"ok": bool, "teams": [...], "error": str|None}``.
        Use this during onboarding to drive a team picker before the
        final create call.
        """
        url = f"{self.api_base_url}/api/device-link/schedule-providers"
        params = {"dry_run": "true"} if dry_run else None
        logger.debug(
            "Creating schedule provider (dry_run=%s): %s",
            dry_run,
            data.get("provider_type"),
        )
        return self._request("POST", url, params=params, json=data)

    def connect_playmetrics(self, email: str, password: str) -> dict[str, Any]:
        """Probe PlayMetrics credentials via TTT and return picker data.

        POST {api_base_url}/api/device-link/schedule-providers/playmetrics/connect

        TTT runs a one-time Firebase ``signInWithPassword`` and discovers
        the user's roles + teams, returning everything the tray wizard
        needs to render a role + team picker. The user's password is
        never persisted on either side. The caller then re-posts the
        chosen ``refresh_token`` + ``current_role_id`` + team ID to
        ``create_schedule_provider`` to finalize onboarding.

        Response shape: ``{"refresh_token": str, "roles": [...], "teams": [...]}``.
        """
        url = f"{self.api_base_url}/api/device-link/schedule-providers/playmetrics/connect"
        logger.debug("Connecting PlayMetrics for %s", email)
        return self._request("POST", url, json={"email": email, "password": password})

    def update_schedule_provider(
        self, provider_id: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Update a schedule provider.

        PUT {api_base_url}/api/device-link/schedule-providers/{provider_id}
        """
        url = f"{self.api_base_url}/api/device-link/schedule-providers/{provider_id}"
        logger.debug("Updating schedule provider %s", provider_id)
        return self._request("PUT", url, json=data)

    # ------------------------------------------------------------------
    # Machine management
    # ------------------------------------------------------------------

    def list_machines(self) -> list[dict[str, Any]]:
        """List all registered machines for this camera manager."""
        url = f"{self.api_base_url}/api/device-link/machines"
        return self._request("GET", url)

    def register_machine(self, machine_id: str, machine_name: str) -> dict[str, Any]:
        """Register or update a machine during onboarding.

        POST {api_base_url}/api/device-link/machines/register
        """
        url = f"{self.api_base_url}/api/device-link/machines/register"
        return self._request(
            "POST", url, json={"machine_id": machine_id, "machine_name": machine_name}
        )

    def list_machine_cameras(self) -> list[dict[str, Any]]:
        """Get per-camera enable state across all machines."""
        url = f"{self.api_base_url}/api/device-link/machine-cameras"
        return self._request("GET", url)

    def enable_camera_on_machine(
        self, camera_id: str, machine_id: str
    ) -> dict[str, Any]:
        """Enable a camera on a machine. Returns conflict info."""
        url = f"{self.api_base_url}/api/device-link/machine-cameras/{camera_id}/enable"
        return self._request("PATCH", url, json={"machine_id": machine_id})

    def disable_camera_on_machine(self, camera_id: str, machine_id: str) -> None:
        """Disable a camera on a machine."""
        url = f"{self.api_base_url}/api/device-link/machine-cameras/{camera_id}/disable"
        self._request("PATCH", url, json={"machine_id": machine_id})

    def confirm_camera_transfer(
        self, camera_id: str, from_machine_id: str, to_machine_id: str
    ) -> None:
        """Transfer a camera: disable on old machine, enable on new."""
        url = f"{self.api_base_url}/api/device-link/machine-cameras/{camera_id}/confirm-transfer"
        self._request(
            "POST",
            url,
            json={
                "from_machine_id": from_machine_id,
                "to_machine_id": to_machine_id,
            },
        )

    # ── Pipeline Questions ───────────────────────────────────────────────

    def create_pipeline_question(
        self,
        team_id: str,
        question_type: str,
        title: str,
        message: str,
        actions: list[dict[str, str]] | None = None,
        recording_group_dir: str | None = None,
        image_url: str | None = None,
        camera_id: str | None = None,
    ) -> dict[str, Any]:
        """Create an interactive pipeline question (push notification to camera manager).

        POST {api_base_url}/api/pipeline-questions
        """
        url = f"{self.api_base_url}/api/pipeline-questions"
        body: dict[str, Any] = {
            "team_id": team_id,
            "question_type": question_type,
            "title": title,
            "message": message,
            "actions": actions or [],
        }
        if recording_group_dir:
            body["recording_group_dir"] = recording_group_dir
        if image_url:
            body["image_url"] = image_url
        if camera_id:
            body["camera_id"] = camera_id
        logger.debug("Creating pipeline question: %s", question_type)
        return self._request("POST", url, json=body)

    def get_pipeline_question(self, question_id: str) -> dict[str, Any]:
        """Get a pipeline question (poll for response from camera manager).

        GET {api_base_url}/api/pipeline-questions/{question_id}
        """
        url = f"{self.api_base_url}/api/pipeline-questions/{question_id}"
        return self._request("GET", url)

    def cancel_pipeline_question(self, question_id: str) -> None:
        """Cancel a pipeline question (respond with 'cancelled').

        PATCH {api_base_url}/api/pipeline-questions/{question_id}/respond
        """
        url = f"{self.api_base_url}/api/pipeline-questions/{question_id}/respond"
        self._request("PATCH", url, json={"response_value": "__cancelled__"})

    # ------------------------------------------------------------------
    # Reprocess requests (cross-network reprocess flow)
    # ------------------------------------------------------------------

    def get_reprocess_queue(self) -> list[dict[str, Any]]:
        """Pending + this-user's in-flight reprocess requests across all
        teams the caller is a camera_manager for.

        GET {api_base_url}/api/internal/reprocess-requests/queue
        """
        url = f"{self.api_base_url}/api/internal/reprocess-requests/queue"
        return self._request("GET", url)

    def claim_reprocess_request(self, request_id: str) -> dict[str, Any]:
        """Atomically claim a pending request — only one camera-manager wins.

        POST {api_base_url}/api/internal/reprocess-requests/{request_id}/claim
        """
        url = f"{self.api_base_url}/api/internal/reprocess-requests/{request_id}/claim"
        return self._request("POST", url)

    def update_reprocess_status(
        self,
        request_id: str,
        status: str,
        current_step: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        """Advance the lifecycle column (claimed → running → completed/cancelled/failed).

        POST {api_base_url}/api/internal/reprocess-requests/{request_id}/status
        """
        url = f"{self.api_base_url}/api/internal/reprocess-requests/{request_id}/status"
        body: dict[str, Any] = {"status": status}
        if current_step is not None:
            body["current_step"] = current_step
        if error_message is not None:
            body["error_message"] = error_message
        return self._request("POST", url, json=body)

    def get_camera_recording(self, recording_id: str) -> dict[str, Any]:
        """Fetch a single recording by id — used by the reprocess flow to
        resolve the recording's ``file_group`` (= local recording_group_dir name).

        GET {api_base_url}/api/camera-recordings/{recording_id}
        """
        url = f"{self.api_base_url}/api/camera-recordings/{recording_id}"
        return self._request("GET", url)
