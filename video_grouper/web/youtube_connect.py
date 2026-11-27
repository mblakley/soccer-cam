"""Connect YouTube with the publisher (TTT) client + a user-owned quota project.

Two Google consents, both started by a button (never automatically):

1. ``/auth/youtube/connect`` -- the publisher client, ``youtube.*`` scopes plus
   ``openid email``. Always shows Google's account chooser so the user can pick
   an existing account / channel. Writes ``<storage>/youtube/token.json``.
2. ``/auth/youtube/quota/start`` -- a setup-only consent for the Cloud project
   scopes, online access only. Its token is held in memory for one
   provisioning run (:mod:`video_grouper.utils.youtube_quota_project`) and
   never written anywhere.

``/auth/youtube/quota`` shows progress and polls ``/auth/youtube/quota/status``.
The result is ``<storage>/youtube/quota_project.json``; uploads refuse to run
with a publisher token until it exists (fail closed).

Only mounted usefully when the build carries the publisher client; without it
every route explains that and points at the bring-your-own flow.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from video_grouper.api_integrations.ttt_api import _decode_jwt_payload
from video_grouper.web import chrome as _chrome
from video_grouper.web.auth_status import clear_auth_needed

logger = logging.getLogger(__name__)

_STATE_TTL_SECONDS = 600
_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

# One provisioning run per process: there is one YouTube token per install.
_states: dict[str, dict] = {}
_job: dict[str, Any] = {"provisioner": None}


def reset_state() -> None:
    """Test hook."""
    _states.clear()
    _job["provisioner"] = None


def _gc() -> None:
    cutoff = time.time() - _STATE_TTL_SECONDS
    for k in [k for k, v in _states.items() if v["created_at"] < cutoff]:
        _states.pop(k, None)


_RETURN_RE = re.compile(r"^/[A-Za-z0-9/_-]*$")


def _safe_return(return_to: str | None) -> str:
    """Local path only, plain characters only.

    Blocks open redirects, and means the value can be dropped into query
    strings and HTML attributes without further escaping.
    """
    if not return_to or not _RETURN_RE.match(return_to) or "//" in return_to:
        return "/"
    return return_to


def _callback_uri(request: Request, path: str) -> str:
    # Loopback Desktop clients accept any port and path on 127.0.0.1/localhost.
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}{path}"


def _error(message: str, status: int = 400) -> HTMLResponse:
    return HTMLResponse(
        _chrome.notice_page(
            "YouTube",
            "Couldn't connect YouTube",
            f"<p>{message}</p><p><a href='/'>Back to status</a></p>",
            tone="bad",
        ),
        status_code=status,
    )


def youtube_connection(storage: Path) -> dict:
    """Summarise publisher-client connection state for pages to render."""
    from video_grouper.utils.youtube_quota_project import read_quota_project
    from video_grouper.utils.youtube_upload import (
        is_byo_token,
        publisher_client_available,
    )

    yt_dir = storage / "youtube"
    token_file = yt_dir / "token.json"
    # Anything that isn't a matching BYO client is the publisher's (fail closed).
    publisher = token_file.exists() and not is_byo_token(str(token_file))
    quota = read_quota_project(yt_dir) if publisher else None
    prov = _job["provisioner"]
    return {
        "available": publisher_client_available(),
        "connected": publisher,
        "quota_project": quota["project_id"] if quota else None,
        "account": quota.get("account") if quota else None,
        "no_channel": bool(quota and quota.get("has_channel") is False),
        "provisioning": bool(prov and not prov.status["done"]),
    }


def _raw_youtube_credentials(token_file: Path):
    """The publisher token *without* the quota header (for userinfo / probes)."""
    import google.oauth2.credentials
    from google.auth.transport.requests import Request as GRequest

    data = json.loads(token_file.read_text("utf-8"))
    creds = google.oauth2.credentials.Credentials.from_authorized_user_info(data)
    if not creds.valid:
        creds.refresh(GRequest())
        token_file.write_text(creds.to_json(), encoding="utf-8")
    return creds


def _reconnect_needed(return_to: str) -> HTMLResponse:
    back = _safe_return(return_to)
    return _error(
        "The YouTube connection has expired or was removed in your Google "
        "Account. "
        f"<a href='/auth/youtube/connect?return_to={back}'>Connect YouTube again</a>"
    )


def _account_email(creds) -> str | None:
    from google.auth.transport.requests import AuthorizedSession

    try:
        resp = AuthorizedSession(creds).get(_USERINFO_URL, timeout=30)
        if resp.status_code == 200:
            return resp.json().get("email")
        logger.warning("YOUTUBE_CONNECT: userinfo returned %s", resp.status_code)
    except Exception as exc:  # noqa: BLE001
        logger.warning("YOUTUBE_CONNECT: userinfo failed: %s", exc)
    return None


_YOUTUBE_SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
)

_CLOUD_SCOPES = (
    "https://www.googleapis.com/auth/cloudplatformprojects",
    "https://www.googleapis.com/auth/service.management",
)


def _granted_scopes(flow) -> set[str]:
    """Scopes Google actually granted (the token response's ``scope``)."""
    raw = (getattr(flow, "oauth2session", None) and flow.oauth2session.token or {}).get(
        "scope"
    )
    if isinstance(raw, str):
        return set(raw.split())
    if isinstance(raw, list | tuple | set):
        return set(raw)
    return set(getattr(flow.credentials, "granted_scopes", None) or [])


def _id_token_email(creds) -> str | None:
    raw = getattr(creds, "id_token", None)
    if not raw:
        return None
    try:
        return _decode_jwt_payload(raw).get("email")
    except ValueError:
        return None


# U8: channels.list says 200 with no items when the account has no channel.
NO_CHANNEL_HTML = (
    '<p class="warn">This Google account has no YouTube channel yet, so uploads '
    'will fail. <a href="https://www.youtube.com/create_channel" target="_blank" '
    'rel="noopener">Create a channel</a>, then you are set.</p>'
)

_STATUS_JS = """(function() {
  var box = document.getElementById('quota-status');
  var terms = document.getElementById('quota-terms');
  var done = document.getElementById('quota-done');
  function tick() {
    fetch('/auth/youtube/quota/status', {credentials: 'same-origin'})
      .then(function(r) { return r.json(); })
      .then(function(s) {
        box.textContent = s.message || s.phase;
        terms.hidden = s.phase !== 'needs_terms';
        if (s.done) {
          done.hidden = false;
          box.className = s.phase === 'ready' ? 'ok' : 'warn';
          return;
        }
        setTimeout(tick, 2000);
      })
      .catch(function() { setTimeout(tick, 4000); });
  }
  tick();
})();
"""


def build_router(storage_path: str) -> APIRouter:
    from video_grouper.utils import youtube_quota_project as _qp
    from video_grouper.utils import youtube_upload as _yu
    from video_grouper.utils.youtube_quota_project import (
        SETUP_SCOPES,
        TERMS_URL,
        clear_quota_project,
        read_quota_project,
        write_quota_project,
    )
    from video_grouper.utils.youtube_upload import (
        PUBLISHER_SCOPES,
        publisher_client_available,
    )

    default_storage = Path(storage_path)
    router = APIRouter()

    def _storage_for(request: Request) -> Path:
        # During onboarding the wizard may have picked a different storage
        # folder than the running service; write where config.ini will point.
        from video_grouper.web.setup.state import cookie_name, get

        state = get(request.cookies.get(cookie_name()))
        if state is not None and state.storage_path:
            return Path(state.storage_path)
        return default_storage

    def _unavailable() -> HTMLResponse:
        return _error(
            "This build doesn't include the Team Tech Tools YouTube client. "
            'Use the <a href="/setup/youtube">bring-your-own Google Cloud '
            "client</a> instead.",
            status=404,
        )

    # -- consent 1: YouTube via the publisher client ------------------------

    @router.get("/auth/youtube/connect")
    def connect(request: Request, return_to: str = "/") -> Response:
        if not publisher_client_available():
            return _unavailable()
        callback = _callback_uri(request, "/auth/youtube/connect/callback")
        flow = _yu.make_publisher_flow(callback, PUBLISHER_SCOPES)
        url, state = flow.authorization_url(
            access_type="offline",
            # Account chooser every time: the user may already have the
            # channel on another Google account.
            prompt="select_account consent",
            include_granted_scopes="false",
        )
        _gc()
        _states[state] = {
            "kind": "connect",
            "storage": _storage_for(request),
            "callback": callback,
            "return_to": _safe_return(return_to),
            "created_at": time.time(),
        }
        return RedirectResponse(url, status_code=302)

    @router.get("/auth/youtube/connect/callback")
    def connect_callback(
        code: str | None = None, state: str | None = None, error: str | None = None
    ) -> Response:
        entry = _states.pop(state or "", None)
        if error:
            return _error(f"Google reported: {html.escape(error)}")
        if not entry or entry["kind"] != "connect" or not code:
            return _error(
                "This sign-in attempt expired. Start again from the dashboard."
            )
        flow = _yu.make_publisher_flow(entry["callback"], PUBLISHER_SCOPES)
        try:
            flow.fetch_token(code=code)
        except Exception as exc:  # noqa: BLE001
            logger.error("YOUTUBE_CONNECT: token exchange failed: %s", exc)
            return _error(f"Token exchange failed: {html.escape(str(exc))}")
        creds = flow.credentials
        granted = _granted_scopes(flow)
        missing_yt = [s for s in _YOUTUBE_SCOPES if s not in granted]
        if missing_yt:
            return _error(
                "Google didn't grant all the YouTube permissions soccer-cam needs "
                "to upload. On Google's screen, tick <strong>every</strong> box "
                "(or <em>Select all</em>). "
                f"<a href='/auth/youtube/connect?return_to={entry['return_to']}'>"
                "Try again</a>"
            )
        email = _id_token_email(creds)
        yt_dir = entry["storage"] / "youtube"
        yt_dir.mkdir(parents=True, exist_ok=True)
        (yt_dir / "token.json").write_text(creds.to_json(), encoding="utf-8")
        clear_auth_needed(str(entry["storage"]), "youtube")
        # An earlier provisioning run belongs to the previous connection; its
        # "ready" must not be shown for this one.
        prov = _job["provisioner"]
        if prov is None or prov.status["done"]:
            _job["provisioner"] = None

        # Reconnecting the same account keeps its project; a different
        # account can't use the old one (403 on every call), so drop it.
        saved = read_quota_project(yt_dir)
        if saved and (not email or saved.get("account") != email):
            clear_quota_project(yt_dir)
            saved = None
        logger.info("YOUTUBE_CONNECT: connected; quota project %s", bool(saved))
        if saved:
            return RedirectResponse(entry["return_to"], status_code=303)
        return RedirectResponse(
            f"/auth/youtube/quota?return_to={entry['return_to']}", status_code=303
        )

    # -- consent 2: one-time Cloud scopes ------------------------------------

    @router.get("/auth/youtube/quota/start")
    def quota_start(request: Request, return_to: str = "/") -> Response:
        if not publisher_client_available():
            return _unavailable()
        storage = _storage_for(request)
        token_file = storage / "youtube" / "token.json"
        if not youtube_connection(storage)["connected"]:
            return RedirectResponse(
                f"/auth/youtube/connect?return_to={_safe_return(return_to)}",
                status_code=303,
            )
        try:
            email = _account_email(_raw_youtube_credentials(token_file))
        except Exception as exc:  # noqa: BLE001 -- RefreshError, network
            logger.warning("YOUTUBE_QUOTA: YouTube token unusable: %s", exc)
            return _reconnect_needed(return_to)
        if not email:
            return _error(
                "Couldn't confirm which Google account is connected to YouTube "
                "(Google didn't answer). "
                f"<a href='/auth/youtube/quota/start?return_to={_safe_return(return_to)}'>"
                "Try again</a>"
            )
        callback = _callback_uri(request, "/auth/youtube/quota/callback")
        flow = _yu.make_publisher_flow(callback, SETUP_SCOPES)
        extra = {"login_hint": email} if email else {}
        # Online access: no refresh token is issued for the Cloud scopes.
        url, state = flow.authorization_url(
            access_type="online", prompt="consent", **extra
        )
        _gc()
        _states[state] = {
            "kind": "quota",
            "storage": storage,
            "callback": callback,
            "email": email,
            "return_to": _safe_return(return_to),
            "created_at": time.time(),
        }
        return RedirectResponse(url, status_code=302)

    @router.get("/auth/youtube/quota/callback")
    def quota_callback(
        code: str | None = None, state: str | None = None, error: str | None = None
    ) -> Response:
        from google.auth.transport.requests import AuthorizedSession

        entry = _states.pop(state or "", None)
        if error:
            return _error(f"Google reported: {html.escape(error)}")
        if not entry or entry["kind"] != "quota" or not code:
            return _error("This attempt expired. Start again from the dashboard.")
        flow = _yu.make_publisher_flow(entry["callback"], SETUP_SCOPES)
        try:
            flow.fetch_token(code=code)
        except Exception as exc:  # noqa: BLE001
            logger.error("YOUTUBE_QUOTA: token exchange failed: %s", exc)
            return _error(f"Token exchange failed: {html.escape(str(exc))}")
        setup_creds = flow.credentials
        # Google's granular consent shows each Cloud permission as its own
        # checkbox, unticked by default (seen live 2026-11-27). Without both,
        # project creation or enabling fails later with a confusing 403.
        granted = _granted_scopes(flow)
        missing = [s for s in _CLOUD_SCOPES if s not in granted]
        if missing:
            return _error(
                "Google didn't grant both permissions soccer-cam needs. On "
                "Google's screen, tick <strong>both</strong> boxes (or "
                '<em>Select all</em>): "Manage your Cloud Platform projects" '
                'and "Manage your Google API service configuration". '
                f"<a href='/auth/youtube/quota/start?return_to={entry['return_to']}'>"
                "Try again</a>"
            )
        email = _id_token_email(setup_creds)
        if not email or email != entry["email"]:
            return _error(
                "You picked a different Google account than the one connected "
                "to YouTube. The quota project has to belong to the same "
                f"account. <a href='/auth/youtube/quota/start?return_to="
                f"{entry['return_to']}'>Try again</a>"
            )
        prov = _job["provisioner"]
        if prov and not prov.status["done"]:
            return RedirectResponse(
                f"/auth/youtube/quota?return_to={entry['return_to']}", status_code=303
            )

        storage = entry["storage"]
        yt_dir = storage / "youtube"

        def _on_ready(project_id: str, has_channel: bool) -> None:
            write_quota_project(yt_dir, project_id, email, has_channel)
            clear_auth_needed(str(storage), "youtube")

        try:
            yt_session = AuthorizedSession(
                _raw_youtube_credentials(yt_dir / "token.json")
            )
        except Exception as exc:  # noqa: BLE001 -- RefreshError, network
            logger.warning("YOUTUBE_QUOTA: YouTube token unusable: %s", exc)
            return _reconnect_needed(entry["return_to"])
        prov = _qp.QuotaProjectProvisioner(
            AuthorizedSession(setup_creds), yt_session, _on_ready
        )
        _job["provisioner"] = prov
        prov.start()
        return RedirectResponse(
            f"/auth/youtube/quota?return_to={entry['return_to']}", status_code=303
        )

    @router.get("/auth/youtube/quota/status")
    def quota_status(request: Request) -> JSONResponse:
        prov = _job["provisioner"]
        if prov is not None:
            return JSONResponse(prov.status)
        conn = youtube_connection(_storage_for(request))
        if conn["quota_project"]:
            return JSONResponse(
                {
                    "phase": "ready",
                    "done": True,
                    "project_id": conn["quota_project"],
                    "message": f"Using project {conn['quota_project']}.",
                }
            )
        return JSONResponse({"phase": "idle", "done": True, "message": ""})

    @router.get("/auth/youtube/quota", response_class=HTMLResponse)
    def quota_page(request: Request, return_to: str = "/") -> HTMLResponse:
        back = _safe_return(return_to)
        conn = youtube_connection(_storage_for(request))
        prov = _job["provisioner"]
        if not conn["connected"]:
            body = (
                "<p>Connect a YouTube account first.</p>"
                f'<p><a class="btn" href="/auth/youtube/connect?return_to={back}">'
                "Connect YouTube</a></p>"
            )
            js = ""
        elif prov is not None and (
            not prov.status["done"] or not conn["quota_project"]
        ):
            body = (
                '<p id="quota-status" class="muted">Starting&hellip;</p>'
                '<p id="quota-terms" hidden>'
                f'<a class="btn" href="{TERMS_URL}" target="_blank" rel="noopener">'
                "Open Google Cloud to accept the Terms</a></p>"
                f'<p id="quota-done" hidden><a class="btn" href="{back}">Continue</a>'
                f' <a class="btn btn-ghost" href="/auth/youtube/quota/start?return_to={back}">'
                "Try again</a></p>"
            )
            js = _STATUS_JS
        elif conn["quota_project"]:
            body = (
                '<p class="ok">Uploads are billed to your own Google Cloud '
                f"project <code>{html.escape(conn['quota_project'])}</code>.</p>"
                + (NO_CHANNEL_HTML if conn["no_channel"] else "")
                + ""
                f'<p><a class="btn" href="{back}">Continue</a></p>'
            )
            js = ""
        else:
            body = (
                "<p>YouTube limits how much each app can upload per day. So that "
                "your uploads never compete with anyone else's, soccer-cam "
                "keeps them on a free Google Cloud project in <em>your</em> "
                "account. It creates and configures it for you.</p>"
                "<p class='muted'>Google will ask once to let soccer-cam manage "
                "your Cloud projects. Soccer-cam uses that only for this step and "
                "never saves it; you can remove it any time under Third-party "
                "access in your Google Account. A brand-new account may also be "
                "asked to accept Google Cloud's Terms of Service.</p>"
                f'<p><a class="btn" href="/auth/youtube/quota/start?return_to={back}">'
                "Set up YouTube quota</a> "
                f'<a class="btn btn-ghost" href="{back}">Not now</a></p>'
            )
            js = ""
        return HTMLResponse(
            _chrome.notice_page("YouTube quota", "YouTube quota", body, extra_js=js)
        )

    return router
