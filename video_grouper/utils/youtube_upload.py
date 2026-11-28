import configparser
import json
import logging
import os
import time
import uuid
from collections.abc import Callable
from datetime import datetime

import google.oauth2.credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from video_grouper.models import MatchInfo

logger = logging.getLogger(__name__)


class YouTubeQuotaError(Exception):
    """Raised when YouTube API quota is exceeded. Should not be retried immediately."""

    pass


class YouTubeUploadRejectedError(Exception):
    """YouTube processed the upload and rejected it (duplicate, copyright,
    length, ...). Sending the same file again gets the same answer, so this is
    never retried: the queue fails the item instead of re-uploading a whole
    game up to three times."""

    pass


class YouTubeChannelNotVerifiedError(YouTubeQuotaError):
    """The channel cannot accept videos longer than 15 minutes.

    YouTube caps unverified channels at 15 minutes, and it does not refuse the
    upload: videos.insert returns 200 with a real id, the video briefly reports
    uploadStatus="uploaded", and then it is destroyed. videos.list then returns
    zero items forever, so there is no rejectionReason left to inspect -- the
    only trace is a "Deleted video" tombstone in the uploads playlist.
    (Measured 2026-08-23 on both the multipart and resumable paths.)

    Since a game is 60-90 minutes, an unverified channel loses every game
    silently. This is raised BEFORE uploading so nothing is lost.

    Subclasses YouTubeQuotaError deliberately: the queue processor already
    parks on that and re-probes on an interval instead of failing the item, so
    the upload is held and resumes on its own once the user verifies at
    https://www.youtube.com/verify -- no new machinery, nothing dropped.
    """

    pass


MAX_RETRIES = 5

# How long to watch a freshly uploaded video before believing it survived.
# The discarded 16-minute test video still reported uploadStatus="uploaded"
# at t+10s and was gone by t+20s, so presence alone -- checked early -- reports
# success for a video that is about to be destroyed.
# Unverified channels keep videos up to 15 minutes and destroy longer ones
# after accepting them. Measured 2026-11-27 on an unverified channel: 15 s to
# 15:30 all reached "processed" and stayed (checked at +38..63 min); 20:00 and
# 60:00 were accepted and gone within 15 s (16:00 likewise on 2026-08-23).
LONG_UPLOAD_LIMIT_SECONDS = 15 * 60


def _video_duration_seconds(path: str) -> float | None:
    """Container duration in seconds, or None if it can't be read."""
    try:
        import av

        with av.open(path) as container:
            if container.duration:
                return container.duration / av.time_base
    except Exception:
        return None
    return None


UPLOAD_SETTLE_TIMEOUT_SECONDS = 180
UPLOAD_SETTLE_POLL_SECONDS = 10

# If modifying these scopes, delete the token.json file.
# ``youtube`` already includes read access, so ``youtube.readonly`` is not
# requested: Google's sensitive-scope review asks for the narrowest set.
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",  # Required for playlist operations
]
API_SERVICE_NAME = "youtube"
API_VERSION = "v3"

# Default paths for YouTube credentials and token
YOUTUBE_DIR = "youtube"
CREDENTIALS_FILENAME = "client_secret.json"
TOKEN_FILENAME = "token.json"

# Embedded OAuth client for Desktop app -- users don't need to touch GCP Console.
# Client secrets for Desktop/Installed apps are NOT truly secret per Google's docs:
# https://developers.google.com/identity/protocols/oauth2/native-app
#
# Values are injected at build time via _youtube_secrets.py (generated from
# GitHub secrets by the build script) or read from environment variables.
# See build-installer.ps1 for the injection step.


def _load_embedded_client_config():
    """Load OAuth client config from build-time secrets or env vars."""
    # Try build-time generated secrets file first
    try:
        from video_grouper.utils._youtube_secrets import YT_CLIENT_ID, YT_CLIENT_SECRET
    except ImportError:
        # Fall back to environment variables
        YT_CLIENT_ID = os.environ.get("SOCCER_CAM_YT_CLIENT_ID", "")
        YT_CLIENT_SECRET = os.environ.get("SOCCER_CAM_YT_CLIENT_SECRET", "")

    return {
        "installed": {
            "client_id": YT_CLIENT_ID,
            "project_id": "team-tech-tools-app",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_secret": YT_CLIENT_SECRET,
            "redirect_uris": ["http://localhost"],
        }
    }


EMBEDDED_CLIENT_CONFIG = _load_embedded_client_config()

# The publisher (TTT) client additionally asks for the account's email so the
# quota-project step can check the user picked the same Google account twice.
PUBLISHER_SCOPES = SCOPES + [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]


class YouTubeQuotaProjectMissing(Exception):
    """A publisher-client token has no user-owned quota project configured.

    Raised instead of falling back: without the ``x-goog-user-project`` header
    every call bills the publisher's project, one pool shared by every install.
    See ROADMAP "Fail closed".
    """


def publisher_client_id() -> str:
    return EMBEDDED_CLIENT_CONFIG["installed"]["client_id"] or ""


def publisher_client_available() -> bool:
    """True when this build carries the publisher OAuth client."""
    cfg = EMBEDDED_CLIENT_CONFIG["installed"]
    return bool(cfg["client_id"] and cfg["client_secret"])


def is_publisher_token(token_data: dict) -> bool:
    cid = publisher_client_id()
    return bool(cid) and token_data.get("client_id") == cid


def is_byo_token(token_file: str) -> bool:
    """True only when the token's client is the ``client_secret.json`` beside it.

    Decided from the files, never from whether *this* process happens to know
    the publisher client id: a source run or a build without the injected id
    would otherwise treat a publisher token as BYO and bill the publisher's
    shared project (fail closed).
    """
    try:
        with open(token_file) as f:
            token_client = json.load(f).get("client_id")
        secret_file = os.path.join(os.path.dirname(token_file), CREDENTIALS_FILENAME)
        with open(secret_file) as f:
            blob = json.load(f)
    except (OSError, ValueError):
        return False
    cfg = blob.get("installed") or blob.get("web") or {}
    return bool(token_client) and cfg.get("client_id") == token_client


def make_publisher_flow(redirect_uri: str, scopes: list[str]) -> InstalledAppFlow:
    """Flow against the build-injected publisher client (no client_secret.json)."""
    flow = InstalledAppFlow.from_client_config(EMBEDDED_CLIENT_CONFIG, scopes)
    flow.redirect_uri = redirect_uri
    return flow


def load_youtube_credentials(token_file: str):
    """Load ``token.json``, attaching the user's quota project when required.

    BYO tokens (see :func:`is_byo_token`) load as before. Every other token
    gets ``with_quota_project(<id>)``, which sends ``x-goog-user-project`` on
    every call; with no project saved this raises
    :class:`YouTubeQuotaProjectMissing` rather than falling back.
    """
    from video_grouper.utils.youtube_quota_project import read_quota_project

    with open(token_file) as f:
        data = json.load(f)
    if is_byo_token(token_file):
        return google.oauth2.credentials.Credentials.from_authorized_user_info(
            data, SCOPES
        )
    saved = read_quota_project(os.path.dirname(token_file))
    if not saved:
        raise YouTubeQuotaProjectMissing(
            "YouTube uploads are paused: no Google Cloud quota project is set "
            "up for this YouTube connection (or its client_secret.json is "
            "missing). Use 'Set up YouTube quota' or reconnect YouTube on the "
            "dashboard."
        )
    creds = google.oauth2.credentials.Credentials.from_authorized_user_info(data)
    return creds.with_quota_project(saved["project_id"])


def make_youtube_flow(credentials_file: str, redirect_uri: str) -> InstalledAppFlow:
    """Build an InstalledAppFlow against the user's BYO ``client_secret.json``.

    The wizard's per-user model: each user creates their own GCP OAuth
    Desktop client (so their daily YouTube API quota is isolated from
    every other soccer-cam install) and drops the downloaded
    ``client_secret.json`` into ``<storage>/youtube/``. The web OAuth
    endpoints in ``web/auth_server.py`` use this flow to redirect the
    user's browser to Google's authorize URL with the loopback callback
    pointing back at the service's own port.
    """
    flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
    flow.redirect_uri = redirect_uri
    return flow


def get_youtube_paths(storage_path: str) -> tuple[str, str]:
    """Get the paths for YouTube credentials and token files.

    Args:
        storage_path: Base storage path from STORAGE.path

    Returns:
        Tuple[str, str]: (credentials_file_path, token_file_path)
    """
    youtube_dir = os.path.join(storage_path, YOUTUBE_DIR)
    os.makedirs(youtube_dir, exist_ok=True)

    credentials_file = os.path.join(youtube_dir, CREDENTIALS_FILENAME)
    token_file = os.path.join(youtube_dir, TOKEN_FILENAME)

    return credentials_file, token_file


def ensure_valid_token(credentials_file: str, token_file: str) -> tuple[bool, str]:
    """Check if YouTube token is valid, refreshing if needed. No interactive auth.

    This is used by the main pipeline (service) which runs headless.
    If the token is missing, expired, or cannot be refreshed, it returns failure
    so the caller can notify the user to re-authenticate via the tray app.

    Args:
        credentials_file: Path to the client_secret.json file
        token_file: Path to the token.json file

    Returns:
        Tuple[bool, str]: (success, message)
    """
    if not os.path.exists(token_file):
        if not os.path.exists(credentials_file):
            return False, "YouTube is not connected. Connect it on the dashboard."
        return (
            False,
            "No YouTube token found. Please authenticate via the tray app.",
        )

    # token.json carries its own client id/secret, so a publisher-client
    # token needs no client_secret.json beside it.
    try:
        creds = load_youtube_credentials(token_file)
    except YouTubeQuotaProjectMissing as e:
        return False, str(e)
    except Exception as e:
        return False, f"Error loading YouTube token: {e}"

    if creds.valid:
        return True, "Token is valid"

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            # Save refreshed token
            with open(token_file, "w") as f:
                f.write(creds.to_json())
            return True, "Token refreshed successfully"
        except Exception as e:
            return (
                False,
                f"Token refresh failed: {e}. Please re-authenticate via the tray app.",
            )

    return (
        False,
        "YouTube token expired and cannot be refreshed. "
        "Please re-authenticate via the tray app.",
    )


def authenticate_youtube(credentials_file: str, token_file: str) -> tuple[bool, str]:
    """Authenticate with YouTube API.

    Args:
        credentials_file: Path to the client_secret.json file
        token_file: Path to store the token.json file

    Returns:
        Tuple[bool, str]: (success, message)
    """
    try:
        # Check if credentials file exists
        if not os.path.exists(credentials_file):
            return False, f"Credentials file not found: {credentials_file}"

        creds = None

        # Check if token file exists
        if os.path.exists(token_file):
            try:
                with open(token_file) as token:
                    creds_data = json.load(token)
                    creds = (
                        google.oauth2.credentials.Credentials.from_authorized_user_info(
                            creds_data, SCOPES
                        )
                    )
            except Exception as e:
                logger.error(f"Error loading credentials from token file: {e}")

        # If credentials don't exist or are invalid, run the OAuth flow
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as e:
                    logger.error(f"Error refreshing credentials: {e}")
                    creds = None

            # If still no valid credentials, run the flow
            if not creds:
                # Try with trailing slash first (most common configuration)
                try:
                    flow = InstalledAppFlow.from_client_secrets_file(
                        credentials_file, SCOPES
                    )
                    # Use explicit redirect URI configuration with trailing slash
                    flow.redirect_uri = "http://localhost:8080/"
                    creds = flow.run_local_server(
                        port=8080, access_type="offline", prompt="consent"
                    )
                except Exception as first_error:
                    logger.error(
                        f"First OAuth attempt failed with trailing slash: {first_error}"
                    )

                    # Try without trailing slash
                    try:
                        flow = InstalledAppFlow.from_client_secrets_file(
                            credentials_file, SCOPES
                        )
                        # Use explicit redirect URI configuration without trailing slash
                        flow.redirect_uri = "http://localhost:8080"
                        creds = flow.run_local_server(
                            port=8080, access_type="offline", prompt="consent"
                        )
                    except Exception as second_error:
                        error_msg = str(second_error)
                        logger.error(
                            f"Second OAuth attempt failed without trailing slash: {second_error}"
                        )

                        # Provide detailed error message
                        if "redirect_uri_mismatch" in error_msg:
                            return False, (
                                "Redirect URI mismatch error. Please add ALL of the following redirect URIs to your OAuth client in Google Cloud Console:\n"
                                "- http://localhost:8080/ (with trailing slash)\n"
                                "- http://localhost:8080 (without trailing slash)\n"
                                "- http://127.0.0.1:8080/ (with trailing slash)\n"
                                "- http://127.0.0.1:8080 (without trailing slash)\n\n"
                                "Steps:\n"
                                "1. Go to Google Cloud Console > APIs & Services > Credentials\n"
                                "2. Edit your OAuth 2.0 Client ID\n"
                                "3. Add these URIs to the 'Authorized redirect URIs' section\n"
                                "4. Click Save and try again"
                            )
                        elif "invalid_client" in error_msg:
                            return (
                                False,
                                "Invalid client error. Please check that your credentials file is correct and not corrupted.",
                            )
                        elif "access_denied" in error_msg:
                            return (
                                False,
                                "Access denied. You declined to authorize the application.",
                            )

                        return False, f"Authentication failed: {error_msg}"

            # Save the credentials for the next run
            try:
                os.makedirs(os.path.dirname(token_file), exist_ok=True)
                with open(token_file, "w") as token:
                    token.write(creds.to_json())
            except Exception as e:
                logger.error(f"Error saving credentials to token file: {e}")
                return False, f"Failed to save token: {str(e)}"

        # Test the credentials by building the service
        try:
            youtube = build(API_SERVICE_NAME, API_VERSION, credentials=creds)

            # Instead of checking channels, just verify we can get upload status
            # which is compatible with the upload scope
            try:
                # Simple API call that works with upload scope
                youtube.videos().getRating(id="dQw4w9WgXcQ").execute()
                logger.info("YouTube API client created successfully")
                return True, "Successfully authenticated with YouTube"
            except Exception as e:
                logger.error(f"Error creating YouTube API client: {e}")
                return False, f"Failed to connect to YouTube API: {str(e)}"

        except Exception as e:
            logger.error(f"Error creating YouTube API client: {e}")
            return False, f"Failed to connect to YouTube API: {str(e)}"

    except Exception as e:
        logger.error(f"Unexpected error during authentication: {e}")
        return False, f"Authentication error: {str(e)}"


def authenticate_youtube_embedded(token_file: str) -> tuple[bool, str]:
    """Authenticate with YouTube using the embedded OAuth client.

    No client_secret.json file is needed -- the OAuth client config is
    shipped with the app. Users just sign into their Google account.

    Args:
        token_file: Path to store the token.json file

    Returns:
        Tuple[bool, str]: (success, message)
    """
    try:
        creds = None

        # Check if token file exists and is valid
        if os.path.exists(token_file):
            try:
                with open(token_file) as f:
                    creds_data = json.load(f)
                    creds = (
                        google.oauth2.credentials.Credentials.from_authorized_user_info(
                            creds_data, SCOPES
                        )
                    )
            except Exception as e:
                logger.error(f"Error loading token file: {e}")

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as e:
                    logger.error(f"Error refreshing credentials: {e}")
                    creds = None

            if not creds:
                flow = InstalledAppFlow.from_client_config(
                    EMBEDDED_CLIENT_CONFIG, SCOPES
                )
                creds = flow.run_local_server(
                    port=8080, access_type="offline", prompt="consent"
                )

            # Save the credentials
            try:
                os.makedirs(os.path.dirname(token_file), exist_ok=True)
                with open(token_file, "w") as f:
                    f.write(creds.to_json())
            except Exception as e:
                logger.error(f"Error saving token: {e}")
                return False, f"Failed to save token: {str(e)}"

        # Verify the credentials work
        try:
            youtube = build(API_SERVICE_NAME, API_VERSION, credentials=creds)
            youtube.videos().getRating(id="dQw4w9WgXcQ").execute()
            return True, "Successfully authenticated with YouTube"
        except Exception as e:
            logger.error(f"Error verifying YouTube API: {e}")
            return False, f"Failed to connect to YouTube API: {str(e)}"

    except Exception as e:
        logger.error(f"Unexpected error during authentication: {e}")
        return False, f"Authentication error: {str(e)}"


class YouTubeUploader:
    """Class to handle YouTube uploads."""

    def __init__(
        self,
        credentials_file: str,
        token_file: str,
        skip_upload: bool = False,
    ):
        """Initialize the YouTube uploader.

        Args:
            credentials_file: Path to the client_secret.json file
            token_file: Path to store the token.json file
            skip_upload: Smoke-test escape hatch. When True, upload_video()
                returns a fake ``smoke-<uuid>`` video id without calling
                the Google API and without touching credentials. MUST NOT be
                set in production — downstream consumers will end up with
                reels referencing video ids that don't exist on YouTube.
        """
        self.credentials_file = credentials_file
        self.token_file = token_file
        self._long_uploads_status: str | None = None
        self.skip_upload = skip_upload
        self.youtube = None

    def _authenticate_headless(self) -> bool:
        """Load the stored token (refreshing it if needed) without ever opening
        a browser. Status probes run from web requests and the service, where
        an interactive sign-in would block the request thread indefinitely."""
        ok, message = ensure_valid_token(self.credentials_file, self.token_file)
        if not ok:
            logger.warning("YouTube status check skipped: %s", message)
            return False
        # Through the fail-closed loader: a publisher token's status probes
        # must carry its quota project too, never bill the shared one.
        creds = load_youtube_credentials(self.token_file)
        self.youtube = build(API_SERVICE_NAME, API_VERSION, credentials=creds)
        return True

    def authenticate(self) -> bool:
        """Authenticate with YouTube API.

        Returns:
            bool: True if authentication was successful, False otherwise
        """
        if self._is_publisher_token():
            # Publisher tokens never go through the interactive BYO flow; a
            # missing quota project raises rather than billing the publisher.
            ok, message = ensure_valid_token(self.credentials_file, self.token_file)
            if not ok:
                logger.error("YouTube authentication failed: %s", message)
                return False
            creds = load_youtube_credentials(self.token_file)
            self.youtube = build(API_SERVICE_NAME, API_VERSION, credentials=creds)
            return True
        success, _ = authenticate_youtube(self.credentials_file, self.token_file)
        if success:
            creds = load_youtube_credentials(self.token_file)
            self.youtube = build(API_SERVICE_NAME, API_VERSION, credentials=creds)
            return True
        return False

    def _is_publisher_token(self) -> bool:
        try:
            if not os.path.exists(self.token_file):
                return False
            return not is_byo_token(self.token_file)
        except (OSError, ValueError):
            return False

    def get_long_uploads_status(self) -> str | None:
        """Whether this channel may upload videos longer than 15 minutes.

        Returns "allowed", "eligible", "disallowed" or None if unknown.
        "eligible" means the channel *could* be verified but has not been --
        long uploads are still destroyed. Only "allowed" is safe.

        Only "allowed" is cached (verification is one-way). Anything else is
        re-read on the next call, so verifying the channel takes effect on the
        next upload attempt without a restart. Never signs in interactively.
        """
        if self._long_uploads_status == "allowed":
            return "allowed"
        if not self.youtube and not self._authenticate_headless():
            return None
        try:
            resp = self.youtube.channels().list(part="status", mine=True).execute()
        except Exception as exc:
            logger.warning("Could not read channel upload status: %s", exc)
            return None
        items = resp.get("items", [])
        if not items:
            # 200 with no items means the Google account has no YouTube
            # channel at all. Sign-in looks healthy; uploads fail later.
            logger.error(
                "This Google account has no YouTube channel. "
                "Create one at https://www.youtube.com before uploading."
            )
            return None
        status = items[0].get("status", {}).get("longUploadsStatus")
        if status == "allowed":
            self._long_uploads_status = status
        return status

    def _confirm_upload_settled(self, video_id: str) -> str:
        """Watch a freshly uploaded video long enough to trust it.

        Returns "processed", "present" (still transcoding when we stopped
        watching -- normal for a large game) or "discarded".

        A video that YouTube destroys disappears from videos.list entirely, so
        disappearance is the signal. Checking rejectionReason cannot work: the
        video object is gone, so no field survives to carry a reason.
        """
        attempts = max(1, UPLOAD_SETTLE_TIMEOUT_SECONDS // UPLOAD_SETTLE_POLL_SECONDS)
        ever_seen = False
        any_answer = False  # did any status check actually get a reply?
        for attempt in range(attempts):
            if attempt:
                # Poll immediately first: a small video can already be
                # processed, and there is no reason to wait to find that out.
                time.sleep(UPLOAD_SETTLE_POLL_SECONDS)
            try:
                resp = self.youtube.videos().list(part="status", id=video_id).execute()
            except Exception as exc:
                logger.warning("Could not check upload status: %s", exc)
                continue
            if not isinstance(resp, dict):
                # Not an answer: count it like a failed check.
                logger.warning(
                    "Unexpected videos.list response for %s (%s)",
                    video_id,
                    type(resp).__name__,
                )
                continue
            any_answer = True
            items = resp.get("items", [])
            if not items:
                if ever_seen:
                    return "discarded"
                # Not indexed yet -- there is a propagation window of roughly
                # 10-20s after insert. Keep waiting.
                continue
            ever_seen = True
            state = items[0].get("status", {}).get("uploadStatus")
            if state == "processed":
                return "processed"
            if state in ("rejected", "failed"):
                status = items[0].get("status", {})
                reason = status.get("rejectionReason") or status.get("failureReason")
                raise YouTubeUploadRejectedError(
                    f"YouTube {state} video {video_id}: {reason}. Not retrying: "
                    "the same file would get the same answer."
                )
        if ever_seen:
            return "present"
        if not any_answer:
            # Every check failed (network, quota). We cannot tell whether the
            # video survived; insert returned an id, so keep it rather than
            # upload the whole game again as a likely duplicate.
            logger.error(
                "Could not confirm %s: every status check failed. Keeping it "
                "as uploaded rather than re-uploading a possible duplicate.",
                video_id,
            )
            return "present"
        return "discarded"

    def upload_video(
        self,
        video_path: str,
        title: str,
        description: str,
        tags: list[str] | None = None,
        privacy_status: str = "unlisted",
        playlist_id: str | None = None,
        on_progress: Callable[[int], None] | None = None,
    ) -> str | None:
        """Upload a video to YouTube.

        Args:
            video_path: Path to the video file
            title: Video title
            description: Video description
            tags: List of tags for the video
            privacy_status: Privacy status (private, unlisted, public)
            playlist_id: ID of playlist to add video to (optional)
            on_progress: Optional callable(percent_int 0-100) invoked after each
                chunk upload completes. Exceptions raised by the callback are
                caught and logged; they never abort the upload.

        Returns:
            str: Video ID if upload was successful, None otherwise
        """
        if self.skip_upload:
            if not os.path.exists(video_path):
                logger.error(f"Video file not found: {video_path}")
                return None
            fake_id = f"smoke-{uuid.uuid4().hex[:11]}"
            logger.warning(
                "YOUTUBE_SKIP_UPLOAD: returning fake video id %s for %s (smoke-test mode)",
                fake_id,
                video_path,
            )
            if on_progress is not None:
                try:
                    on_progress(100)
                except Exception as cb_exc:
                    logger.warning("on_progress callback raised: %s", cb_exc)
            return fake_id

        if not self.youtube:
            if not self.authenticate():
                logger.error("Failed to authenticate with YouTube API")
                return None

        if not os.path.exists(video_path):
            logger.error(f"Video file not found: {video_path}")
            return None

        # An unverified channel destroys anything over 15 minutes after
        # accepting it (no refusal, no reason), so a long upload has to be
        # held BEFORE anything is sent. Shorter videos -- clips, reels -- are
        # kept, so they go through. A video whose length can't be read is
        # treated as long. Decided 2026-11-27 on measurements (see
        # LONG_UPLOAD_LIMIT_SECONDS).
        duration = _video_duration_seconds(video_path)
        is_long = duration is None or duration > LONG_UPLOAD_LIMIT_SECONDS
        status = self.get_long_uploads_status() if is_long else None
        if status is not None and status != "allowed":
            raise YouTubeChannelNotVerifiedError(
                f"Holding {os.path.basename(video_path)}: this YouTube channel "
                f"is not verified (longUploadsStatus={status}), and YouTube "
                "deletes videos longer than 15 minutes from unverified "
                "channels. Clips and other short videos still upload. To fix "
                "it: open "
                "https://www.youtube.com/verify, sign in as the Google "
                "account this install is authorized under, and choose text "
                "message or voice call to receive a code. Held uploads then "
                "resume automatically."
            )

        # Get file size for progress tracking
        file_size = os.path.getsize(video_path)
        logger.info(
            f"Uploading {os.path.basename(video_path)} ({file_size / (1024 * 1024):.1f} MB)"
        )

        try:
            # Prepare the request body
            body = {
                "snippet": {
                    "title": title,
                    "description": description,
                    "tags": tags or [],
                    "categoryId": "17",  # Sports category
                },
                "status": {
                    "privacyStatus": privacy_status,
                    "selfDeclaredMadeForKids": False,
                },
            }

            # Use resumable upload for reliable large file transfers
            logger.info("Using resumable upload")
            media = MediaFileUpload(
                video_path, resumable=True, chunksize=50 * 1024 * 1024
            )

            # Create the upload request
            request = self.youtube.videos().insert(
                part=",".join(body.keys()), body=body, media_body=media
            )
            logger.info(f"Created upload request for video: {title}")

            # Execute the resumable upload with progress tracking
            start_time = time.time()
            logger.info(
                f"Starting upload... (file: {file_size / (1024 * 1024):.1f} MB)"
            )

            try:
                response = None
                while response is None:
                    status, response = request.next_chunk()
                    if status:
                        pct = int(status.progress() * 100)
                        elapsed = time.time() - start_time
                        speed = (
                            status.progress() * file_size / elapsed / 1024 / 1024
                            if elapsed > 0
                            else 0
                        )
                        logger.info(f"Upload {pct}% ({speed:.1f} MB/s)")
                        if on_progress is not None:
                            try:
                                on_progress(pct)
                            except Exception as cb_exc:
                                logger.warning(
                                    "on_progress callback raised: %s", cb_exc
                                )
                logger.info("Upload completed successfully")
                if on_progress is not None:
                    try:
                        on_progress(100)
                    except Exception as cb_exc:
                        logger.warning("on_progress final callback raised: %s", cb_exc)
            except HttpError as e:
                reason = ""
                # 429 is what the API actually returns when the project's
                # "Video Uploads per day" limit is hit. It was missing here,
                # so `reason` stayed empty, YouTubeQuotaError was never
                # raised, and the queue's quota-deferral path never ran --
                # the uploader just retried every 60s. Observed 2026-08-04:
                # 1,800 such calls in six hours, all answered identically.
                if e.resp and e.resp.status in (400, 403, 429):
                    try:
                        import json

                        detail = json.loads(e.content)
                        errors = detail.get("error", {}).get("errors", [])
                        reason = errors[0].get("reason", "") if errors else ""
                    except Exception:
                        pass
                # Belt and braces: a 429 from this API is a quota response
                # whatever the body parses to.
                if not reason and e.resp and e.resp.status == 429:
                    reason = "rateLimitExceeded"
                if reason in (
                    "uploadLimitExceeded",
                    "quotaExceeded",
                    "rateLimitExceeded",
                    "dailyLimitExceeded",
                ):
                    logger.error(
                        f"YouTube quota exceeded ({reason}). "
                        f"Upload will be retried after quota resets."
                    )
                    raise YouTubeQuotaError(reason) from e
                logger.error(f"Upload failed with error: {e}")
                return None
            except Exception as e:
                logger.error(f"Unexpected error during upload: {e}")
                return None

            total_time = time.time() - start_time
            logger.info("Upload completed")
            if response:
                video_id = response["id"]
                logger.info(f"Upload transferred: {title} (ID: {video_id})")
                logger.info(f"Upload completed in {total_time:.1f}s")

                # A returned id does not mean the video survived -- YouTube can
                # accept an upload and then destroy it. Confirm before telling
                # the caller this game is safely on YouTube.
                settled = self._confirm_upload_settled(video_id)
                if settled == "discarded":
                    logger.error(
                        "YouTube accepted and then DISCARDED %s (ID: %s). "
                        "The video no longer exists and was not uploaded. "
                        "Not recording it; the game will be retried.",
                        title,
                        video_id,
                    )
                    return None
                if settled == "present":
                    logger.warning(
                        "Video %s still transcoding after %ds; treating as uploaded.",
                        video_id,
                        UPLOAD_SETTLE_TIMEOUT_SECONDS,
                    )
                logger.info(f"Successfully uploaded video: {title} (ID: {video_id})")

                # Add to playlist only once we believe the video exists.
                if playlist_id:
                    self.add_video_to_playlist(video_id, playlist_id)

                return video_id
            else:
                logger.error("Upload completed but no response received")
                return None

        except YouTubeQuotaError:
            # MUST propagate. YouTubeQuotaError is an Exception, so the broad
            # handler below used to swallow it and return None -- the caller
            # then saw a generic failure, the queue processor's quota-deferral
            # branch never ran, and the uploader retried every ~12s forever.
            # Measured 2026-08-04: ~300 rejected calls an hour, all day,
            # unaffected by adding 429 detection upstream because the raise
            # never escaped this method.
            raise
        except YouTubeUploadRejectedError:
            raise  # permanent: the queue fails the item instead of retrying
        except Exception as e:
            logger.error(f"Error uploading video {video_path}: {e}")
            return None

    def find_playlist_by_name(self, name: str) -> str | None:
        """Find a playlist by name.

        Args:
            name: Name of the playlist to find

        Returns:
            str: Playlist ID if found, None otherwise
        """
        if not self.youtube:
            if not self.authenticate():
                logger.error("Failed to authenticate with YouTube API")
                return None

        try:
            # Get all playlists for the authenticated user
            request = self.youtube.playlists().list(
                part="snippet", mine=True, maxResults=50
            )

            while request:
                response = request.execute()

                for playlist in response["items"]:
                    if playlist["snippet"]["title"] == name:
                        playlist_id = playlist["id"]
                        logger.info(f"Found playlist '{name}' with ID: {playlist_id}")
                        return playlist_id

                # Check if there are more results
                request = self.youtube.playlists().list_next(request, response)

            logger.info(f"Playlist '{name}' not found")
            return None

        except Exception as e:
            logger.error(f"Error searching for playlist '{name}': {e}")
            return None

    def create_playlist(
        self, name: str, description: str = "", privacy_status: str = "unlisted"
    ) -> str | None:
        """Create a new playlist.

        Args:
            name: Name of the playlist
            description: Description of the playlist
            privacy_status: Privacy status (private, unlisted, public)

        Returns:
            str: Playlist ID if created successfully, None otherwise
        """
        if not self.youtube:
            if not self.authenticate():
                logger.error("Failed to authenticate with YouTube API")
                return None

        try:
            request = self.youtube.playlists().insert(
                part="snippet,status",
                body={
                    "snippet": {"title": name, "description": description},
                    "status": {"privacyStatus": privacy_status},
                },
            )

            response = request.execute()
            playlist_id = response["id"]
            logger.info(f"Created playlist '{name}' with ID: {playlist_id}")
            return playlist_id

        except Exception as e:
            logger.error(f"Error creating playlist '{name}': {e}")
            return None

    def get_or_create_playlist(
        self, name: str, description: str = "", privacy_status: str = "unlisted"
    ) -> str | None:
        """Get an existing playlist by name or create a new one.

        Args:
            name: Name of the playlist
            description: Description for the playlist if it needs to be created
            privacy_status: Privacy status for the playlist if it needs to be created

        Returns:
            str: Playlist ID if found or created successfully, None otherwise
        """
        playlist_id = self.find_playlist_by_name(name)
        if playlist_id:
            logger.info(f"Found existing playlist: {name} (ID: {playlist_id})")
            return playlist_id

        # Playlist not found, create it
        return self.create_playlist(name, description, privacy_status)

    def add_video_to_playlist(self, video_id: str, playlist_id: str) -> bool:
        """Add a video to a playlist.

        Args:
            video_id: The ID of the video to add
            playlist_id: The ID of the playlist to add the video to

        Returns:
            bool: True if the video was added successfully, False otherwise
        """
        if not self.youtube:
            if not self.authenticate():
                logger.error("Failed to authenticate with YouTube API")
                return False

        try:
            # Add the video to the playlist
            request = self.youtube.playlistItems().insert(
                part="snippet",
                body={
                    "snippet": {
                        "playlistId": playlist_id,
                        "resourceId": {"kind": "youtube#video", "videoId": video_id},
                    }
                },
            )

            request.execute()
            logger.info(f"Added video {video_id} to playlist {playlist_id}")
            return True

        except HttpError as e:
            logger.error(f"Error adding video to playlist: {e}")
            return False


def format_video_title(match_info, file_path: str, group_dir: str) -> str:
    """Format the video title according to the specified format.

    Args:
        match_info: MatchInfo object containing team names and location
        file_path: Path to the video file
        group_dir: Path to the group directory

    Returns:
        str: Formatted video title
    """
    # Extract date from group directory name (format: YYYY.MM.DD-HH.MM.SS)
    try:
        dir_name = os.path.basename(group_dir)
        date_part = dir_name.split("-")[0]  # YYYY.MM.DD
        date_obj = datetime.strptime(date_part, "%Y.%m.%d")
        formatted_date = date_obj.strftime("%m-%d-%Y")
    except Exception:
        # Fallback to current date if parsing fails
        formatted_date = datetime.now().strftime("%m-%d-%Y")

    # Base title format: "<my_team_name> vs <opponent_team_name> (<location>) MM-DD-YYYY"
    base_title = f"{match_info.my_team_name} vs {match_info.opponent_team_name} ({match_info.location}) {formatted_date}"

    # Check if this is a raw file
    if "-raw.mp4" in file_path:
        return f"{base_title} raw"

    return base_title


def get_playlist_name_from_mapping(
    team_name: str, config: configparser.ConfigParser
) -> str | None:
    """
    Get the base playlist name for a team from the config mapping.

    Args:
        team_name: The name of the team.
        config: The application config.

    Returns:
        The base playlist name, or None if not found.
    """
    if not config.has_section("YOUTUBE_PLAYLIST_MAPPING"):
        return None

    mapping = config["YOUTUBE_PLAYLIST_MAPPING"]
    return mapping.get(team_name) or mapping.get("Default")


def upload_group_videos(
    group_dir: str,
    credentials_file: str,
    token_file: str,
    processed_playlist_name: str | None = None,
    raw_playlist_name: str | None = None,
    privacy_status: str = "private",
) -> bool:
    """
    Uploads all videos in a group directory to YouTube.

    This function only handles the actual YouTube upload process.
    Playlist coordination and user interaction should be handled by the caller.

    Args:
        group_dir: The directory containing the videos and match_info.ini.
        credentials_file: Path to the YouTube API credentials file.
        token_file: Path to the YouTube API token file.
        processed_playlist_name: Name of playlist for processed videos (optional).
        raw_playlist_name: Name of playlist for raw videos (optional).
        privacy_status: Privacy status for uploaded videos.

    Returns:
        True if all uploads were successful, False otherwise.
    """
    match_info_path = os.path.join(group_dir, "match_info.ini")
    if not os.path.exists(match_info_path):
        logger.error(f"match_info.ini not found in {group_dir}")
        return False

    match_info = MatchInfo.from_file(match_info_path)
    if not match_info:
        logger.error(f"Could not load match info from {match_info_path}")
        return False

    uploader = YouTubeUploader(credentials_file, token_file)

    # Processed (trimmed) video
    processed_video_path = os.path.join(group_dir, "combined_trimmed.mp4")
    if os.path.exists(processed_video_path):
        logger.info(f"Uploading processed video: {processed_video_path}")
        title = match_info.get_youtube_title("processed")
        description = match_info.get_youtube_description("processed")
        playlist_id = None

        if processed_playlist_name:
            playlist_id = uploader.get_or_create_playlist(
                processed_playlist_name, description
            )

        success = uploader.upload_video(
            processed_video_path,
            title,
            description,
            privacy_status=privacy_status,
            playlist_id=playlist_id,
        )

        if not success:
            logger.error(f"Failed to upload processed video: {processed_video_path}")
            return False

    # Raw (untrimmed) video
    raw_video_path = os.path.join(group_dir, "combined.mp4")
    if os.path.exists(raw_video_path):
        logger.info(f"Uploading raw video: {raw_video_path}")
        title = match_info.get_youtube_title("raw")
        description = match_info.get_youtube_description("raw")
        playlist_id = None

        if raw_playlist_name:
            playlist_id = uploader.get_or_create_playlist(
                raw_playlist_name, description
            )

        success = uploader.upload_video(
            raw_video_path,
            title,
            description,
            privacy_status=privacy_status,
            playlist_id=playlist_id,
        )

        if not success:
            logger.error(f"Failed to upload raw video: {raw_video_path}")
            return False

    return True
