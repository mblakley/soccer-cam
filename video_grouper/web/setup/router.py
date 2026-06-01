"""FastAPI router for the onboarding wizard."""

from __future__ import annotations

import html
import json
import logging
import os
import string
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from video_grouper.pipeline.presets import apply_preset
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
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@500;600;700&family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {
  --bg-base: #0a0b0f;
  --bg-surface: #13141a;
  --bg-elev: #181a22;
  --bg-input: #0f1015;
  --rule: #2a2c34;
  --rule-strong: #3b3e48;
  --text: #e6e7ec;
  --text-mute: #94969f;
  --text-faint: #5e616b;
  --accent: #fb923c;
  --accent-glow: rgba(251,146,60,0.16);
  --signal-on: #22c55e;
  --signal-bad: #f43f5e;
  --display: 'Barlow Condensed', 'Bebas Neue', sans-serif;
  --body: 'IBM Plex Sans', system-ui, sans-serif;
  --mono: 'IBM Plex Mono', ui-monospace, monospace;
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body {
  margin: 0;
  font-family: var(--body);
  font-size: 14px;
  line-height: 1.55;
  color: var(--text);
  background:
    radial-gradient(ellipse 80% 50% at 50% -20%, rgba(251,146,60,0.06), transparent 60%),
    radial-gradient(ellipse 60% 40% at 100% 100%, rgba(34,197,94,0.04), transparent 60%),
    var(--bg-base);
  background-attachment: fixed;
  position: relative;
}
body::before {
  content: ""; position: fixed; inset: 0;
  background-image: repeating-linear-gradient(
    0deg, transparent 0, transparent 2px, rgba(255,255,255,0.012) 2px, rgba(255,255,255,0.012) 3px);
  pointer-events: none; z-index: 1;
}
.topbar { position: relative; z-index: 2; border-bottom: 1px solid var(--rule); background: rgba(10,11,15,0.72); backdrop-filter: blur(8px); }
.topbar-inner { max-width: 720px; margin: 0 auto; padding: 14px 28px; display: flex; align-items: center; justify-content: space-between; }
.brand { font-family: var(--display); font-weight: 700; letter-spacing: 0.18em; font-size: 18px; text-transform: uppercase; }
.brand .dot { color: var(--accent); }
.crumb { font-family: var(--mono); font-size: 11px; letter-spacing: 0.16em; text-transform: uppercase; color: var(--text-mute); }
.shell {
  position: relative; z-index: 2;
  max-width: 720px; margin: 0 auto;
  padding: 32px 28px 80px;
  animation: page-in 320ms ease-out both;
}
@keyframes page-in { from { opacity: 0; transform: translateY(6px); } }
.steps { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 28px; font-family: var(--mono); font-size: 10px; letter-spacing: 0.16em; text-transform: uppercase; color: var(--text-faint); }
.steps .step { padding: 6px 10px; border: 1px solid var(--rule); }
.steps .step.now { color: var(--accent); border-color: var(--accent); }
.headline { font-family: var(--display); font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; font-size: clamp(32px, 5vw, 48px); line-height: 0.95; margin: 0 0 8px; }
.lede { color: var(--text-mute); max-width: 56ch; margin: 0 0 24px; }
.lede code { font-family: var(--mono); font-size: 12px; background: var(--bg-elev); padding: 1px 6px; border: 1px solid var(--rule); }
form { display: flex; flex-direction: column; gap: 18px; }
label { display: flex; flex-direction: column; gap: 6px; font-family: var(--mono); font-size: 11px; letter-spacing: 0.12em; text-transform: uppercase; color: var(--text-mute); }
input[type="text"], input[type="password"], input[type="number"], select {
  width: 100%; font: inherit; font-family: var(--mono); font-size: 13px;
  color: var(--text); background: var(--bg-input);
  border: 1px solid var(--rule); padding: 10px 12px; border-radius: 0;
  outline: none; transition: border-color 120ms ease, box-shadow 120ms ease;
}
input[type="text"]:focus, input[type="password"]:focus, input[type="number"]:focus, select:focus {
  border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-glow);
}
input::placeholder { color: var(--text-faint); font-style: italic; }
.row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
.btn { font-family: var(--mono); font-size: 11px; font-weight: 600; letter-spacing: 0.18em; text-transform: uppercase; padding: 11px 22px; background: var(--accent); color: #1a0e02 !important; border: 0; cursor: pointer; text-decoration: none; transition: filter 120ms ease, transform 120ms ease; }
.btn:hover { filter: brightness(1.08); }
.btn:active { transform: translateY(1px); }
.btn-ghost { background: transparent; color: var(--text-mute) !important; border: 1px solid var(--rule); }
.btn-ghost:hover { color: var(--text); border-color: var(--rule-strong); }
.summary { padding: 18px 22px; border: 1px solid var(--rule); background: var(--bg-elev); }
.summary dt { font-family: var(--mono); font-size: 10px; font-weight: 600; letter-spacing: 0.16em; text-transform: uppercase; color: var(--text-faint); margin-top: 10px; }
.summary dt:first-child { margin-top: 0; }
.summary dd { margin: 0 0 6px; font-family: var(--mono); font-size: 13px; }
.summary code { color: var(--accent); }
.muted { color: var(--text-mute); font-family: var(--mono); font-size: 12px; }
.err { padding: 10px 14px; background: rgba(244,63,94,0.06); color: var(--signal-bad); border: 1px solid rgba(244,63,94,0.4); font-family: var(--mono); font-size: 12px; }
.path-list { display: flex; flex-direction: column; gap: 4px; max-height: 280px; overflow-y: auto; }
.path-chip { text-align: left; padding: 8px 12px; background: var(--bg-input); border: 1px solid var(--rule); cursor: pointer; font-family: var(--mono); font-size: 12px; color: var(--text); }
.path-chip:hover { background: var(--bg-elev); border-color: var(--accent); color: var(--accent); }
</style>
</head>
<body>
<header class="topbar">
  <div class="topbar-inner">
    <div class="brand">SOCCER<span class="dot">·</span>CAM</div>
    <div class="crumb">Setup</div>
  </div>
</header>
<div class="shell">
<__STEPS__>
<h1 class="headline">__TITLE__</h1>
<p class="lede">__LEDE__</p>
__BODY__
</div>
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

  browseBtn.addEventListener("click", () => openBrowser(input.value || ""));

  function openBrowser(at) {
    showModal('<div class="muted">Loading…</div>');
    fetch("/setup/storage/browse?at=" + encodeURIComponent(at || ""))
      .then((r) => r.text())
      .then((html) => {
        modal.innerHTML = html;
        modal.querySelectorAll(".path-chip").forEach((b) => {
          b.addEventListener("click", (e) => {
            openBrowser(e.currentTarget.dataset.path);
          });
        });
        const useBtn = modal.querySelector("#use-this-path");
        if (useBtn) {
          useBtn.addEventListener("click", (e) => {
            input.value = e.currentTarget.dataset.path;
            closeModal();
          });
        }
        const goForm = modal.querySelector("#browse-go-form");
        if (goForm) {
          goForm.addEventListener("submit", (e) => {
            e.preventDefault();
            const goInput = modal.querySelector("#browse-go-input");
            openBrowser(goInput.value.trim());
          });
        }
        const closeBtn = modal.querySelector("#browse-close");
        if (closeBtn) {
          closeBtn.addEventListener("click", closeModal);
        }
      })
      .catch((err) => {
        modal.innerHTML = '<div class="err">Browse failed: ' + err + "</div>";
      });
  }
})();
</script>
"""

_CAMERA_TEST_JS = """
<script>
(function () {
  const btn = document.getElementById("test-btn");
  const out = document.getElementById("test-result");
  if (!btn || !out) return;
  btn.addEventListener("click", async () => {
    const form = new FormData();
    form.set("camera_type", document.getElementById("camera-type").value);
    form.set("camera_ip", document.getElementById("camera-ip").value);
    form.set("camera_username", document.getElementById("camera-username").value);
    form.set("camera_password", document.getElementById("camera-password").value);
    out.textContent = "Testing…";
    out.className = "muted";
    try {
      const r = await fetch("/setup/camera/test", { method: "POST", body: form });
      const data = await r.json();
      out.textContent = (data.ok ? "✓ " : "✗ ") + (data.message || "");
      out.className = data.ok ? "muted" : "err";
      out.style.color = data.ok ? "#15803d" : "#7f1d1d";
    } catch (e) {
      out.textContent = "✗ " + e;
      out.style.color = "#7f1d1d";
    }
  });
})();
</script>
"""

_STEPS = ("welcome", "storage", "camera", "youtube", "summary")
_STEP_LABELS = {
    "welcome": "Welcome",
    "storage": "Storage",
    "camera": "Camera",
    "youtube": "YouTube",
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


def _render_youtube_body(has_secret: bool, has_token: bool) -> str:
    """Render the wizard body for the YouTube step.

    Three states:
      - No client_secret yet: show GCP setup instructions + file upload
      - client_secret uploaded, not authorized yet: show "Authorize" button
      - Both done: show "Authorized" status + "Continue" button

    Skip is always available — YouTube is optional.
    """
    setup_steps = (
        '<ol class="muted" style="line-height:1.7;">'
        "<li>Open the "
        '<a href="https://console.cloud.google.com/" target="_blank" '
        'rel="noopener">Google Cloud Console</a> and create (or pick) '
        "a project.</li>"
        "<li>In the project, open <strong>APIs &amp; Services &rsaquo; "
        "Library</strong>, find <em>YouTube Data API v3</em>, click "
        "<strong>Enable</strong>.</li>"
        "<li>Open <strong>APIs &amp; Services &rsaquo; OAuth consent "
        "screen</strong>, configure as <em>External</em>, and add these "
        "scopes:"
        '<pre style="margin:0.4rem 0;font-size:11px;">'
        "https://www.googleapis.com/auth/youtube.upload\n"
        "https://www.googleapis.com/auth/youtube.readonly\n"
        "https://www.googleapis.com/auth/youtube"
        "</pre>"
        "Add your own Google account as a Test user.</li>"
        "<li>Open <strong>APIs &amp; Services &rsaquo; Credentials</strong>, "
        "click <strong>Create credentials &rsaquo; OAuth client ID</strong>, "
        "pick <em>Desktop app</em>, and authorize "
        "<code>http://127.0.0.1:8765/auth/youtube/callback</code> as a "
        "redirect URI.</li>"
        "<li>Download the JSON file and upload it below.</li>"
        "</ol>"
    )
    why_byo = (
        '<p class="muted">Why your own GCP project? '
        "YouTube limits API uploads to ~3 games/day per OAuth client. "
        "If every soccer-cam install shared one client they'd fight "
        "over a single quota; with your own client you get your own "
        "limit (and can request more from Google later if you need to).</p>"
    )
    upload_form = (
        '<form method="post" action="/setup/youtube/upload" '
        'enctype="multipart/form-data" '
        'style="display:flex;flex-direction:column;gap:8px;'
        'border:1px solid var(--rule);padding:14px;">'
        '<label style="font-family:var(--mono);font-size:11px;'
        "letter-spacing:0.12em;text-transform:uppercase;"
        'color:var(--text-mute);">Upload client_secret.json'
        '<input type="file" name="client_secret" accept="application/json" '
        'required style="font-family:var(--mono);"></label>'
        '<button class="btn" type="submit" style="align-self:flex-start;">'
        "Upload</button>"
        "</form>"
    )
    skip_form = (
        '<form method="post" action="/setup/youtube/skip" '
        'style="display:inline;">'
        '<button class="btn-ghost btn" type="submit">'
        "Skip &mdash; set up later</button></form>"
    )
    if not has_secret:
        body = (
            "<p>Soccer-cam can upload your finished games to <strong>your own"
            " YouTube channel</strong>. This step is optional and can also "
            'be set up later from the <a href="/">dashboard</a>.</p>'
            f"{why_byo}"
            "<h3>One-time setup</h3>"
            f"{setup_steps}"
            f"{upload_form}"
            '<div class="row" style="margin-top:18px;">'
            '<a class="btn-ghost btn" href="/setup/camera">Back</a>'
            f"{skip_form}"
            "</div>"
        )
        return body
    if not has_token:
        body = (
            "<p><span style='color:var(--signal-on);'>&#10003;</span> "
            "<code>client_secret.json</code> uploaded. Now sign into the "
            "Google account whose YouTube channel should receive the "
            "uploads.</p>"
            "<p>The browser will redirect you to Google, you'll sign in, "
            "Google will redirect you back here, and the resulting token "
            "will be saved to <code>&lt;storage&gt;/youtube/token.json</code>."
            "</p>"
            '<div class="row">'
            '<a class="btn" href="/auth/youtube/start?return_to=/setup/youtube">'
            "Authorize with YouTube</a>"
            "</div>"
            '<div class="row" style="margin-top:18px;">'
            '<a class="btn-ghost btn" href="/setup/camera">Back</a>'
            f"{skip_form}"
            "</div>"
        )
        return body
    body = (
        "<p><span style='color:var(--signal-on);'>&#10003;</span> "
        "Authorized &mdash; soccer-cam can upload to this YouTube channel.</p>"
        '<div class="row">'
        '<a class="btn-ghost btn" href="/auth/youtube/start?return_to=/setup/youtube">'
        "Re-authorize a different account</a>"
        '<a class="btn" href="/setup/summary">Continue</a>'
        "</div>"
    )
    return body


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

        ``at`` is the directory to list. Empty/missing → top level
        (drives on Windows, root on Unix). Accepts UNC paths
        (``\\\\server\\share``) so users can pick into network shares
        the service can reach.
        """
        # Persistent header lets users type any path (drive letter,
        # UNC, anywhere) and Go to it — useful for network shares
        # that aren't in the drive listing.
        current = at or ""
        header_html = (
            '<form id="browse-go-form" class="row" style="margin-bottom:0.5rem;">'
            f'<input id="browse-go-input" type="text" value="{html.escape(current)}" '
            'placeholder="C:\\path\\to\\folder or \\\\server\\share" '
            'spellcheck="false" '
            'style="flex:1; font-family: ui-monospace, Consolas, monospace;">'
            '<button type="submit" class="btn btn-ghost">Go</button>'
            '<button type="button" class="btn btn-ghost" id="browse-close">Close</button>'
            "</form>"
        )

        if not current:
            if os.name == "nt":
                drives = _list_drives()
                drive_buttons = "".join(
                    f'<button type="button" class="path-chip" '
                    f'data-path="{html.escape(p)}">{html.escape(p)}</button>'
                    for p in drives
                )
                network_help = (
                    '<div class="muted" style="margin-top:0.75rem;">'
                    "Network share? Type the UNC path "
                    "(<code>\\\\server\\share</code>) into the box above "
                    "and click Go. Per-user mapped letter drives won't "
                    "appear here — the service runs as <code>LocalSystem</code> "
                    "and doesn't see your session's drive mappings."
                    "</div>"
                )
                return HTMLResponse(
                    header_html
                    + '<div class="muted">Local drives</div>'
                    + f'<div class="path-list">{drive_buttons}</div>'
                    + network_help
                )
            return HTMLResponse(
                header_html
                + '<div class="muted">Filesystem</div>'
                + '<div class="path-list">'
                + '<button type="button" class="path-chip" data-path="/">/</button>'
                + "</div>"
            )

        path_obj = Path(current)
        try:
            is_dir = path_obj.is_dir()
        except OSError as exc:
            return HTMLResponse(
                header_html
                + f'<div class="err">Cannot access: {html.escape(str(path_obj))} '
                + f"&mdash; {html.escape(str(exc))}</div>"
            )
        if not is_dir:
            return HTMLResponse(
                header_html
                + f'<div class="err">Not a directory: {html.escape(str(path_obj))}</div>'
            )

        # Parent navigation. Drive roots (C:\) and UNC share roots
        # (\\server\share) loop back on .parent — send those to the
        # top-level "Drives" view instead.
        parent = path_obj.parent
        s = str(path_obj)
        is_drive_root = os.name == "nt" and str(parent) == s
        is_unc_share_root = (
            os.name == "nt" and s.startswith("\\\\") and len(path_obj.parts) <= 2
        )
        if is_drive_root or is_unc_share_root:
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
            header_html
            + '<div class="row" style="justify-content:space-between;">'
            + f"<div>{parent_html}</div>"
            + '<button type="button" class="btn" id="use-this-path" '
            + f'data-path="{html.escape(str(path_obj))}">Use this folder</button>'
            + "</div>"
            + '<div class="muted" style="margin-top:0.5rem; '
            + 'font-family: ui-monospace, Consolas, monospace;">'
            + f"{html.escape(str(path_obj))}</div>"
            + '<div class="path-list" style="margin-top:0.5rem;">'
            + f"{subdir_buttons}</div>"
        )

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
            '<form method="post" action="/setup/camera" id="camera-form">'
            "<label>Camera type"
            '<select name="camera_type" id="camera-type" required>'
            f'<option value="dahua" {"selected" if state.camera_type == "dahua" else ""}>Dahua</option>'
            f'<option value="reolink" {"selected" if state.camera_type == "reolink" else ""}>Reolink</option>'
            "</select></label>"
            "<label>Camera name"
            f'<input name="camera_name" id="camera-name" type="text" value="{html.escape(state.camera_name)}" required>'
            '<span class="muted">Used as the [CAMERA.&lt;name&gt;] section in '
            "config.ini. Pick anything (e.g. <code>field</code>).</span></label>"
            "<label>IP address"
            f'<input name="camera_ip" id="camera-ip" type="text" value="{html.escape(state.camera_ip)}" placeholder="192.168.1.100" required>'
            "</label>"
            "<label>Username"
            f'<input name="camera_username" id="camera-username" type="text" value="{html.escape(state.camera_username)}" required></label>'
            "<label>Password"
            '<input name="camera_password" id="camera-password" type="password" value="" placeholder="(set on save)" required>'
            "</label>"
            '<div class="row">'
            '<button type="button" class="btn btn-ghost" id="test-btn">'
            "Test connection</button>"
            '<span id="test-result" class="muted"></span>'
            "</div>"
            '<div class="row">'
            '<a class="btn-ghost btn" href="/setup/storage">Back</a>'
            '<button class="btn" type="submit">Next</button>'
            "</div></form>" + _CAMERA_TEST_JS
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

    @router.post("/camera/test")
    async def camera_test(
        camera_type: str = Form(...),
        camera_ip: str = Form(...),
        camera_username: str = Form(...),
        camera_password: str = Form(...),
    ) -> dict:
        """Probe the camera with the user's typed credentials.

        TCP-connects to port 80 first so a typo / wrong subnet fails
        fast with a clear message; then runs the camera class's
        ``check_availability`` (HTTP Digest auth for Dahua, Reolink's
        login API for Reolink) for an end-to-end verdict.
        """
        import socket

        ip = camera_ip.strip()
        if not ip:
            return {"ok": False, "message": "IP address is empty."}

        # Step 1: cheap TCP probe so unreachable IPs fail in <2s
        # rather than hanging for the full HTTP timeout.
        try:
            with socket.create_connection((ip, 80), timeout=2):
                pass
        except OSError as exc:
            return {
                "ok": False,
                "message": f"Cannot reach {ip}:80 — {exc}. "
                "Check the camera is powered on and on the same network.",
            }

        # Step 2: real auth check.
        try:
            cam_config = CameraConfig(
                name="setup-probe",
                type=camera_type,
                device_ip=ip,
                username=camera_username,
                password=camera_password,
            )
        except Exception as exc:
            return {"ok": False, "message": f"Bad camera config: {exc}"}

        if camera_type == "dahua":
            from video_grouper.cameras.dahua import DahuaCamera

            cam = DahuaCamera(cam_config, storage_path=str(config_path.parent))
        elif camera_type == "reolink":
            from video_grouper.cameras.reolink import ReolinkCamera

            cam = ReolinkCamera(cam_config, storage_path=str(config_path.parent))
        else:
            return {"ok": False, "message": f"Unknown camera type: {camera_type}"}

        try:
            ok = await cam.check_availability()
        except Exception as exc:
            return {
                "ok": False,
                "message": f"Connect failed: {exc}",
            }
        if ok:
            return {"ok": True, "message": f"Connected to {camera_type} at {ip}."}
        return {
            "ok": False,
            "message": (
                "TCP reached the device but auth check failed. "
                "Verify username/password, and that this is a "
                f"{camera_type} camera."
            ),
        }

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
        return _redirect_with_cookie("/setup/youtube", token)

    @router.get("/youtube", response_class=HTMLResponse)
    def youtube_get(request: Request) -> HTMLResponse:
        token, state = get_or_create(request.cookies.get(cookie_name()))
        # State is read from disk: did the user already drop in a
        # client_secret, and have they completed OAuth? The OAuth
        # callback in auth_server.py writes to the same path we read
        # here.
        yt_dir = Path(state.storage_path or "") / "youtube"
        has_secret = (yt_dir / "client_secret.json").exists()
        has_token = (yt_dir / "token.json").exists()
        body = _render_youtube_body(has_secret, has_token)
        resp = HTMLResponse(
            _page(
                "youtube",
                "YouTube uploads",
                "Upload your finished games to your own channel.",
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

    @router.post("/youtube/upload", response_class=HTMLResponse)
    async def youtube_upload(
        request: Request,
        client_secret: UploadFile,
    ) -> RedirectResponse:
        token = request.cookies.get(cookie_name())
        state = get(token)
        if state is None or not state.storage_path:
            return RedirectResponse(url="/setup/welcome", status_code=303)
        # Validate it's a JSON file with the expected shape before
        # writing — otherwise a typo file would leave the wizard stuck
        # at "OAuth fails with cryptic error".
        try:
            raw = await client_secret.read()
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Uploaded file is not valid JSON: {exc}",
            )
        # Google's Desktop OAuth client_secret.json wraps everything
        # under a top-level "installed" key with client_id/client_secret.
        installed = data.get("installed") or data.get("web") or {}
        if not installed.get("client_id") or not installed.get("client_secret"):
            raise HTTPException(
                status_code=400,
                detail=(
                    "client_secret.json missing client_id/client_secret. "
                    "Make sure you downloaded it from a Google Cloud "
                    "OAuth 2.0 Client (Desktop app type)."
                ),
            )
        yt_dir = Path(state.storage_path) / "youtube"
        yt_dir.mkdir(parents=True, exist_ok=True)
        (yt_dir / "client_secret.json").write_bytes(raw)
        return _redirect_with_cookie("/setup/youtube", token)

    @router.post("/youtube/skip")
    def youtube_skip(request: Request) -> RedirectResponse:
        token = request.cookies.get(cookie_name())
        if get(token) is None:
            return RedirectResponse(url="/setup/welcome", status_code=303)
        return _redirect_with_cookie("/setup/summary", token)

    @router.get("/summary", response_class=HTMLResponse)
    def summary_get(request: Request) -> HTMLResponse:
        token = request.cookies.get(cookie_name())
        state = get(token)
        if state is None or not state.is_complete:
            return RedirectResponse(url="/setup/welcome", status_code=303)
        # Reflect filesystem state for YouTube — token.json is the
        # source of truth for "did OAuth succeed". The /finish handler
        # uses the same check to decide whether to set
        # [YOUTUBE].enabled = true in config.ini.
        yt_token = Path(state.storage_path) / "youtube" / "token.json"
        yt_line = (
            "<dt>YouTube</dt><dd><code class='ok'>Authorized</code></dd>"
            if yt_token.exists()
            else "<dt>YouTube</dt><dd><span class='muted'>Skipped &mdash; "
            "set up later from the dashboard</span></dd>"
        )
        body = (
            '<div class="summary"><dl>'
            f"<dt>Storage path</dt><dd><code>{html.escape(state.storage_path)}</code></dd>"
            f"<dt>Camera</dt><dd>{html.escape(state.camera_type)} <code>{html.escape(state.camera_name)}</code> "
            f"@ <code>{html.escape(state.camera_ip)}</code> (user <code>{html.escape(state.camera_username)}</code>)</dd>"
            f"{yt_line}"
            "</dl></div>"
            f'<form method="post" action="/setup/finish">'
            f'<p class="muted">Saving will write <code>{html.escape(str(config_path))}</code> '
            "with these values plus safe defaults for everything else. After save, "
            'visit <a href="/config">/config</a> for NTFY, PlayMetrics, TeamSnap.</p>'
            '<div class="row">'
            '<a class="btn-ghost btn" href="/setup/youtube">Back</a>'
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
    # YOUTUBE.enabled tracks whether the user completed the OAuth flow
    # (token.json exists). Skipping the YouTube wizard step leaves it
    # disabled; the user can enable it later from the dashboard.
    yt_token = Path(state.storage_path) / "youtube" / "token.json"
    youtube_cfg = YouTubeConfig(enabled=yt_token.exists())
    # Seed a starting [PIPELINE] from the homegrown preset so a fresh install
    # has a real, hand-editable pipeline scaffold (stitch -> detect -> track ->
    # render) rather than a blank section. It's left DISABLED: the detect step
    # needs a model source the wizard doesn't collect (TTT login resolves a
    # model_key, or the user points model_path at a local .onnx), so the user
    # finishes wiring it up on /config before flipping enabled = true. We keep
    # onboarding minimal here — no visual pipeline editor.
    pipeline_cfg = apply_preset("homegrown", enabled=False)
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
            "YOUTUBE": youtube_cfg.model_dump(),
            "AUTOCAM": AutocamConfig().model_dump(),
            "CLOUD_SYNC": CloudSyncConfig().model_dump(),
            "TTT": TTTConfig().model_dump(),
            "SETUP": SetupConfig(onboarding_completed=True).model_dump(),
            "PIPELINE": pipeline_cfg.model_dump(),
        },
        by_alias=True,
        by_name=True,
    )


# Optional helper used by the dashboard to detect "no config yet" and
# redirect to the wizard. Kept here so the auth_server doesn't grow a
# new responsibility.
def needs_setup(config_path: Optional[Path]) -> bool:
    return config_path is None or not config_path.exists()
