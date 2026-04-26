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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from video_grouper.api_integrations.ttt_api import (
    TTTApiClient,
    TTTApiError,
    _decode_jwt_payload,
)
from video_grouper.utils.config import TTTConfig

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
        TTT_SUPABASE_URL,
        TTT_ANON_KEY,
        TTT_API_BASE_URL,
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
</style>
</head>
<body>
<h1>Soccer-Cam</h1>
<p class="muted">Auto-refreshes every 10s.</p>

<section>
<h2>Authentication</h2>
__AUTH_BLOCK__
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
    var accessToken = params.get('access_token');
    var error = params.get('error');
    var errorDesc = params.get('error_description');
    var url = '/receive-token?';
    if (accessToken) {
        url += 'access_token=' + encodeURIComponent(accessToken);
    } else {
        url += 'error=' + encodeURIComponent(error || 'unknown');
        if (errorDesc) {
            url += '&error_description=' + encodeURIComponent(errorDesc);
        }
    }
    window.location.replace(url);
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

    user_id: Optional[str] = None
    email: Optional[str] = None
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
        games.append(
            {
                "name": child.name,
                "status": data.get("status", "pending"),
                "error_message": data.get("error_message"),
                "files_count": len(data.get("files", {})),
            }
        )
    games.sort(key=lambda g: g["name"], reverse=True)
    return games


def _fmt_ts(ts: Optional[int]) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
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


def _render_queues_section(status: Optional[dict]) -> str:
    queue_sizes = (status or {}).get("queue_sizes")
    if not queue_sizes:
        return '<p class="muted">No live pipeline status available.</p>'
    rows = "".join(
        f"<tr><td>{html.escape(str(name))}</td><td>{html.escape(str(count))}</td></tr>"
        for name, count in queue_sizes.items()
    )
    return "<table><tr><th>Processor</th><th>Queue size</th></tr>" + rows + "</table>"


def _render_cameras_section(status: Optional[dict]) -> str:
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


def _render_games_section(games: list[dict]) -> str:
    if not games:
        return '<p class="muted">No game groups in storage yet.</p>'
    rows = []
    for g in games:
        err = g.get("error_message")
        err_html = (
            f' <span class="err">&mdash; {html.escape(err)}</span>' if err else ""
        )
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(g['name'])}</code></td>"
            f"<td>{html.escape(g['status'])}</td>"
            f"<td>{g['files_count']} files{err_html}</td>"
            "</tr>"
        )
    return (
        "<table><tr><th>Group</th><th>Status</th><th>Files</th></tr>"
        + "".join(rows)
        + "</table>"
    )


def create_app(
    ttt_config: TTTConfig,
    storage_path: str,
    status_provider: Optional[Callable[[], dict[str, Any]]] = None,
    providers: tuple[str, ...] = _DEFAULT_PROVIDERS,
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

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> HTMLResponse:
        try:
            status = status_provider() if status_provider else None
        except Exception as exc:
            logger.warning("status_provider raised: %s", exc)
            status = None
        body = (
            _DASHBOARD_PAGE.replace(
                "__AUTH_BLOCK__", _render_auth_section(token_file, providers)
            )
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
        params = urlencode(
            {"provider": provider, "redirect_to": _redirect_uri(request)}
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

    @app.get("/receive-token", response_class=HTMLResponse)
    def receive_token(
        access_token: Optional[str] = None,
        error: Optional[str] = None,
        error_description: Optional[str] = None,
    ) -> HTMLResponse:
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
        return HTMLResponse(_SUCCESS_PAGE)

    @app.post("/logout")
    def logout() -> Response:
        if token_file.exists():
            try:
                token_file.unlink()
            except OSError as exc:
                logger.warning("Failed to delete tokens.json: %s", exc)
                raise HTTPException(status_code=500, detail="Could not delete tokens")
        client._access_token = None
        client._refresh_token_value = None
        client._expires_at = None
        # Send the user back to the dashboard so the new state is visible.
        return RedirectResponse(url="/", status_code=303)

    return app
