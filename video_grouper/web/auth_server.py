"""Headless TTT auth web server with a small status dashboard.

Lets a Docker / Linux user sign in to TTT via a browser. The OAuth flow
mirrors ``video_grouper/tray/onboarding_wizard.py:1820+``: the user is
redirected to Supabase's hosted ``/auth/v1/authorize`` endpoint with the
configured provider (Google, etc.); after sign-in Supabase redirects
back to ``/callback`` with the access token in the URL fragment; a
small JS snippet on that page reads the fragment and forwards the token
to ``/receive-token``, which calls
``TTTApiClient.set_session_from_token`` and writes
``shared_data/ttt/tokens.json`` for the rest of the pipeline to pick
up.

The dashboard at ``/`` shows auth state, pipeline queue sizes, camera
connectivity, and per-game progress. Live state (queues, cameras) comes
from a ``status_provider`` callable supplied by the orchestrator;
per-game state is read directly from disk so the dashboard works in
unit tests too.

Email / password is not handled here. Users who don't sign in to TTT via
an OAuth provider can keep using the existing ``[TTT] email + password``
config.ini path; this server is the alternative for OAuth users.

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
from urllib.parse import urlencode

from fastapi import Body, FastAPI, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from video_grouper.api_integrations.ttt_api import (
    TTTApiClient,
    TTTApiError,
    _decode_jwt_payload,
)
from video_grouper.utils.config import TTTConfig
from video_grouper.web.auth_status import clear_auth_needed, list_auth_needed

logger = logging.getLogger(__name__)

# TTT infrastructure defaults (not secrets -- Supabase anon keys are public).
# Mirror of onboarding_wizard.py:60-80; copied rather than imported so this
# module stays free of PyQt6 (the tray's transitive dep).
_TTT_DEFAULT_SUPABASE_URL = "https://zmuwmngqqiaectpcqlfj.supabase.co"
_TTT_DEFAULT_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inpt"
    "dXdtbmdxcWlhZWN0cGNxbGZqIiwicm9sZSI6"
    "ImFub24iLCJpYXQiOjE3NjU1MDE1MDksImV4"
    "cCI6MjA4MTA3NzUwOX0.UzAKgFWmXSFSN7uu"
    "JsmCXRR5c_0oSHyFjJYeBxbmzmY"
)
_TTT_DEFAULT_API_BASE_URL = "https://team-tech-tools.vercel.app"

try:
    from video_grouper.utils._ttt_config import (
        TTT_ANON_KEY,
        TTT_API_BASE_URL,
        TTT_SUPABASE_URL,
    )
except ImportError:
    TTT_SUPABASE_URL = _TTT_DEFAULT_SUPABASE_URL
    TTT_ANON_KEY = _TTT_DEFAULT_ANON_KEY
    TTT_API_BASE_URL = _TTT_DEFAULT_API_BASE_URL


# Default OAuth providers shown on the dashboard. Matches the providers
# enabled in TTT's Supabase config (auth.external.*). If a provider isn't
# enabled for a given deployment, clicking it surfaces a Supabase error
# page rather than a hard failure here.
_DEFAULT_PROVIDERS = ("google", "discord", "apple", "facebook", "twitter")

# Game group directory format from camera_poller.py / DirectoryState.
_GAME_DIR_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2}-\d{2}\.\d{2}\.\d{2}$")

# Loopback Host values always accepted by the rebinding-defense middleware,
# regardless of the configured bind. Anything else has to be the explicit bind.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


_DASHBOARD_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="10">
<title>Soccer-Cam</title>
<style>
body { font-family: system-ui, sans-serif; max-width: 760px; margin: 2em auto; padding: 0 1em; color: #222; }
h1 { font-size: 1.5rem; margin-bottom: 0.25rem; }
section { margin: 1.25rem 0; padding: 1rem 1.25rem; border: 1px solid #e5e7eb; border-radius: 6px; background: #fff; }
section h2 { font-size: 1rem; margin: 0 0 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; color: #475569; }
.muted { color: #6b7280; font-size: 0.85rem; }
.ok { color: #15803d; }
.warn { color: #b45309; }
.err { color: #b91c1c; }
.btn { display: inline-block; padding: 0.45rem 0.9rem; text-decoration: none; background: #2563eb; color: white !important; border-radius: 4px; font-weight: 600; border: 0; cursor: pointer; font-size: 0.9rem; }
.btn:hover { background: #1d4ed8; }
.btn-ghost { background: transparent; color: #2563eb !important; border: 1px solid #2563eb; }
.btn-ghost:hover { background: #eff6ff; }
table { border-collapse: collapse; width: 100%; }
td, th { padding: 0.4rem 0.6rem; border-bottom: 1px solid #f1f5f9; text-align: left; font-size: 0.9rem; vertical-align: top; }
th { color: #475569; font-weight: 600; }
code { background: #f3f4f6; padding: 0.1rem 0.3rem; border-radius: 3px; font-size: 0.9em; }
form.inline { display: inline; margin-left: 0.5rem; }
.status-dot { display: inline-block; width: 0.6rem; height: 0.6rem; border-radius: 50%; margin-right: 0.4rem; vertical-align: middle; }
.status-dot.on { background: #15803d; }
.status-dot.off { background: #94a3b8; }
.status-dot.bad { background: #b91c1c; }
.auth-details { margin-top: 0.5rem; border-top: 1px solid #f1f5f9; padding-top: 0.5rem; }
.auth-details summary { cursor: pointer; color: #475569; font-size: 0.9rem; padding: 0.25rem 0; }
.auth-form { display: flex; flex-direction: column; gap: 0.5rem; max-width: 320px; padding-top: 0.5rem; }
.auth-form label { display: flex; flex-direction: column; gap: 0.25rem; font-size: 0.85rem; color: #475569; }
.auth-form input { padding: 0.45rem 0.6rem; border: 1px solid #cbd5e1; border-radius: 4px; font-size: 0.9rem; }
.auth-form input:focus { outline: 2px solid #2563eb; outline-offset: -1px; border-color: #2563eb; }
.auth-form button { align-self: flex-start; }
.banner { padding: 0.85rem 1.1rem; border-radius: 6px; background: #fef3c7; border: 1px solid #fcd34d; color: #78350f; margin: 1rem 0; }
.banner strong { color: #78350f; }
.banner code { background: #fde68a; }
</style>
</head>
<body>
<h1>Soccer-Cam</h1>
<p class="muted">Auto-refreshes every 10s.</p>

<nav style="margin: 0 0 1rem;">
<a class="btn btn-ghost" href="/config">Configure</a>
<a class="btn btn-ghost" href="/setup">Setup wizard</a>
</nav>

__AUTH_FLAGS_BANNER__

<section>
<h2>Authentication</h2>
__AUTH_BLOCK__
</section>

<section id="youtube">
<h2>YouTube</h2>
__YOUTUBE_BLOCK__
</section>

<section id="tray">
<h2>Tray (autocam_gui)</h2>
__TRAY_BLOCK__
</section>

<section>
<h2>Pipeline</h2>
__QUEUES_BLOCK__
</section>

<section>
<h2>Cameras</h2>
__CAMERAS_BLOCK__
</section>

<section>
<h2>Games</h2>
__GAMES_BLOCK__
</section>
</body>
</html>
"""


_CALLBACK_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Completing sign-in...</title>
<style>
body { font-family: system-ui, sans-serif; max-width: 380px; margin: 4em auto; padding: 0 1em; color: #222; }
</style>
</head>
<body>
<h2>Completing sign-in&hellip;</h2>
<p id="status">Processing authentication response&hellip;</p>
<script>
(function() {
    var hash = window.location.hash.substring(1);
    if (!hash) {
        document.getElementById('status').textContent =
            'Error: no authentication data received from the identity provider.';
        return;
    }
    var params = new URLSearchParams(hash);
    // POST to /receive-token so the existing same-origin Origin/Referer
    // CSRF middleware applies. A GET endpoint would be reachable via
    // <img src=...> from any page; POST + same-origin enforcement isn't.
    var body = {
        access_token: params.get('access_token') || null,
        error: params.get('error') || null,
        error_description: params.get('error_description') || null
    };
    fetch('/receive-token', {
        method: 'POST',
        credentials: 'same-origin',
        headers: {'content-type': 'application/json'},
        body: JSON.stringify(body)
    }).then(function(resp) {
        return resp.text();
    }).then(function(text) {
        // Replace the page with the server-rendered success/error page so
        // _SUCCESS_PAGE's <meta http-equiv="refresh"> can return the user
        // to the dashboard.
        document.open();
        document.write(text);
        document.close();
    }).catch(function(err) {
        document.getElementById('status').textContent =
            'Error contacting local server: ' + err;
    });
})();
</script>
</body>
</html>
"""


_SUCCESS_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="2; url=/">
<title>Signed in</title>
<style>
body { font-family: system-ui, sans-serif; max-width: 380px; margin: 4em auto; padding: 0 1em; color: #222; }
.ok { color: #15803d; }
</style>
</head>
<body>
<h2 class="ok">Signed in to Team Tech Tools</h2>
<p>Tokens saved to <code>shared_data/ttt/tokens.json</code>. Returning to dashboard&hellip;</p>
<p><a href="/">Continue</a></p>
</body>
</html>
"""


_MAGIC_LINK_SENT_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Magic link sent</title>
<style>
body { font-family: system-ui, sans-serif; max-width: 420px; margin: 4em auto; padding: 0 1em; color: #222; }
.ok { color: #15803d; }
</style>
</head>
<body>
<h2 class="ok">Magic link sent</h2>
<p>Check <code>__EMAIL__</code> for a sign-in link from Team Tech Tools. Clicking the link in the email will return you to this server's <code>/callback</code> and complete sign-in.</p>
<p><a href="/">Back to dashboard</a></p>
</body>
</html>
"""


_ERROR_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Sign-in failed</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 380px; margin: 4em auto; padding: 0 1em; color: #222; }}
.err {{ color: #b91c1c; }}
</style>
</head>
<body>
<h2 class="err">Sign-in failed</h2>
<p>{message}</p>
<p><a href="/">Back to dashboard</a></p>
</body>
</html>
"""


def _resolve_url(override: str, fallback: str) -> str:
    return override.strip() if override and override.strip() else fallback


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
    if not client.is_authenticated():
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

    authenticated = time.time() < float(expires_at)
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
            cta = (
                ' <a class="btn btn-ghost" '
                'href="/auth/youtube/start">Re-authenticate</a>'
            )
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
            "isn't here, check <code>%LOCALAPPDATA%\\VideoGrouper\\logs\\"
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
        f'<pre style="background:#f3f4f6;padding:0.6rem;border-radius:4px;'
        f"overflow-x:auto;font-size:0.78rem;line-height:1.35;max-height:280px;"
        f'overflow-y:auto;">{body}</pre>'
    )


def _render_youtube_section(storage: Path) -> str:
    """Render the YouTube auth status block on the dashboard.

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
    return (
        '<p><span class="status-dot on"></span>Authorized under '
        f"<code>{ident}</code>"
        + (
            f" &mdash; token expires {_fmt_ts(status['expires_at'])}."
            if status["expires_at"]
            else "."
        )
        + "</p>"
        '<p><a class="btn btn-ghost" href="/auth/youtube/start">'
        "Re-authorize a different account</a></p>"
        f"{_YT_UPLOAD_FORM}"
    )


def _render_auth_section(token_file: Path, providers: tuple[str, ...]) -> str:
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
        # Token present but expired. Surface that, then offer re-sign-in.
        prefix = (
            f'<p class="warn"><span class="status-dot bad"></span>Token expired '
            f"on {_fmt_ts(s['expires_at'])}.</p>"
        )
    else:
        prefix = '<p><span class="status-dot off"></span>Not signed in.</p>'
    oauth_buttons = " ".join(
        f'<a class="btn" href="/login?provider={p}">{p.title()}</a>' for p in providers
    )
    oauth_block = (
        f'<p class="muted">Sign in with an OAuth provider:</p><p>{oauth_buttons}</p>'
    )
    password_form = """
<details class="auth-details">
<summary>Or sign in with email and password</summary>
<form method="post" action="/login/password" class="auth-form">
<label>Email <input name="email" type="email" autocomplete="username" required></label>
<label>Password <input name="password" type="password" autocomplete="current-password" required></label>
<button type="submit" class="btn">Sign in</button>
</form>
</details>
"""
    magic_form = """
<details class="auth-details">
<summary>Or get a magic link by email</summary>
<form method="post" action="/login/magic" class="auth-form">
<label>Email <input name="email" type="email" autocomplete="email" required></label>
<button type="submit" class="btn">Send magic link</button>
</form>
</details>
"""
    return prefix + oauth_block + password_form + magic_form


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
        return '<p class="muted">No cameras configured.</p>'
    rows = []
    for c in cameras:
        connected = c.get("connected")
        if connected is True:
            dot = '<span class="status-dot on"></span>connected'
        elif connected is False:
            dot = '<span class="status-dot bad"></span>not connected'
        else:
            dot = '<span class="status-dot off"></span>unknown'
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(c.get('name', '?')))}</td>"
            f"<td><code>{html.escape(str(c.get('ip', '?')))}</code></td>"
            f"<td>{dot}</td>"
            "</tr>"
        )
    return (
        "<table><tr><th>Name</th><th>IP</th><th>Status</th></tr>"
        + "".join(rows)
        + "</table>"
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
    providers: tuple[str, ...] = _DEFAULT_PROVIDERS,
    config_path: Path | None = None,
    node_role: str = "standalone",
    update_processor: Any | None = None,
) -> FastAPI:
    """Build the FastAPI app for the headless auth server.

    Backend URL resolution: ``[TTT].supabase_url`` etc. when set, otherwise
    the build-time ``_ttt_config`` constants, otherwise the production
    fallbacks above. The documented user path is "leave [TTT] URLs blank";
    overrides are a dev escape hatch.

    ``status_provider``: called on each dashboard render to surface live
    state from the orchestrator. Returns a dict with optional keys
    ``queue_sizes`` (dict[str, int]) and ``cameras`` (list of
    ``{name, ip, connected}``). The dashboard renders ``—`` /
    "not available" when ``None`` (used by tests and standalone runs).
    """
    supabase_url = _resolve_url(ttt_config.supabase_url, TTT_SUPABASE_URL)
    # supabase_url is the browser-facing URL emitted in OAuth redirects.
    # supabase_internal_url is what HTTP clients inside the container use
    # (e.g., a docker-network container name when Supabase runs as a
    # sibling container). Falls back to supabase_url when blank, which is
    # the right thing for production where one URL serves both legs.
    supabase_internal_url = _resolve_url(ttt_config.supabase_internal_url, supabase_url)
    anon_key = _resolve_url(ttt_config.anon_key, TTT_ANON_KEY)
    api_base_url = _resolve_url(ttt_config.api_base_url, TTT_API_BASE_URL)

    client = TTTApiClient(
        supabase_url=supabase_internal_url,
        anon_key=anon_key,
        api_base_url=api_base_url,
        storage_path=storage_path,
    )
    storage = Path(storage_path)
    token_file = storage / "ttt" / "tokens.json"

    app = FastAPI(title="Soccer-Cam Headless TTT Auth", version="0.3.0")

    # Mount the schema-driven config editor at /config when we know the
    # path on disk (the orchestrator passes it in; tests can opt in).
    if config_path is not None:
        from video_grouper.web.config_editor import build_router as _build_config

        app.include_router(_build_config(config_path))

        # And the onboarding wizard at /setup/*. Same trigger as the config
        # editor — both need a writable config path.
        from video_grouper.web.setup.router import build_router as _build_setup

        app.include_router(_build_setup(config_path))

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
    #   2. CSRF on state-changing endpoints — a malicious page can drive
    #      `<img src="http://localhost:8765/receive-token?access_token=...">`
    #      to silently sign the local pipeline into the attacker's TTT
    #      account. Defense: state-changing endpoints (including
    #      /receive-token) only accept POST, and POSTs must have a
    #      same-origin Origin or Referer.
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
            _DASHBOARD_PAGE.replace(
                "__AUTH_FLAGS_BANNER__", _render_auth_flags_banner(storage)
            )
            .replace("__AUTH_BLOCK__", _render_auth_section(token_file, providers))
            .replace("__YOUTUBE_BLOCK__", _render_youtube_section(storage))
            .replace("__TRAY_BLOCK__", _render_tray_section(storage))
            .replace("__QUEUES_BLOCK__", _render_queues_section(status))
            .replace("__CAMERAS_BLOCK__", _render_cameras_section(status))
            .replace("__GAMES_BLOCK__", _render_games_section(_scan_games(storage)))
        )
        return HTMLResponse(body)

    def _redirect_uri(request: Request) -> str:
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme
        host = request.headers.get("host") or request.url.netloc
        return f"{proto}://{host}/callback"

    @app.get("/login")
    def login(request: Request, provider: str = "google") -> RedirectResponse:
        # Do NOT pass `state` to Supabase. GoTrue treats client-supplied
        # state as authoritative for the redirect to Google but never
        # creates a flow_state row for it, so /auth/v1/callback rejects
        # with "OAuth state parameter is invalid". CSRF for /receive-token
        # is handled by POST + same-origin Origin/Referer enforcement
        # (see the host_and_origin_check middleware).
        params = urlencode(
            {
                "provider": provider,
                "redirect_to": _redirect_uri(request),
            }
        )
        authorize_url = f"{supabase_url}/auth/v1/authorize?{params}"
        logger.info("Redirecting to OAuth authorize: provider=%s", provider)
        return RedirectResponse(url=authorize_url, status_code=302)

    @app.post("/login/password")
    def login_password(email: str = Form(...), password: str = Form(...)) -> Response:
        try:
            client.login(email, password)
        except TTTApiError as exc:
            return HTMLResponse(
                _ERROR_TEMPLATE.format(message=html.escape(str(exc))),
                status_code=401,
            )
        logger.info("Password sign-in complete for %s", email)
        auto_claim_camera_manager(client)
        return RedirectResponse(url="/", status_code=303)

    @app.post("/login/magic")
    def login_magic(request: Request, email: str = Form(...)) -> Response:
        try:
            client.send_magic_link(email, _redirect_uri(request))
        except TTTApiError as exc:
            return HTMLResponse(
                _ERROR_TEMPLATE.format(message=html.escape(str(exc))),
                status_code=400,
            )
        return HTMLResponse(
            _MAGIC_LINK_SENT_PAGE.replace("__EMAIL__", html.escape(email))
        )

    @app.get("/callback", response_class=HTMLResponse)
    def callback() -> HTMLResponse:
        return HTMLResponse(_CALLBACK_PAGE)

    @app.post("/receive-token", response_class=HTMLResponse)
    def receive_token(payload: dict[str, Any] = Body(default={})) -> HTMLResponse:
        # POST + same-origin Origin/Referer (enforced by middleware) is
        # the CSRF defense for this endpoint; <img>/<script> attacks can
        # only do GET, and cross-origin form/fetch POSTs fail the
        # middleware check.
        access_token = payload.get("access_token")
        error = payload.get("error")
        error_description = payload.get("error_description")

        if not access_token:
            msg = error_description or error or "No access token returned."
            return HTMLResponse(
                _ERROR_TEMPLATE.format(message=html.escape(msg)), status_code=400
            )
        try:
            client.set_session_from_token(access_token)
        except (ValueError, KeyError) as exc:
            logger.error("Failed to persist OAuth token: %s", exc)
            return HTMLResponse(
                _ERROR_TEMPLATE.format(
                    message=f"Token rejected: {html.escape(str(exc))}"
                ),
                status_code=400,
            )
        logger.info("OAuth sign-in complete; tokens persisted")
        auto_claim_camera_manager(client)
        return HTMLResponse(_SUCCESS_PAGE)

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
                _ERROR_TEMPLATE.format(
                    message=f"Uploaded file is not valid JSON: {html.escape(str(exc))}"
                ),
                status_code=400,
            )
        installed = data.get("installed") or data.get("web") or {}
        if not installed.get("client_id") or not installed.get("client_secret"):
            return HTMLResponse(
                _ERROR_TEMPLATE.format(
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
                _ERROR_TEMPLATE.format(
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
                _ERROR_TEMPLATE.format(
                    message=f"YouTube upload module not available: {exc}"
                ),
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
                _ERROR_TEMPLATE.format(
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
                _ERROR_TEMPLATE.format(
                    message=f"Google reported an error: {html.escape(error)}"
                ),
                status_code=400,
            )
        if not code or not state:
            return HTMLResponse(
                _ERROR_TEMPLATE.format(
                    message="Callback missing code or state parameter."
                ),
                status_code=400,
            )
        _yt_states_gc()
        entry = _yt_oauth_states.pop(state, None)
        if entry is None:
            return HTMLResponse(
                _ERROR_TEMPLATE.format(
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
                _ERROR_TEMPLATE.format(
                    message=f"YouTube upload module not available: {exc}"
                ),
                status_code=500,
            )
        try:
            flow = make_youtube_flow(str(secret_file), entry["callback_uri"])
            flow.fetch_token(code=code)
            creds = flow.credentials
        except Exception as exc:
            logger.error("YouTube OAuth callback failed: %s", exc)
            return HTMLResponse(
                _ERROR_TEMPLATE.format(
                    message=f"OAuth exchange failed: {html.escape(str(exc))}"
                ),
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
        client._access_token = None
        client._refresh_token_value = None
        client._expires_at = None
        # Send the user back to the dashboard so the new state is visible.
        return RedirectResponse(url="/", status_code=303)

    return app
