"""FastAPI router for the onboarding wizard."""

from __future__ import annotations

import html
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from video_grouper.utils.config import (
    AppConfig,
    AutocamConfig,
    CameraConfig,
    CloudSyncConfig,
    Config,
    LoggingConfig,
    NtfyConfig,
    PlayMetricsConfig,
    ProcessingConfig,
    RecordingConfig,
    StorageConfig,
    TeamSnapConfig,
    TTTConfig,
    YouTubeConfig,
    save_config,
)
from video_grouper.web.setup.state import (
    cookie_name,
    discard,
    get,
    get_or_create,
)

logger = logging.getLogger(__name__)


_PAGE_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Soccer-Cam setup &mdash; __TITLE__</title>
<style>
body { font-family: system-ui, sans-serif; max-width: 540px; margin: 2.5em auto; padding: 0 1em; color: #222; }
h1 { font-size: 1.4rem; margin-bottom: 0.25rem; }
p.lede { color: #6b7280; margin-top: 0; margin-bottom: 1.25rem; }
form { display: flex; flex-direction: column; gap: 0.75rem; }
label { display: flex; flex-direction: column; gap: 0.25rem; font-size: 0.9rem; color: #475569; }
input, select { padding: 0.5rem 0.6rem; font: inherit; border: 1px solid #cbd5e1; border-radius: 4px; }
.row { display: flex; gap: 0.5rem; align-items: center; }
.btn { padding: 0.55rem 1rem; background: #2563eb; color: white !important; border: 0; border-radius: 4px; font-weight: 600; cursor: pointer; text-decoration: none; }
.btn:hover { background: #1d4ed8; }
.btn-ghost { background: transparent; color: #2563eb !important; border: 1px solid #2563eb; }
.steps { display: flex; gap: 0.4rem; margin-bottom: 1.25rem; font-size: 0.8rem; color: #94a3b8; }
.steps .step.now { color: #2563eb; font-weight: 600; }
.summary { padding: 1rem 1.25rem; border: 1px solid #e5e7eb; border-radius: 6px; background: #f9fafb; }
.summary dt { font-weight: 600; color: #475569; margin-top: 0.5rem; }
.summary dd { margin: 0 0 0.4rem; }
.muted { color: #6b7280; font-size: 0.85rem; }
.err { padding: 0.5rem 0.75rem; background: #fee2e2; color: #7f1d1d; border-radius: 4px; }
</style>
</head>
<body>
<__STEPS__>
<h1>__TITLE__</h1>
<p class="lede">__LEDE__</p>
__BODY__
</body>
</html>
"""

_STEPS = ("welcome", "storage", "camera", "summary")
_STEP_LABELS = {
    "welcome": "Welcome",
    "storage": "Storage",
    "camera": "Camera",
    "summary": "Review & save",
}


def _render_steps(active: str) -> str:
    parts = []
    for step in _STEPS:
        cls = "step now" if step == active else "step"
        parts.append(f'<span class="{cls}">{html.escape(_STEP_LABELS[step])}</span>')
    return '<div class="steps">' + " &rsaquo; ".join(parts) + "</div>"


def _page(active: str, title: str, lede: str, body: str) -> str:
    return (
        _PAGE_TEMPLATE.replace("<__STEPS__>", _render_steps(active))
        .replace("__TITLE__", title)
        .replace("__LEDE__", lede)
        .replace("__BODY__", body)
    )


def _redirect_with_cookie(target: str, token: str) -> RedirectResponse:
    resp = RedirectResponse(url=target, status_code=303)
    resp.set_cookie(
        key=cookie_name(),
        value=token,
        max_age=3600,  # 1 hour is plenty for any onboarding session
        httponly=True,
        samesite="lax",
    )
    return resp


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def build_router(config_path: Path) -> APIRouter:
    """Build the wizard router. Persists to ``config_path`` on submit."""
    router = APIRouter(prefix="/setup")

    @router.get("/", response_class=HTMLResponse)
    def setup_root(request: Request) -> RedirectResponse:
        return RedirectResponse(url="/setup/welcome", status_code=303)

    @router.get("/welcome", response_class=HTMLResponse)
    def welcome(request: Request) -> HTMLResponse:
        token, _ = get_or_create(request.cookies.get(cookie_name()))
        body = (
            "<p>This wizard walks through the minimum config to get "
            "Soccer-Cam recording: where to store videos and one camera "
            "to poll. After you finish, integrations (YouTube, NTFY, "
            "PlayMetrics, TeamSnap) and any advanced settings live on "
            'the <a href="/config">configuration page</a>.</p>'
            '<p><a class="btn" href="/setup/storage">Get started</a></p>'
        )
        resp = HTMLResponse(
            _page(
                "welcome",
                "Welcome to Soccer-Cam",
                "First-time setup.",
                body,
            )
        )
        resp.set_cookie(
            key=cookie_name(),
            value=token,
            max_age=3600,
            httponly=True,
            samesite="lax",
        )
        return resp

    @router.get("/storage", response_class=HTMLResponse)
    def storage_get(request: Request) -> HTMLResponse:
        token, state = get_or_create(request.cookies.get(cookie_name()))
        path_val = html.escape(state.storage_path or "/shared_data")
        body = (
            '<form method="post" action="/setup/storage">'
            "<label>Storage path"
            f'<input name="storage_path" type="text" value="{path_val}" required>'
            '<span class="muted">Where game videos and per-game state are saved. '
            "On a Linux/Docker host this is the path inside the container "
            "(typically <code>/app/shared_data</code>); on Windows, an absolute "
            "path like <code>C:/SoccerCam</code>.</span>"
            "</label>"
            '<div class="row">'
            '<a class="btn-ghost btn" href="/setup/welcome">Back</a>'
            '<button class="btn" type="submit">Next</button>'
            "</div></form>"
        )
        resp = HTMLResponse(_page("storage", "Storage", "Where do videos go?", body))
        resp.set_cookie(
            key=cookie_name(),
            value=token,
            max_age=3600,
            httponly=True,
            samesite="lax",
        )
        return resp

    @router.post("/storage", response_class=HTMLResponse)
    def storage_post(
        request: Request, storage_path: str = Form(...)
    ) -> RedirectResponse:
        token = request.cookies.get(cookie_name())
        state = get(token)
        if state is None:
            return RedirectResponse(url="/setup/welcome", status_code=303)
        state.storage_path = storage_path.strip()
        return _redirect_with_cookie("/setup/camera", token)

    @router.get("/camera", response_class=HTMLResponse)
    def camera_get(request: Request) -> HTMLResponse:
        token, state = get_or_create(request.cookies.get(cookie_name()))
        # No password echo on render (sensitive)
        body = (
            '<form method="post" action="/setup/camera">'
            "<label>Camera type"
            '<select name="camera_type" required>'
            f'<option value="dahua" {"selected" if state.camera_type == "dahua" else ""}>Dahua</option>'
            f'<option value="reolink" {"selected" if state.camera_type == "reolink" else ""}>Reolink</option>'
            "</select></label>"
            "<label>Camera name"
            f'<input name="camera_name" type="text" value="{html.escape(state.camera_name)}" required>'
            '<span class="muted">Used as the [CAMERA.&lt;name&gt;] section in '
            "config.ini. Pick anything (e.g. <code>field</code>).</span></label>"
            "<label>IP address"
            f'<input name="camera_ip" type="text" value="{html.escape(state.camera_ip)}" placeholder="192.168.1.100" required>'
            "</label>"
            "<label>Username"
            f'<input name="camera_username" type="text" value="{html.escape(state.camera_username)}" required></label>'
            "<label>Password"
            '<input name="camera_password" type="password" value="" placeholder="(set on save)" required>'
            "</label>"
            '<div class="row">'
            '<a class="btn-ghost btn" href="/setup/storage">Back</a>'
            '<button class="btn" type="submit">Next</button>'
            "</div></form>"
        )
        resp = HTMLResponse(
            _page(
                "camera",
                "Camera",
                "How do we reach your camera?",
                body,
            )
        )
        resp.set_cookie(
            key=cookie_name(),
            value=token,
            max_age=3600,
            httponly=True,
            samesite="lax",
        )
        return resp

    @router.post("/camera", response_class=HTMLResponse)
    def camera_post(
        request: Request,
        camera_type: str = Form(...),
        camera_name: str = Form(...),
        camera_ip: str = Form(...),
        camera_username: str = Form(...),
        camera_password: str = Form(...),
    ) -> RedirectResponse:
        token = request.cookies.get(cookie_name())
        state = get(token)
        if state is None:
            return RedirectResponse(url="/setup/welcome", status_code=303)
        state.camera_type = camera_type
        state.camera_name = camera_name.strip() or "default"
        state.camera_ip = camera_ip.strip()
        state.camera_username = camera_username.strip() or "admin"
        state.camera_password = camera_password
        return _redirect_with_cookie("/setup/summary", token)

    @router.get("/summary", response_class=HTMLResponse)
    def summary_get(request: Request) -> HTMLResponse:
        token = request.cookies.get(cookie_name())
        state = get(token)
        if state is None or not state.is_complete:
            return RedirectResponse(url="/setup/welcome", status_code=303)
        body = (
            '<div class="summary"><dl>'
            f"<dt>Storage path</dt><dd><code>{html.escape(state.storage_path)}</code></dd>"
            f"<dt>Camera</dt><dd>{html.escape(state.camera_type)} <code>{html.escape(state.camera_name)}</code> "
            f"@ <code>{html.escape(state.camera_ip)}</code> (user <code>{html.escape(state.camera_username)}</code>)</dd>"
            "</dl></div>"
            f'<form method="post" action="/setup/finish">'
            f'<p class="muted">Saving will write <code>{html.escape(str(config_path))}</code> '
            "with these values plus safe defaults for everything else. After save, "
            'visit <a href="/config">/config</a> to enable YouTube, NTFY, etc.</p>'
            '<div class="row">'
            '<a class="btn-ghost btn" href="/setup/camera">Back</a>'
            '<button class="btn" type="submit">Save configuration</button>'
            "</div></form>"
        )
        return HTMLResponse(_page("summary", "Review & save", "Almost done.", body))

    @router.post("/finish", response_class=HTMLResponse)
    def finish(request: Request) -> RedirectResponse:
        token = request.cookies.get(cookie_name())
        state = get(token)
        if state is None or not state.is_complete:
            raise HTTPException(
                status_code=400,
                detail="Wizard state missing or incomplete; restart the wizard.",
            )

        config = _build_config(state)
        try:
            save_config(config, config_path)
        except OSError as exc:
            logger.error("SETUP: save failed: %s", exc)
            raise HTTPException(
                status_code=500,
                detail=f"Could not write {config_path}: {exc}",
            )

        discard(token)
        # Land on /config so the user can immediately tweak integrations.
        resp = RedirectResponse(url="/config?saved=1", status_code=303)
        resp.delete_cookie(cookie_name())
        return resp

    return router


def _build_config(state) -> Config:
    """Materialize wizard state into a complete Config with safe defaults."""
    return Config.model_validate(
        {
            "cameras": [
                CameraConfig(
                    name=state.camera_name,
                    type=state.camera_type,
                    device_ip=state.camera_ip,
                    username=state.camera_username,
                    password=state.camera_password,
                ).model_dump()
            ],
            "STORAGE": StorageConfig(path=state.storage_path).model_dump(),
            "RECORDING": RecordingConfig().model_dump(),
            "PROCESSING": ProcessingConfig().model_dump(),
            "LOGGING": LoggingConfig().model_dump(),
            "APP": AppConfig().model_dump(),
            "TEAMSNAP": TeamSnapConfig().model_dump(),
            "PLAYMETRICS": PlayMetricsConfig().model_dump(),
            "NTFY": NtfyConfig().model_dump(),
            "YOUTUBE": YouTubeConfig().model_dump(),
            "AUTOCAM": AutocamConfig().model_dump(),
            "CLOUD_SYNC": CloudSyncConfig().model_dump(),
            "TTT": TTTConfig().model_dump(),
        },
        by_alias=True,
        by_name=True,
    )


# Optional helper used by the dashboard to detect "no config yet" and
# redirect to the wizard. Kept here so the auth_server doesn't grow a
# new responsibility.
def needs_setup(config_path: Optional[Path]) -> bool:
    return config_path is None or not config_path.exists()
