"""Soccer-cam's local web app: dashboard, TTT sign-in, YouTube connect.

TTT sign-in is TTT's own browser flow; soccer-cam knows only TTT's API URL.
``/login`` sends the browser to ``{api}/api/device-auth/start`` with a PKCE
challenge and a loopback ``redirect_uri`` (``/ttt/callback`` on this server).
The user signs in on TTT's site and approves the device; TTT redirects back
with a one-time code, which ``/ttt/callback`` redeems for this install's own
session (``TTTApiClient.complete_sign_in``) and writes
``shared_data/ttt/tokens.json`` for the rest of the pipeline.

The dashboard at ``/`` shows auth state, pipeline queue sizes, camera
connectivity, and per-game progress. Live state (queues, cameras) comes
from a ``status_provider`` callable supplied by the orchestrator;
per-game state is read directly from disk so the dashboard works in
unit tests too.

Trust model: unauthenticated. Default bind is localhost; anyone who can
reach the port is treated as the signed-in TTT user.
"""

import html
import json
import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from video_grouper.api_integrations.ttt_api import (
    TTTApiClient,
    TTTApiError,
    _decode_jwt_payload,
    resolve_api_base_url,
)
from video_grouper.utils.config import TTTConfig
from video_grouper.web import chrome as _chrome
from video_grouper.web.auth_status import clear_auth_needed, list_auth_needed

logger = logging.getLogger(__name__)

# Game group directory format from camera_poller.py / DirectoryState.
_GAME_DIR_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2}-\d{2}\.\d{2}\.\d{2}$")

# Loopback Host values always accepted by the rebinding-defense middleware,
# regardless of the configured bind. Anything else has to be the explicit bind.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


_DASHBOARD_BODY = """\
<main class="shell shell--rail">

<aside class="rail">
  <h2>On this page</h2>
  <ul>
    <li><a href="#auth">Authentication</a></li>
    <li><a href="#youtube">YouTube</a></li>
    <li><a href="#tray">Tray</a></li>
    <li><a href="#pipeline">Pipeline</a></li>
    <li><a href="#cameras">Cameras</a></li>
    <li><a href="#games">Games</a></li>
  </ul>
</aside>

<div>
<div class="page-header">
  <div>
    <h1>Status</h1>
    <p class="lede">What the capture appliance is doing right now. Refreshes every 10s.</p>
  </div>
</div>

__AUTH_FLAGS_BANNER__

<section class="panel" id="auth">
<h2>Authentication</h2>
__AUTH_BLOCK__
</section>

<section class="panel" id="youtube">
<h2>YouTube</h2>
__YOUTUBE_BLOCK__
</section>

<section class="panel" id="tray">
<h2>Tray</h2>
__TRAY_BLOCK__
</section>

<section class="panel" id="pipeline">
<h2>Pipeline</h2>
__QUEUES_BLOCK__
</section>

<section class="panel" id="cameras">
<h2>Cameras</h2>
__CAMERAS_BLOCK__
</section>

<section class="panel" id="games">
<h2>Games</h2>
__GAMES_BLOCK__
</section>

</div>
</main>
"""


def _capture_state(status: dict | None) -> str:
    """Derive the tally state from live pipeline status.

    The tally claims the appliance is capturing, so it is derived from work
    actually in flight -- never from "a camera exists". Unknown status means
    ``idle``: a strip that cannot see the pipeline must not imply recording.
    """
    if not status:
        return "idle"

    cameras = status.get("cameras") or []
    if any(c.get("connected") is False for c in cameras):
        return "error"

    queue_sizes = status.get("queue_sizes") or {}
    in_flight = sum(
        count
        for name, count in queue_sizes.items()
        if isinstance(count, int) and "download" in str(name).lower()
    )
    if in_flight > 0:
        return "recording"

    if any(c.get("connected") is True for c in cameras):
        return "armed"
    return "idle"


_SUCCESS_PAGE = _chrome.notice_page(
    "Signed in",
    "Signed in to Team Tech Tools",
    '<p class="muted">Tokens saved to <code>shared_data/ttt/tokens.json</code>. '
    "Returning to status&hellip;</p>"
    '<p><a href="/">Continue now</a></p>',
    tone="ok",
    extra_js='setTimeout(function(){location.href="/";}, 2000);',
)


def _success_page(storage: Path, return_to: str | None) -> str:
    """Page shown after TTT sign-in.

    From the wizard (``return_to`` set) it goes straight back there -- the
    wizard's YouTube step carries the Connect button. From the dashboard it
    offers Connect YouTube as a button when this build has the publisher
    client and nothing is connected yet; the user may want a different Google
    account for YouTube than for TTT, so it never starts on its own.
    """
    from video_grouper.web.youtube_connect import _safe_return, youtube_connection

    if return_to:
        target = _safe_return(return_to)
        return _chrome.notice_page(
            "Signed in",
            "Signed in to Team Tech Tools",
            f'<p class="muted">Returning&hellip;</p><p><a href="{target}">Continue now</a></p>',
            tone="ok",
            extra_js=f'setTimeout(function(){{location.href="{target}";}}, 1000);',
        )
    conn = youtube_connection(storage)
    if conn["available"] and not conn["connected"]:
        return _chrome.notice_page(
            "Signed in",
            "Signed in to Team Tech Tools",
            "<p>Connect a YouTube account so soccer-cam can upload your games. "
            "You'll choose which Google account and channel to use.</p>"
            '<p><a class="btn" href="/auth/youtube/connect">Connect YouTube</a> '
            '<a class="btn btn-ghost" href="/">Not now</a></p>',
            tone="ok",
        )
    return _SUCCESS_PAGE


def _error_page(message: str) -> str:
    """Render a sign-in failure. ``message`` is escaped by the caller."""
    return _chrome.notice_page(
        "Sign-in failed",
        "Sign-in failed",
        f'<p class="muted">{message}</p><p><a href="/">Back to status</a></p>',
        tone="bad",
    )


def auto_claim_camera_manager(client: TTTApiClient) -> None:
    """After a successful TTT sign-in, auto-claim camera-manager for the user's teams.

    Calls ``GET /api/device-link/me`` first; if the user already has any
    assignments we skip the register POST (idempotent on the server, but
    the GET is cheaper than re-walking the user's team_members rows).
    When ``GET`` returns empty, POSTs ``/api/internal/device-link/register-camera-manager``
    so every team the signed-in user is approved on gets a camera_managers row.

    All failures are swallowed and logged at WARNING — this is a
    best-effort bookkeeping step that must never block sign-in. The
    network call sits inline with sign-in (no polling loop, per the
    inline-pipeline-push convention).
    """
    if not client.has_session():
        logger.debug("Skipping camera-manager auto-claim: client not authenticated")
        return
    try:
        existing = client.get_team_assignments()
    except TTTApiError as exc:
        logger.warning(
            "Camera-manager auto-claim skipped: get_team_assignments failed: %s", exc
        )
        return
    except Exception as exc:  # noqa: BLE001 — best-effort, must not block sign-in
        logger.warning(
            "Camera-manager auto-claim skipped: get_team_assignments raised: %s", exc
        )
        return
    if existing:
        logger.debug(
            "Camera-manager auto-claim skipped: user already has %d assignment(s)",
            len(existing),
        )
        return
    try:
        rows = client.register_as_camera_manager()
    except TTTApiError as exc:
        logger.warning("Camera-manager auto-claim failed: %s", exc)
        return
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("Camera-manager auto-claim raised: %s", exc)
        return
    logger.info("Camera-manager auto-claim returned %d row(s)", len(rows))


# In-memory OAuth state store for the YouTube flow. Maps an
# unguessable random ``state`` token to the wizard's chosen
# ``storage_path`` (so the callback writes ``token.json`` to the
# right place even when the wizard's storage_path doesn't match the
# service's current config) and the URL to redirect to after success.
# Entries expire after 10 minutes to bound memory growth on abandoned
# attempts and prevent stale state-token replay.
_YT_STATE_TTL_SECONDS = 600
_yt_oauth_states: dict[str, dict] = {}


# TTT sign-in attempts: ``state`` -> PKCE verifier, redirect URI and where to
# land afterwards. Same TTL as the YouTube flow.
_ttt_sign_in_states: dict[str, dict] = {}


def _ttt_states_gc() -> None:
    cutoff = time.time() - _YT_STATE_TTL_SECONDS
    for k in [k for k, v in _ttt_sign_in_states.items() if v["created_at"] < cutoff]:
        _ttt_sign_in_states.pop(k, None)


def _yt_states_gc() -> None:
    """Drop OAuth state entries older than the TTL."""
    cutoff = time.time() - _YT_STATE_TTL_SECONDS
    stale = [k for k, v in _yt_oauth_states.items() if v["created_at"] < cutoff]
    for k in stale:
        _yt_oauth_states.pop(k, None)


def _read_youtube_status(token_file: Path) -> dict:
    """Summarize the current state of ``<storage>/youtube/token.json``.

    Mirrors :func:`_read_status` for the TTT case but pulls from
    google-auth's serialized credential format (``client_id``,
    ``client_secret``, ``refresh_token``, ``token``, ``expiry``,
    ``scopes``).
    """
    empty = {"authorized": False, "expires_at": None, "identity": None}
    if not token_file.exists():
        return empty
    try:
        data = json.loads(token_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read youtube token: %s", exc)
        return empty
    if not data.get("refresh_token"):
        return empty
    expiry_iso = data.get("expiry")
    expires_at: int | None = None
    if expiry_iso:
        try:
            # google-auth writes naive ISO timestamps in UTC.
            expires_at = int(
                datetime.fromisoformat(expiry_iso.replace("Z", "+00:00"))
                .replace(tzinfo=UTC if expiry_iso.endswith("Z") else None)
                .timestamp()
            )
        except (ValueError, TypeError):
            expires_at = None
    # token.json doesn't carry the user's email; ``client_id`` is the
    # nearest stable identifier and helps the user spot which OAuth
    # client they're authorized under (matters when comparing against
    # client_secret.json from a different GCP project).
    return {
        "authorized": True,
        "expires_at": expires_at,
        "identity": data.get("client_id"),
    }


def _read_status(token_file: Path) -> dict:
    """Read tokens.json from disk and summarize auth state.

    Returns ``identity`` (email when the JWT carries one, else ``sub``)
    alongside the raw fields so callers can show a readable label.
    """
    empty = {
        "authenticated": False,
        "user_id": None,
        "email": None,
        "identity": None,
        "expires_at": None,
    }
    if not token_file.exists():
        return empty
    try:
        data = json.loads(token_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read tokens.json: %s", exc)
        return empty

    access_token = data.get("access_token")
    expires_at = data.get("expires_at")
    if not access_token or not expires_at:
        return empty

    user_id: str | None = None
    email: str | None = None
    try:
        payload = _decode_jwt_payload(access_token)
        user_id = payload.get("sub")
        email = payload.get("email")
    except (ValueError, KeyError) as exc:
        logger.warning("Failed to decode tokens.json JWT: %s", exc)

    # The access token lives an hour; a stored refresh token keeps the install
    # signed in (the client refreshes on its next call).
    authenticated = bool(data.get("refresh_token")) or time.time() < float(expires_at)
    return {
        "authenticated": authenticated,
        "user_id": user_id,
        "email": email,
        "identity": email or user_id,
        "expires_at": int(float(expires_at)),
    }


def _scan_games(storage_path: Path) -> list[dict]:
    """Walk storage_path for game directories and read their state.json."""
    if not storage_path.is_dir():
        return []
    games: list[dict] = []
    for child in storage_path.iterdir():
        if not child.is_dir() or not _GAME_DIR_RE.match(child.name):
            continue
        state_file = child / "state.json"
        if not state_file.exists():
            continue
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        current_strength: str | None = None
        reproc_path = child / "reprocess_request.json"
        if reproc_path.exists():
            try:
                rd = json.loads(reproc_path.read_text(encoding="utf-8"))
                if isinstance(rd, dict):
                    current_strength = rd.get("stabilization_strength")
            except (json.JSONDecodeError, OSError):
                pass
        from video_grouper.pipeline.reprocess import is_pipeline_running

        games.append(
            {
                "name": child.name,
                "status": data.get("status", "pending"),
                "error_message": data.get("error_message"),
                "files_count": len(data.get("files", {})),
                "current_strength": current_strength,
                "running": is_pipeline_running(child),
            }
        )
    games.sort(key=lambda g: g["name"], reverse=True)
    return games


def _fmt_ts(ts: int | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(int(ts), tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _render_auth_flags_banner(storage: Path) -> str:
    """Render a banner if any provider has flagged itself for re-auth."""
    flags = list_auth_needed(storage)
    if not flags:
        return ""
    rows = []
    for f in flags:
        provider_raw = f.get("provider", "?")
        provider = html.escape(provider_raw)
        msg = html.escape(f.get("last_error", ""))
        since = html.escape(f.get("since", ""))
        # Per-provider Re-authenticate button. Only YouTube is wired
        # right now; other providers still fall back to the generic
        # prose below until they grow their own re-auth endpoint.
        cta = ""
        if provider_raw == "youtube":
            from video_grouper.web.youtube_connect import youtube_connection

            conn = youtube_connection(storage)
            if conn["connected"] and not conn["quota_project"]:
                href, label = "/auth/youtube/quota", "Set up YouTube quota"
            elif conn["connected"] or (
                conn["available"]
                and not (storage / "youtube" / "client_secret.json").exists()
            ):
                href, label = "/auth/youtube/connect", "Reconnect YouTube"
            else:
                href, label = "/auth/youtube/start", "Re-authenticate"
            cta = f' <a class="btn btn-ghost" href="{href}">{label}</a>'
        rows.append(
            f"<li><strong>{provider}</strong> &mdash; "
            f"<code>{msg}</code> "
            f'<span class="muted">(since {since})</span>{cta}</li>'
        )
    return (
        '<div class="banner">'
        "<strong>One or more providers need interactive sign-in:</strong>"
        f'<ul style="margin: 0.5rem 0 0 1.2rem; padding: 0;">{"".join(rows)}</ul>'
        "</div>"
    )


_YT_UPLOAD_FORM = """
<details class="auth-details">
<summary>Upload a different client_secret.json</summary>
<form method="post" action="/auth/youtube/upload-credentials"
      enctype="multipart/form-data" class="auth-form">
<label>client_secret.json from your GCP OAuth Desktop client
<input type="file" name="client_secret" accept="application/json" required>
</label>
<button class="btn" type="submit">Upload</button>
</form>
</details>
"""

_YT_BOOTSTRAP_HELP = """
<p class="muted">YouTube uploads need a one-time setup: create your own
GCP project + OAuth Desktop client (so your daily upload quota is
yours, not shared) and drop the downloaded
<code>client_secret.json</code> here. The
<a href="/setup/youtube">setup wizard's YouTube step</a> walks through
the GCP Console clicks.</p>
"""


def _render_tray_section(storage: Path) -> str:
    """Render a "Tray" status section on the dashboard.

    The tray (after load_config) writes to
    ``<storage>/logs/video_grouper_tray.log`` — same convention the
    service uses for its own log. We just read it from there. No
    marker-file lookup, no per-user-profile guesswork.

    If the file isn't present the tray either hasn't started since the
    storage path was created OR couldn't write to it (older builds, or
    a permission issue that pushed it back to its bootstrap
    LOCALAPPDATA path).
    """
    log_path = storage / "logs" / "video_grouper_tray.log"
    if not log_path.exists():
        return (
            '<p><span class="status-dot off"></span>Tray log not found at '
            f"<code>{html.escape(str(log_path))}</code>.</p>"
            '<p class="muted">The tray writes its log here once it loads '
            "the configured storage path. If the tray isn't expected to "
            "run on this install (Linux/Docker, or the configured "
            "<code>[PIPELINE]</code> has no tray step such as autocam), this "
            "is normal. If the tray IS running on this box but the log "
            "isn't here, check <code>%LOCALAPPDATA%\\SoccerCam\\logs\\"
            "</code> for the bootstrap fallback.</p>"
        )
    try:
        # Last 20 lines is enough to spot recent activity / errors
        # without flooding the dashboard. Tail the file by reading
        # backwards rather than loading it all into memory — these
        # logs grow fast under DEBUG.
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = 8192
            data = b""
            while size > 0 and data.count(b"\n") < 25:
                read = min(chunk, size)
                size -= read
                f.seek(size)
                data = f.read(read) + data
            lines = data.decode("utf-8", errors="replace").splitlines()[-20:]
    except OSError as exc:
        return (
            '<p class="warn"><span class="status-dot bad"></span>Could not '
            f"read tray log: {html.escape(str(exc))}</p>"
        )
    mtime = datetime.fromtimestamp(log_path.stat().st_mtime, tz=UTC)
    age_seconds = (datetime.now(tz=UTC) - mtime).total_seconds()
    if age_seconds < 120:
        dot = "on"
        age_label = f"active &mdash; last entry {int(age_seconds)}s ago"
    elif age_seconds < 600:
        dot = "on"
        age_label = f"active &mdash; last entry {int(age_seconds // 60)} min ago"
    else:
        dot = "bad"
        age_label = (
            f"stale &mdash; last entry {int(age_seconds // 60)} min ago "
            "(tray may be hung or stopped)"
        )
    body = "\n".join(html.escape(line) for line in lines)
    return (
        f'<p><span class="status-dot {dot}"></span>{age_label}</p>'
        f'<p class="muted">Log: <code>{html.escape(str(log_path))}</code></p>'
        f'<pre style="background:var(--color-bg-tertiary);padding:12px;'
        f"overflow-x:auto;font-size:0.78rem;line-height:1.35;max-height:280px;"
        f'overflow-y:auto;">{body}</pre>'
    )


def _youtube_uploads_enabled(config_path: Path | None) -> bool:
    """Is [YOUTUBE] enabled in config? False if it cannot be determined."""
    if config_path is None:
        return False
    try:
        from video_grouper.utils.config import load_config

        return bool(load_config(config_path).youtube.enabled)
    except Exception as exc:
        logger.debug("could not read youtube.enabled: %s", exc)
        return False


def _youtube_credentials_problem(storage: Path) -> str | None:
    """Why the stored credentials cannot be used, or None if they are fine.

    _read_youtube_status only inspects the token *file*, so a refresh token
    that Google has since expired or revoked still reads as "authorized".
    This actually attempts the refresh, which is the only way to know.
    """
    try:
        from video_grouper.utils.youtube_upload import ensure_valid_token

        yt_dir = storage / "youtube"
        ok, message = ensure_valid_token(
            str(yt_dir / "client_secret.json"), str(yt_dir / "token.json")
        )
        return None if ok else message
    except Exception as exc:
        return f"Could not validate the stored credentials: {exc}"


# The dashboard reloads every 10 s; probing YouTube on every load would spend
# quota all day for a value that changes about once in a channel's life.
_LONG_UPLOAD_STATUS_TTL_SECONDS = 15 * 60
_long_upload_status_cache: dict[str, tuple[float, float, str | None]] = {}


def _cached_long_uploads_status(storage: Path) -> str | None:
    """longUploadsStatus, re-read at most every 15 min or when the token file
    changes (a re-authorization may be a different channel)."""
    from video_grouper.utils.youtube_upload import YouTubeUploader

    yt_dir = storage / "youtube"
    token = yt_dir / "token.json"
    key = str(token)
    mtime = token.stat().st_mtime if token.exists() else 0.0
    hit = _long_upload_status_cache.get(key)
    if (
        hit
        and hit[1] == mtime
        and time.monotonic() - hit[0] < _LONG_UPLOAD_STATUS_TTL_SECONDS
    ):
        return hit[2]
    state = YouTubeUploader(
        credentials_file=str(yt_dir / "client_secret.json"),
        token_file=str(token),
    ).get_long_uploads_status()
    # Re-stat: the probe may have refreshed (rewritten) the token file.
    mtime = token.stat().st_mtime if token.exists() else 0.0
    _long_upload_status_cache[key] = (time.monotonic(), mtime, state)
    return state


def _render_long_upload_warning(storage: Path) -> str:
    """Warn if this channel cannot keep a full-length game.

    An unverified channel caps at 15 minutes, and YouTube does not refuse the
    upload -- it accepts it and then destroys the video. Uploads are held
    rather than lost (see YouTubeChannelNotVerifiedError), but the user has to
    know to go and verify, so surface it here rather than only in the log.
    """
    try:
        state = _cached_long_uploads_status(storage)
    except Exception as exc:  # never break the dashboard over a status probe
        logger.debug("long-upload status probe failed: %s", exc)
        return ""

    if state == "allowed":
        return (
            '<p class="muted"><span class="status-dot on"></span>'
            "Channel verified &mdash; full-length games can be uploaded.</p>"
        )
    if state is None:
        return (
            '<p class="warn"><span class="status-dot bad"></span>'
            "Could not read this channel&rsquo;s upload limit. If the Google "
            "account has no YouTube channel, create one at "
            '<a href="https://www.youtube.com" target="_blank" rel="noopener">'
            "youtube.com</a> before uploading.</p>"
        )
    return (
        '<p class="warn"><span class="status-dot bad"></span>'
        "<strong>Error &mdash; channel not verified. Uploads are blocked.</strong> "
        "YouTube will not accept videos longer than 15 minutes on an unverified "
        f"channel (<code>{html.escape(state)}</code>). soccer-cam is holding all "
        "uploads until this is fixed.</p>"
        "<p>To fix it:</p>"
        '<ol class="muted" style="line-height:1.7;">'
        '<li>Open <a href="https://www.youtube.com/verify" target="_blank" '
        'rel="noopener">youtube.com/verify</a> in a browser.</li>'
        "<li>Sign in as the <strong>same Google account</strong> this install "
        "is authorized under.</li>"
        "<li>Choose text message or voice call, and enter the code you are "
        "sent.</li>"
        "</ol>"
        '<p><a class="btn" href="https://www.youtube.com/verify" target="_blank" '
        'rel="noopener">Verify this channel</a> &mdash; held uploads resume '
        "automatically.</p>"
    )


def _render_youtube_section(storage: Path, config_path: Path | None = None) -> str:
    """Render the YouTube block: publisher client first, BYO as the fallback.

    With the publisher (TTT) client in this build, "Connect YouTube" is the
    primary action and the bring-your-own client sits behind a disclosure.
    Builds without it (source builds) show only the BYO flow.
    """
    from video_grouper.web.youtube_connect import youtube_connection

    conn = youtube_connection(storage)
    if conn["connected"]:
        if conn["quota_project"]:
            quota = (
                '<p><span class="status-dot on"></span>Connected'
                + (
                    f" as <code>{html.escape(conn['account'])}</code>"
                    if conn["account"]
                    else ""
                )
                + f"; uploads billed to your project "
                f"<code>{html.escape(conn['quota_project'])}</code>.</p>"
            )
            if conn["no_channel"]:
                from video_grouper.web.youtube_connect import NO_CHANNEL_HTML

                quota += NO_CHANNEL_HTML
        elif conn["provisioning"]:
            quota = (
                '<p><span class="status-dot off"></span>Setting up your quota '
                'project&hellip; <a href="/auth/youtube/quota">Show progress</a></p>'
            )
        else:
            quota = (
                '<p class="warn"><span class="status-dot bad"></span>YouTube is '
                "connected, but uploads are paused until your quota project is "
                "set up.</p>"
                '<p><a class="btn" href="/auth/youtube/quota">Set up YouTube quota</a></p>'
            )
            # Same checks as the BYO panel: usable credentials, and a channel
            # verified for full-length games (#145).
            if _youtube_uploads_enabled(config_path):
                problem = _youtube_credentials_problem(storage)
                if problem:
                    quota += (
                        '<p class="warn"><span class="status-dot bad"></span>'
                        "<strong>Warning &mdash; YouTube uploads are enabled, but "
                        "the stored credentials are not usable.</strong> "
                        f"{html.escape(problem)}</p>"
                    )
                else:
                    quota += _render_long_upload_warning(storage)
        return quota + (
            '<p><a class="btn btn-ghost" href="/auth/youtube/connect">'
            "Connect a different account</a></p>"
        )
    byo = _render_byo_youtube_section(storage, config_path)
    if not conn["available"]:
        return byo
    return (
        '<p><span class="status-dot off"></span>YouTube not connected.</p>'
        '<p><a class="btn" href="/auth/youtube/connect">Connect YouTube</a></p>'
        '<details class="auth-details"><summary>Use my own Google Cloud client '
        f"instead</summary>{byo}</details>"
    )


def _render_byo_youtube_section(storage: Path, config_path: Path | None = None) -> str:
    """Render the bring-your-own-client YouTube block.

    Shows three states:
      - No client_secret yet: bootstrap help + upload form
      - client_secret present, no token: prompt to authorize + replace
      - Both present and valid refresh token: signed-in summary +
        re-authorize-different-account button + replace
    """
    yt_dir = storage / "youtube"
    has_secret = (yt_dir / "client_secret.json").exists()
    token_file = yt_dir / "token.json"
    status = _read_youtube_status(token_file)

    if not has_secret:
        return (
            '<p><span class="status-dot off"></span>YouTube not configured.</p>'
            f"{_YT_BOOTSTRAP_HELP}"
            '<form method="post" action="/auth/youtube/upload-credentials"'
            ' enctype="multipart/form-data" class="auth-form">'
            "<label>Upload client_secret.json"
            '<input type="file" name="client_secret" accept="application/json" required>'
            "</label>"
            '<button class="btn" type="submit">Upload</button>'
            "</form>"
        )
    if not status["authorized"]:
        return (
            '<p class="warn"><span class="status-dot bad"></span>'
            "client_secret.json is here, but no YouTube account is "
            "authorized yet.</p>"
            '<p><a class="btn" href="/auth/youtube/start">Authorize</a></p>'
            f"{_YT_UPLOAD_FORM}"
        )
    ident = html.escape(status.get("identity") or "?")
    creds_warning = ""
    channel_note = ""
    if _youtube_uploads_enabled(config_path):
        problem = _youtube_credentials_problem(storage)
        # Only ask YouTube about the channel when uploads are on and the
        # credentials work: otherwise the probe can only fail (or spend quota
        # for nothing).
        if not problem:
            channel_note = _render_long_upload_warning(storage)
        if problem:
            creds_warning = (
                '<p class="warn"><span class="status-dot bad"></span>'
                "<strong>Warning &mdash; YouTube uploads are enabled, but the "
                "stored credentials are not usable.</strong> "
                f"{html.escape(problem)}</p>"
                '<p><a class="btn" href="/auth/youtube/start">'
                "Re-authorize</a></p>"
            )
    return (
        creds_warning + '<p><span class="status-dot on"></span>Authorized under '
        f"<code>{ident}</code>"
        + (
            f" &mdash; token expires {_fmt_ts(status['expires_at'])}."
            if status["expires_at"]
            else "."
        )
        + "</p>"
        + channel_note
        + '<p><a class="btn btn-ghost" href="/auth/youtube/start">'
        "Re-authorize a different account</a></p>"
        f"{_YT_UPLOAD_FORM}"
    )


def _render_auth_section(token_file: Path) -> str:
    s = _read_status(token_file)
    if s["authenticated"]:
        ident = html.escape(s["identity"] or "?")
        return (
            f'<p><span class="status-dot on"></span>Signed in as <code>{ident}</code> '
            f"&mdash; expires {_fmt_ts(s['expires_at'])}.</p>"
            '<form method="post" action="/logout" class="inline">'
            '<button type="submit" class="btn btn-ghost">Sign out</button></form>'
        )
    if s["expires_at"]:
        prefix = (
            f'<p class="warn"><span class="status-dot bad"></span>Session expired '
            f"on {_fmt_ts(s['expires_at'])}.</p>"
        )
    else:
        prefix = '<p><span class="status-dot off"></span>Not signed in.</p>'
    return (
        prefix + '<p><a class="btn" href="/login">Sign in to Team Tech Tools</a></p>'
        '<p class="muted">Opens Team Tech Tools in your browser. Sign in there '
        "however you normally do, then approve this camera.</p>"
    )


def _render_queues_section(status: dict | None) -> str:
    queue_sizes = (status or {}).get("queue_sizes")
    if not queue_sizes:
        return '<p class="muted">No live pipeline status available.</p>'
    rows = "".join(
        f"<tr><td>{html.escape(str(name))}</td><td>{html.escape(str(count))}</td></tr>"
        for name, count in queue_sizes.items()
    )
    return "<table><tr><th>Processor</th><th>Queue size</th></tr>" + rows + "</table>"


def _render_cameras_section(status: dict | None) -> str:
    cameras = (status or {}).get("cameras") or []
    if not cameras:
        # An empty screen is an invitation to act, not a dead end.
        return (
            '<p class="muted">No cameras configured yet.</p>'
            '<p><a class="btn btn-sm" href="/setup/camera?add=1">Add a camera</a></p>'
        )
    rows = []
    for c in cameras:
        connected = c.get("connected")
        if connected is True:
            dot = '<span class="status-dot on"></span>Connected'
        elif connected is False:
            dot = '<span class="status-dot bad"></span>Not connected'
        else:
            dot = '<span class="status-dot off"></span>Unknown'
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(c.get('name', '?')))}</td>"
            f"<td><code>{html.escape(str(c.get('ip', '?')))}</code></td>"
            f"<td>{dot}</td>"
            "</tr>"
        )
    # Seam calibration is a camera job, so it is launched from the cameras --
    # not from a global menu where it would sit unused between remounts.
    return (
        "<table><tr><th>Name</th><th>IP</th><th>Status</th></tr>"
        + "".join(rows)
        + "</table>"
        '<p class="btn-row" style="margin-top:16px">'
        '<a class="btn btn-sm btn-secondary" href="/stitch">Calibrate seam</a>'
        '<span class="hint">Realign the stitch after a knock or a remount.</span>'
        "</p>"
    )


_STRENGTH_OPTIONS = ("light", "standard", "heavy", "extreme")


def _render_reprocess_form(
    game_name: str, current_strength: str | None, running: bool
) -> str:
    """Per-row reprocess form: pick a stabilization strength + cheap-detect
    flag, POST writes ``reprocess_request.json`` into the group dir.

    When the pipeline is currently running for this group, the form is
    replaced by a Cancel button — one run at a time, with explicit
    user-driven cancellation if they want to change settings mid-flight.
    """
    name_esc = html.escape(game_name)
    if running:
        return (
            f'<form method="post" action="/recordings/{name_esc}/cancel" '
            'class="reproc reproc-cancel">'
            '<span class="muted">Pipeline running. </span>'
            '<button type="submit">Cancel</button>'
            "</form>"
        )
    options_html = "".join(
        f'<option value="{s}"{" selected" if s == current_strength else ""}>'
        f"{s}</option>"
        for s in _STRENGTH_OPTIONS
    )
    action = f"/recordings/{name_esc}/reprocess"
    current_label = (
        f' <span class="muted">(current: {html.escape(current_strength)})</span>'
        if current_strength
        else ""
    )
    return (
        f'<form method="post" action="{action}" class="reproc">'
        "<label>Strength: "
        f'<select name="stabilization_strength">{options_html}</select>'
        "</label> "
        '<label><input type="checkbox" name="skip_detect" value="1" checked> '
        "skip detect (reuse existing detections)</label> "
        '<button type="submit">Reprocess</button>'
        f"{current_label}"
        "</form>"
    )


def _render_games_section(games: list[dict]) -> str:
    if not games:
        return '<p class="muted">No game groups in storage yet.</p>'
    rows = []
    for g in games:
        err = g.get("error_message")
        err_html = (
            f' <span class="err">&mdash; {html.escape(err)}</span>' if err else ""
        )
        reproc_html = _render_reprocess_form(
            g["name"], g.get("current_strength"), bool(g.get("running"))
        )
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(g['name'])}</code></td>"
            f"<td>{html.escape(g['status'])}</td>"
            f"<td>{g['files_count']} files{err_html}</td>"
            f"<td>{reproc_html}</td>"
            "</tr>"
        )
    return (
        "<table><tr><th>Group</th><th>Status</th><th>Files</th>"
        "<th>Reprocess</th></tr>" + "".join(rows) + "</table>"
    )


def create_app(
    ttt_config: TTTConfig,
    storage_path: str,
    status_provider: Callable[[], dict[str, Any]] | None = None,
    config_path: Path | None = None,
    node_role: str = "standalone",
    update_processor: Any | None = None,
) -> FastAPI:
    """Build the FastAPI app for the headless auth server.

    TTT URL resolution: ``[TTT].api_base_url`` when set, otherwise the
    build-time ``_ttt_config.TTT_API_BASE_URL``, otherwise the fallback above.
    The documented user path is "leave it blank"; the override is for dev and
    self-hosted TTT.

    ``status_provider``: called on each dashboard render to surface live
    state from the orchestrator. Returns a dict with optional keys
    ``queue_sizes`` (dict[str, int]) and ``cameras`` (list of
    ``{name, ip, connected}``). The dashboard renders ``—`` /
    "not available" when ``None`` (used by tests and standalone runs).
    """
    client = TTTApiClient(
        api_base_url=resolve_api_base_url(ttt_config.api_base_url),
        storage_path=storage_path,
    )
    storage = Path(storage_path)
    token_file = storage / "ttt" / "tokens.json"

    app = FastAPI(title="Soccer-Cam Headless TTT Auth", version="0.3.0")

    # The shared stylesheet every page links. One sheet, one place to change
    # what Soccer-Cam looks like -- see video_grouper/web/chrome.py.
    app.mount(
        "/static",
        StaticFiles(directory=str(_chrome.STATIC_DIR)),
        name="static",
    )

    # Mount the schema-driven config editor at /config when we know the
    # path on disk (the orchestrator passes it in; tests can opt in).
    if config_path is not None:
        from video_grouper.web.config_editor import build_router as _build_config

        app.include_router(_build_config(config_path))

        # And the onboarding wizard at /setup/*. Same trigger as the config
        # editor — both need a writable config path.
        from video_grouper.web.setup.router import build_router as _build_setup

        app.include_router(_build_setup(config_path, storage_path))

        # And the stitch-seam calibration tool at /stitch/*. Same trigger
        # again -- it reads the camera credentials out of config.ini and
        # writes the calibration profile next to it.
        from video_grouper.web.stitch_calibration import (
            build_router as _build_stitch,
        )

        app.include_router(_build_stitch(config_path, storage))

    # Connect YouTube through the publisher (TTT) client + a quota project
    # the user owns. Routes explain themselves when the build has no client.
    from video_grouper.web.youtube_connect import build_router as _build_yt_connect

    app.include_router(_build_yt_connect(storage_path))

    # Phase 4: master nodes expose the worker-coordination API.
    if node_role == "master":
        from video_grouper.web.worker_api import build_router as _build_worker

        app.include_router(_build_worker(storage_path))

    # Auto-upgrade API. Only mounted when the orchestrator passes a
    # processor reference -- tests and standalone runs (no
    # VideoGrouperApp) skip it.
    if update_processor is not None:
        from video_grouper.web.update_api import build_router as _build_update

        app.include_router(_build_update(update_processor))

    # ---- Hardening: DNS-rebinding + CSRF defenses ----
    #
    # HTTP loopback is the right default for a local web app — Docker
    # Desktop, Ollama, Home Assistant, Jellyfin all do this. HTTPS only
    # matters on a non-trusted segment, which the loopback bind avoids.
    # But two threats remain even on loopback:
    #
    #   1. DNS rebinding — `evil.com` resolves to 127.0.0.1 and the
    #      victim's browser issues requests to localhost. Defense:
    #      validate the Host header against an allowlist.
    #   2. CSRF on state-changing endpoints — a malicious page could try to
    #      sign the local pipeline into the attacker's TTT account. Defense:
    #      state-changing endpoints only accept POST with a same-origin
    #      Origin or Referer, and /ttt/callback (a GET, by OAuth design) only
    #      accepts a ``state`` this server just issued for its own /login.
    bind = (ttt_config.auth_server_bind or "127.0.0.1").strip()
    allowed_hosts = set(_LOOPBACK_HOSTS)
    if bind and bind not in {"0.0.0.0", "127.0.0.1", "::1", "[::1]", "localhost"}:
        allowed_hosts.add(bind.lower())

    @app.middleware("http")
    async def host_and_origin_check(request: Request, call_next):
        # Host allowlist (DNS-rebinding defense)
        raw_host = request.headers.get("host") or ""
        host_name = raw_host.split(":")[0].strip("[]").lower()
        if host_name and host_name not in allowed_hosts:
            return PlainTextResponse(
                f"Host header '{raw_host}' is not allowed.\n"
                "If you reach this server via a non-loopback name, set "
                "[TTT].auth_server_bind in config.ini to that name.",
                status_code=403,
            )

        # Origin/Referer check on state-changing methods. Browsers send
        # one or both on every cross-origin request; we require the value
        # to point at us. Requests with neither header (curl, scripts) are
        # also rejected — there is no legitimate non-browser caller of
        # these endpoints, and accepting headerless POSTs would re-open
        # the CSRF gap that <img>-style attacks would otherwise hit.
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin")
            referer = request.headers.get("referer")
            check_value = origin or referer
            if not check_value:
                return PlainTextResponse(
                    "Missing Origin/Referer on state-changing request.",
                    status_code=403,
                )
            from urllib.parse import urlparse

            check_host = urlparse(check_value).hostname or ""
            if check_host.lower() not in allowed_hosts:
                return PlainTextResponse(
                    f"Origin '{check_value}' does not match this server.",
                    status_code=403,
                )

        return await call_next(request)

    @app.get("/")
    def dashboard() -> Response:
        # Phase 2 done-criterion: when shared_data has no real config yet
        # (or onboarding wasn't finished), bounce the user to the wizard
        # so the headless flow matches the tray's auto-launch behavior.
        if config_path is not None:
            from video_grouper.utils.config import config_needs_onboarding

            if config_needs_onboarding(config_path):
                return RedirectResponse(url="/setup/welcome", status_code=303)
        try:
            status = status_provider() if status_provider else None
        except Exception as exc:
            logger.warning("status_provider raised: %s", exc)
            status = None
        body = (
            _DASHBOARD_BODY.replace(
                "__AUTH_FLAGS_BANNER__", _render_auth_flags_banner(storage)
            )
            .replace("__AUTH_BLOCK__", _render_auth_section(token_file))
            .replace(
                "__YOUTUBE_BLOCK__",
                _render_youtube_section(storage, config_path),
            )
            .replace("__TRAY_BLOCK__", _render_tray_section(storage))
            .replace("__QUEUES_BLOCK__", _render_queues_section(status))
            .replace("__CAMERAS_BLOCK__", _render_cameras_section(status))
            .replace("__GAMES_BLOCK__", _render_games_section(_scan_games(storage)))
        )
        return HTMLResponse(
            _chrome.page(
                "Status",
                body,
                active="/",
                capture_state=_capture_state(status),
                refresh=10,
            )
        )

    @app.get("/login")
    def login(request: Request, return_to: str = "") -> Response:
        """Start TTT sign-in: hand the browser to TTT with a PKCE challenge."""
        from video_grouper.web.youtube_connect import _safe_return

        proto = request.headers.get("x-forwarded-proto") or request.url.scheme
        host = request.headers.get("host") or request.url.netloc
        hostname = (request.url.hostname or host.split(":")[0]).strip("[]").lower()
        if hostname not in _LOOPBACK_HOSTS and not hostname.startswith("127."):
            # TTT only returns sign-in codes to a loopback address on the
            # camera's own machine, so a dashboard opened over the LAN can't
            # finish sign-in. Say so instead of failing on TTT's side.
            port = request.url.port or 8765
            return HTMLResponse(
                _error_page(
                    message=(
                        "Sign in from the computer soccer-cam runs on: open "
                        f"<code>http://localhost:{port}/</code> there (or tunnel "
                        f"the port: <code>ssh -L {port}:localhost:{port} ...</code>). "
                        "Team Tech Tools only sends sign-in codes back to that "
                        "computer's own address."
                    )
                ),
                status_code=400,
            )
        redirect_uri = f"{proto}://{host}/ttt/callback"
        url, state, verifier = client.begin_sign_in(redirect_uri)
        _ttt_states_gc()
        _ttt_sign_in_states[state] = {
            "verifier": verifier,
            "redirect_uri": redirect_uri,
            "return_to": _safe_return(return_to) if return_to else None,
            "created_at": time.time(),
        }
        logger.info("Starting TTT sign-in")
        return RedirectResponse(url=url, status_code=302)

    @app.get("/ttt/callback", response_class=HTMLResponse)
    def ttt_callback(
        code: str | None = None, state: str | None = None, error: str | None = None
    ) -> HTMLResponse:
        """TTT sends the browser back here with a one-time code."""
        _ttt_states_gc()
        entry = _ttt_sign_in_states.pop(state or "", None)
        if error == "access_denied":
            return HTMLResponse(
                _error_page(message="Sign-in was cancelled. Nothing was changed."),
                status_code=400,
            )
        if error or not code or entry is None:
            return HTMLResponse(
                _error_page(
                    message=(
                        "This sign-in attempt expired or didn't come from this "
                        'camera. Start again from the <a href="/">dashboard</a>.'
                    )
                ),
                status_code=400,
            )
        try:
            client.complete_sign_in(code, entry["verifier"], entry["redirect_uri"])
        except TTTApiError as exc:
            logger.error("TTT sign-in failed: %s", exc)
            return HTMLResponse(
                _error_page(message="Team Tech Tools didn't accept the sign-in."),
                status_code=400,
            )
        auto_claim_camera_manager(client)
        return HTMLResponse(_success_page(storage, entry["return_to"]))

    @app.post("/auth/youtube/upload-credentials", response_class=HTMLResponse)
    async def youtube_upload_credentials(
        request: Request, client_secret: UploadFile
    ) -> Response:
        """Save a user-uploaded ``client_secret.json`` to disk.

        Reachable from the dashboard's YouTube section and from the
        wizard's YouTube step (the wizard has its own per-session
        endpoint that writes to its in-progress storage_path; this one
        writes to the running service's configured storage_path so it
        works after onboarding too).
        """
        try:
            raw = await client_secret.read()
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return HTMLResponse(
                _error_page(
                    message=f"Uploaded file is not valid JSON: {html.escape(str(exc))}"
                ),
                status_code=400,
            )
        installed = data.get("installed") or data.get("web") or {}
        if not installed.get("client_id") or not installed.get("client_secret"):
            return HTMLResponse(
                _error_page(
                    message=(
                        "client_secret.json missing client_id/client_secret. "
                        "Make sure you downloaded it from a Google Cloud "
                        "OAuth 2.0 Client (Desktop app type)."
                    )
                ),
                status_code=400,
            )
        target = storage / "youtube" / "client_secret.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        logger.info("YouTube client_secret.json uploaded to %s", target)
        # Send the user back to the dashboard so they can immediately
        # click Authorize next.
        return RedirectResponse(url="/", status_code=303)

    @app.get("/auth/youtube/start")
    def youtube_start(request: Request, return_to: str = "/") -> Response:
        """Kick off YouTube OAuth using the user's BYO client_secret.json.

        Reads ``<storage>/youtube/client_secret.json`` (the file the
        wizard's upload step or the /config page wrote earlier),
        generates an unguessable state token, and redirects the user's
        browser to Google's authorize URL with our local callback URL.
        Honors ``return_to`` so the wizard can land back at
        ``/setup/youtube`` and the dashboard banner can land back at
        ``/``.
        """
        # Reject open-redirect attempts: ``return_to`` must be a local
        # absolute path so the only landing target is this server.
        if not return_to.startswith("/") or return_to.startswith("//"):
            return_to = "/"
        secret_file = storage / "youtube" / "client_secret.json"
        if not secret_file.exists():
            return HTMLResponse(
                _error_page(
                    message=(
                        "No YouTube <code>client_secret.json</code> on disk. "
                        'Run the <a href="/setup/youtube">setup wizard\'s '
                        "YouTube step</a> to upload one, then retry."
                    )
                ),
                status_code=400,
            )
        try:
            from video_grouper.utils.youtube_upload import make_youtube_flow
        except ImportError as exc:
            logger.error("YouTube upload module unavailable: %s", exc)
            return HTMLResponse(
                _error_page(message=f"YouTube upload module not available: {exc}"),
                status_code=500,
            )
        # Use the same Host the user is hitting us on for the redirect
        # URI. Loopback Desktop OAuth clients accept any port on
        # localhost / 127.0.0.1, so this works without pre-registering
        # in GCP. The Host check middleware already validates raw_host.
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme
        host = request.headers.get("host") or request.url.netloc
        callback_uri = f"{proto}://{host}/auth/youtube/callback"
        try:
            flow = make_youtube_flow(str(secret_file), callback_uri)
            authorize_url, state = flow.authorization_url(
                access_type="offline",
                include_granted_scopes="true",
                prompt="consent",
            )
        except Exception as exc:
            logger.error("Failed to build YouTube OAuth URL: %s", exc)
            return HTMLResponse(
                _error_page(
                    message=f"Could not start YouTube OAuth: {html.escape(str(exc))}"
                ),
                status_code=500,
            )
        _yt_states_gc()
        _yt_oauth_states[state] = {
            "return_to": return_to,
            "callback_uri": callback_uri,
            "created_at": time.time(),
        }
        logger.info("Starting YouTube OAuth, state=%s return_to=%s", state, return_to)
        return RedirectResponse(url=authorize_url, status_code=302)

    @app.get("/auth/youtube/callback", response_class=HTMLResponse)
    def youtube_callback(
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ) -> Response:
        """Receive Google's OAuth redirect, persist the user's token."""
        if error:
            return HTMLResponse(
                _error_page(message=f"Google reported an error: {html.escape(error)}"),
                status_code=400,
            )
        if not code or not state:
            return HTMLResponse(
                _error_page(message="Callback missing code or state parameter."),
                status_code=400,
            )
        _yt_states_gc()
        entry = _yt_oauth_states.pop(state, None)
        if entry is None:
            return HTMLResponse(
                _error_page(
                    message=(
                        "OAuth state mismatch. The sign-in attempt expired or "
                        "was tampered with — please start over from the "
                        '<a href="/">dashboard</a>.'
                    )
                ),
                status_code=400,
            )
        secret_file = storage / "youtube" / "client_secret.json"
        try:
            from video_grouper.utils.youtube_upload import make_youtube_flow
        except ImportError as exc:
            return HTMLResponse(
                _error_page(message=f"YouTube upload module not available: {exc}"),
                status_code=500,
            )
        try:
            flow = make_youtube_flow(str(secret_file), entry["callback_uri"])
            flow.fetch_token(code=code)
            creds = flow.credentials
        except Exception as exc:
            logger.error("YouTube OAuth callback failed: %s", exc)
            return HTMLResponse(
                _error_page(message=f"OAuth exchange failed: {html.escape(str(exc))}"),
                status_code=400,
            )
        token_path = storage / "youtube" / "token.json"
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
        clear_auth_needed(storage_path, "youtube")
        logger.info("YouTube OAuth complete, token saved to %s", token_path)
        return RedirectResponse(url=entry["return_to"], status_code=303)

    @app.post("/recordings/{name}/reprocess")
    async def reprocess_recording(name: str, request: Request) -> Response:
        """Write ``reprocess_request.json`` to the named recording's group
        dir. The pipeline runner picks it up on the next run and patches
        the stabilize step (and optionally swaps detect for the cheap
        ``transform_detections`` step).

        Path traversal defense: ``name`` must match the canonical group
        dir naming pattern (YYYY.MM.DD-HH.MM.SS) and the resolved path
        must stay under ``storage``.
        """
        if not _GAME_DIR_RE.match(name):
            raise HTTPException(status_code=400, detail="bad recording name")
        group_dir = (storage / name).resolve()
        if not group_dir.is_dir() or storage.resolve() not in group_dir.parents:
            raise HTTPException(status_code=404, detail="recording not found")
        # One pipeline run per group at a time. If something is already
        # running here, the user must Cancel first — the runner can't
        # mid-flight swap configs cleanly and a queued "next config"
        # would silently shadow whatever the in-flight run produces.
        from video_grouper.pipeline.reprocess import is_pipeline_running

        if is_pipeline_running(group_dir):
            raise HTTPException(
                status_code=409,
                detail=(
                    "a pipeline run is already in progress for this "
                    "recording; cancel it first to change settings"
                ),
            )
        # Pull form fields; tolerate missing skip_detect (checkbox unchecked).
        form = await request.form()
        strength = form.get("stabilization_strength", "")
        if strength not in _STRENGTH_OPTIONS:
            raise HTTPException(status_code=400, detail="invalid strength")
        skip_detect = bool(form.get("skip_detect"))
        payload = {
            "stabilization_strength": strength,
            "skip_detect": skip_detect,
            "requested_at": datetime.now(UTC).isoformat(),
            "requested_by": "tray",
        }
        (group_dir / "reprocess_request.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        # Also nudge the group's state.json so the orchestrator's discovery
        # loop re-queues it. The runner's fingerprint check is what
        # ultimately drives the re-run, but the discovery loop only looks
        # at groups whose state.json isn't terminal.
        state_path = group_dir / "state.json"
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                # Roll any terminal status back to "queued" so the
                # pipeline_processor picks it up. Non-terminal statuses
                # are left alone.
                if state.get("status") in (
                    "pipeline_complete",
                    "ball_tracking_complete",
                    "complete",
                    "failed",
                ):
                    state["status"] = "pipeline_queued_reprocess"
                    state.pop("error_message", None)
                    state_path.write_text(json.dumps(state), encoding="utf-8")
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(
                    "reprocess: could not update state.json for %s: %s", name, exc
                )
        logger.info(
            "reprocess: queued %s (strength=%s, skip_detect=%s)",
            name,
            strength,
            skip_detect,
        )
        return RedirectResponse(url="/", status_code=303)

    @app.post("/recordings/{name}/cancel")
    async def cancel_recording(name: str) -> Response:
        """Drop a cancel marker the runner sees between steps. The
        in-flight step keeps going to completion (PyAV/ONNX can't be
        torn down mid-frame cleanly), but no subsequent steps run; the
        pipeline_processor marks the group ``pipeline_cancelled`` and
        clears any pending reprocess request."""
        if not _GAME_DIR_RE.match(name):
            raise HTTPException(status_code=400, detail="bad recording name")
        group_dir = (storage / name).resolve()
        if not group_dir.is_dir() or storage.resolve() not in group_dir.parents:
            raise HTTPException(status_code=404, detail="recording not found")
        from video_grouper.pipeline.reprocess import (
            is_pipeline_running,
            write_cancel_request,
        )

        if not is_pipeline_running(group_dir):
            raise HTTPException(
                status_code=409,
                detail="no pipeline run is currently in progress",
            )
        write_cancel_request(group_dir)
        logger.info("cancel: requested for %s", name)
        return RedirectResponse(url="/", status_code=303)

    @app.post("/logout")
    def logout() -> Response:
        if token_file.exists():
            try:
                token_file.unlink()
            except OSError as exc:
                logger.warning("Failed to delete tokens.json: %s", exc)
                raise HTTPException(
                    status_code=500, detail="Could not delete tokens"
                ) from exc
        client.clear_session()
        # Send the user back to the dashboard so the new state is visible.
        return RedirectResponse(url="/", status_code=303)

    return app
