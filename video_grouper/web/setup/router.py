"""FastAPI router for the onboarding wizard."""

from __future__ import annotations

import html
import logging
import os
import string
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Query, Request
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
    SetupConfig,
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
body { font-family: system-ui, sans-serif; max-width: 600px; margin: 2.5em auto; padding: 0 1em; color: #222; }
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
.path-list { display: flex; flex-direction: column; gap: 0.25rem; max-height: 280px; overflow-y: auto; }
.path-chip { text-align: left; padding: 0.4rem 0.6rem; background: white; border: 1px solid #e5e7eb; border-radius: 4px; cursor: pointer; font-family: ui-monospace, Consolas, monospace; font-size: 0.85rem; color: #1f2937; }
.path-chip:hover { background: #eff6ff; border-color: #2563eb; }
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

_STORAGE_PICKER_JS = """
<script>
(function () {
  const browseBtn = document.getElementById("browse-btn");
  const modal = document.getElementById("browse-modal");
  const input = document.getElementById("storage-input");
  if (!browseBtn || !modal || !input) return;

  function showModal(html) { modal.style.display = "block"; modal.innerHTML = html; }
  function closeModal() { modal.style.display = "none"; modal.innerHTML = ""; }

  // Try a native PyQt6 dialog first (signaled to the running tray);
  // if the tray doesn't respond, fall back to in-page server-side
  // browsing. Linux/Docker installs have no tray and skip step 1.
  browseBtn.addEventListener("click", async () => {
    showModal('<div class="muted">Opening folder picker…</div>');
    let id = null;
    try {
      const resp = await fetch("/setup/storage/request-pick", { method: "POST" });
      if (resp.ok) { id = (await resp.json()).id; }
    } catch (e) { /* tray-pick not supported; fall through */ }
    if (id) {
      pollNative(id);
    } else {
      openServerBrowser("");
    }
  });

  async function pollNative(id) {
    let tries = 0;
    const tick = async () => {
      tries++;
      try {
        const r = await fetch(
          "/setup/storage/pick-result?id=" + encodeURIComponent(id));
        if (r.status === 200) {
          const data = await r.json();
          if (data.path) { input.value = data.path; closeModal(); return; }
          if (data.cancelled) {
            showModal(
              '<div class="muted">Cancelled. ' +
              '<a href="#" id="server-fallback">Browse server-side instead</a>' +
              "</div>");
            document.getElementById("server-fallback").addEventListener(
              "click", (e) => { e.preventDefault(); openServerBrowser(""); });
            return;
          }
        }
      } catch (e) { /* ignore, keep polling */ }
      // 60 ticks * 500ms = 30s of patience for the native dialog.
      if (tries < 60) {
        setTimeout(tick, 500);
      } else {
        showModal('<div class="muted">No response from tray; loading server-side browser…</div>');
        setTimeout(() => openServerBrowser(""), 600);
      }
    };
    setTimeout(tick, 250);
  }

  function openServerBrowser(at) {
    showModal('<div class="muted">Loading…</div>');
    fetch("/setup/storage/browse?at=" + encodeURIComponent(at || ""))
      .then((r) => r.text())
      .then((html) => {
        modal.innerHTML = html;
        modal.querySelectorAll(".path-chip").forEach((b) => {
          b.addEventListener("click", (e) => {
            openServerBrowser(e.currentTarget.dataset.path);
          });
        });
        const use = modal.querySelector("#use-this-path");
        if (use) {
          use.addEventListener("click", (e) => {
            input.value = e.currentTarget.dataset.path;
            closeModal();
          });
        }
      })
      .catch((err) => {
        modal.innerHTML = '<div class="err">Browse failed: ' + err + "</div>";
      });
  }
})();
</script>
"""

_STEPS = ("welcome", "storage", "camera", "summary")
_STEP_LABELS = {
    "welcome": "Welcome",
    "storage": "Storage",
    "camera": "Camera",
    "summary": "Review & save",
}


def _default_storage_path() -> str:
    """Return an OS-appropriate default for the storage path field.

    Windows: a path under %ProgramData% the LocalSystem service can
    write to without bumping into Program Files' admin-only ACLs.
    Other platforms (Linux/Docker mostly) keep the historical
    /shared_data convention since that's the documented in-container
    mount point.
    """
    if os.name == "nt":
        program_data = os.environ.get("ProgramData", r"C:\ProgramData")
        return os.path.join(program_data, "VideoGrouper", "storage")
    return "/shared_data"


def _list_drives() -> list[str]:
    """Enumerate drive letters that currently have mounted volumes."""
    drives = []
    for letter in string.ascii_uppercase:
        path = f"{letter}:\\"
        if os.path.exists(path):
            drives.append(path)
    return drives


def _picker_ipc_dir() -> Path:
    """Shared dir for native-picker request/response files.

    Lives under ProgramData so service (LocalSystem) and tray (user
    session) can both read+write without permissions wrangling.
    """
    if os.name == "nt":
        program_data = os.environ.get("ProgramData", r"C:\ProgramData")
        return Path(program_data) / "VideoGrouper" / "picker"
    return Path("/tmp/videogrouper-picker")


def _list_subdirs(path: str) -> list[str]:
    """Return immediate subdirectories under ``path``, sorted, hidden filtered."""
    try:
        entries = os.listdir(path)
    except (PermissionError, OSError):
        return []
    subdirs = []
    for name in entries:
        if name.startswith(".") or name.startswith("$"):
            continue
        full = os.path.join(path, name)
        try:
            if os.path.isdir(full):
                subdirs.append(name)
        except OSError:
            continue
    subdirs.sort(key=str.lower)
    return subdirs


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
        path_val = html.escape(state.storage_path or _default_storage_path())
        is_windows = os.name == "nt"
        # On Windows, prefer the native tray-mediated picker (QFileDialog).
        # On Linux/Docker, the server-side browser is the only option since
        # no PyQt tray is running. The page-level JS picks the right one.
        if is_windows:
            help_html = (
                '<span class="muted">Where game videos and per-game state are saved. '
                "Pre-filled with a path the service can write to without "
                "elevating; pick a different drive (e.g., a larger one) "
                "if you want videos elsewhere.</span>"
            )
        else:
            help_html = (
                '<span class="muted">Where game videos and per-game state are saved. '
                "On a Linux/Docker host this is the path inside the container "
                "(typically <code>/app/shared_data</code>).</span>"
            )
        body = (
            '<form method="post" action="/setup/storage" id="storage-form">'
            "<label>Storage path"
            f'<input name="storage_path" id="storage-input" type="text" '
            f'value="{path_val}" required spellcheck="false" '
            'style="font-family: ui-monospace, Consolas, monospace;">'
            f"{help_html}"
            "</label>"
            '<div class="row">'
            '<button type="button" class="btn btn-ghost" id="browse-btn">'
            "Browse…</button>"
            "</div>"
            '<div id="browse-modal" style="display:none; margin-top:0.75rem; '
            "padding:0.75rem; border:1px solid #cbd5e1; border-radius:6px; "
            'background:#f8fafc;"></div>'
            '<div class="row">'
            '<a class="btn-ghost btn" href="/setup/welcome">Back</a>'
            '<button class="btn" type="submit">Next</button>'
            "</div></form>" + _STORAGE_PICKER_JS
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

    @router.get("/storage/browse", response_class=HTMLResponse)
    def storage_browse(at: Optional[str] = Query(None)) -> HTMLResponse:
        """Render a directory listing fragment for the in-page browse modal.

        ``at`` is the directory to list. If empty/missing, list the
        machine's available drive letters (Windows) or "/" (Unix).
        Returns HTML, not a full page — meant to be loaded into the
        modal div.
        """
        if not at:
            # Top-level: drives on Windows, / on Unix.
            if os.name == "nt":
                items = _list_drives()
                title = "Drives"
            else:
                items = ["/"]
                title = "Filesystem"
            buttons = "".join(
                f'<button type="button" class="path-chip" '
                f'data-path="{html.escape(p)}">{html.escape(p)}</button>'
                for p in items
            )
            return HTMLResponse(
                f'<div class="muted">{title}</div>'
                f'<div class="path-list">{buttons}</div>'
            )

        # Reject paths that don't exist; the input could be anything.
        path_obj = Path(at)
        if not path_obj.is_dir():
            return HTMLResponse(
                f'<div class="err">Not a directory: {html.escape(str(path_obj))}</div>'
            )

        # Parent link (unless we're at a drive root).
        parent_html = ""
        parent = path_obj.parent
        # On Windows, Path("C:\\").parent == Path("C:\\"), so we'd loop;
        # detect that and offer "back to drives" instead.
        if str(parent) == str(path_obj):
            parent_html = (
                '<button type="button" class="path-chip" data-path="">← Drives</button>'
            )
        else:
            parent_html = (
                '<button type="button" class="path-chip" '
                f'data-path="{html.escape(str(parent))}">'
                f"← {html.escape(parent.name or str(parent))}</button>"
            )

        subdirs = _list_subdirs(str(path_obj))
        subdir_buttons = "".join(
            f'<button type="button" class="path-chip" '
            f'data-path="{html.escape(str(path_obj / name))}">'
            f"{html.escape(name)}/</button>"
            for name in subdirs
        )
        if not subdirs:
            subdir_buttons = '<span class="muted">(no subdirectories)</span>'

        return HTMLResponse(
            f'<div class="row" style="justify-content:space-between;">'
            f"<div>{parent_html}</div>"
            f'<button type="button" class="btn" id="use-this-path" '
            f'data-path="{html.escape(str(path_obj))}">Use this folder</button>'
            f"</div>"
            f'<div class="muted" style="margin-top:0.5rem; '
            f'font-family: ui-monospace, Consolas, monospace;">'
            f"{html.escape(str(path_obj))}</div>"
            f'<div class="path-list" style="margin-top:0.5rem;">'
            f"{subdir_buttons}</div>"
        )

    @router.post("/storage/request-pick")
    def storage_request_pick() -> dict:
        """Ask the running tray to show a native folder-picker dialog.

        Drops a request file in the picker IPC dir; the tray polls for
        it, shows QFileDialog, and writes the response file. Returns
        404 on non-Windows or when the IPC dir isn't writable so the
        wizard JS can fall back to the in-page server-side browser.
        """
        if os.name != "nt":
            raise HTTPException(status_code=404, detail="native picker not available")
        ipc_dir = _picker_ipc_dir()
        try:
            ipc_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(
                status_code=503, detail=f"picker IPC dir unwritable: {exc}"
            )
        # Clean stale responses from prior runs to keep the protocol simple.
        for stale in ipc_dir.glob("response_*.json"):
            try:
                stale.unlink()
            except OSError:
                pass

        import json
        import secrets
        import time

        req_id = secrets.token_hex(8)
        request_file = ipc_dir / "request.json"
        request_file.write_text(
            json.dumps({"id": req_id, "ts": time.time()}), encoding="utf-8"
        )
        return {"id": req_id}

    @router.get("/storage/pick-result")
    def storage_pick_result(id: str = Query(...)) -> dict:
        """Poll for the tray's response to a native folder-picker request.

        Returns ``{path: "..."}`` once the tray has written the response,
        ``{cancelled: true}`` if the user dismissed the dialog, or 204
        while still waiting.
        """
        ipc_dir = _picker_ipc_dir()
        response_file = ipc_dir / f"response_{id}.json"
        if not response_file.exists():
            from fastapi.responses import Response

            return Response(status_code=204)

        import json

        try:
            data = json.loads(response_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"cancelled": True}
        finally:
            try:
                response_file.unlink()
            except OSError:
                pass
        return data

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
            "SETUP": SetupConfig(onboarding_completed=True).model_dump(),
        },
        by_alias=True,
        by_name=True,
    )


# Optional helper used by the dashboard to detect "no config yet" and
# redirect to the wizard. Kept here so the auth_server doesn't grow a
# new responsibility.
def needs_setup(config_path: Optional[Path]) -> bool:
    return config_path is None or not config_path.exists()
