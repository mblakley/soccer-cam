"""FastAPI router for the onboarding wizard."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import string
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

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
    load_config,
    save_config,
)
from video_grouper.web import chrome
from video_grouper.web.setup.state import (
    cookie_name,
    discard,
    get,
    get_or_create,
)

logger = logging.getLogger(__name__)


def _sse(event: str, payload: dict) -> str:
    """Format one server-sent event. The blank line terminates the frame."""
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


_PAGE_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
__CHROME_HEAD__
<style>
/* Page-specific: the step tracker, the settings summary and the storage
   path picker. Everything else comes from static/soccer-cam.css. */

.shell { max-width: 720px; }

.steps {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 24px;
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--color-text-muted);
}
.steps .step {
  padding: 6px 10px;
  border: 1px solid var(--color-border);
  border-radius: 2px;
}
.steps .step.now {
  color: var(--color-accent);
  border-color: var(--color-accent);
}

.headline {
  font-family: var(--font-headline);
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  font-size: clamp(32px, 5vw, 48px);
  line-height: 0.95;
  margin: 0 0 8px;
}
.lede { max-width: 56ch; margin: 0 0 24px; }

.summary {
  padding: 18px 22px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  background: var(--color-bg-card);
}
.summary dt {
  font-family: var(--font-mono);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--color-text-muted);
  margin-top: 10px;
}
.summary dt:first-child { margin-top: 0; }
.summary dd {
  margin: 0 0 6px;
  font-family: var(--font-mono);
  font-size: 13px;
}
.summary code { color: var(--color-accent); }

/* Each field in this wizard is a bare <label> wrapping its input. Labels are
   inline by default, so a trailing hint and the next label share a line --
   which is how "Make" ended up printed after the config.ini note. */
form label {
  display: block;
  margin-bottom: 16px;
  font-size: 14px;
  font-weight: 500;
}
form label .hint,
form label .muted {
  display: block;
  margin-top: 4px;
  font-weight: 400;
}
form label input,
form label select {
  margin-top: 6px;
}

.path-list { max-height: 280px; overflow-y: auto; }
.path-chip {
  display: block;
  width: 100%;
  text-align: left;
  padding: 8px 12px;
  background: var(--color-bg-input);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  cursor: pointer;
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--color-text-primary);
  transition:
    background-color var(--transition-fast),
    border-color var(--transition-fast),
    color var(--transition-fast);
}
.path-chip:hover {
  background: var(--color-bg-hover);
  border-color: var(--color-accent);
  color: var(--color-accent);
}
/* A picked camera, not merely hovered. */
.path-chip.active {
  border-color: var(--color-accent);
  background: var(--color-accent-light);
  color: var(--color-text-primary);
}
.path-chip strong { font-weight: 600; }
.path-chip .faint { margin-left: 8px; }

/* Advanced entry, folded away. */
details.panel > summary {
  cursor: pointer;
  font-family: var(--font-headline);
  font-weight: 700;
  font-size: 18px;
  list-style: none;
  display: flex;
  align-items: center;
  gap: 8px;
}
details.panel > summary::-webkit-details-marker { display: none; }
details.panel > summary::before {
  content: "+";
  color: var(--color-accent);
  font-family: var(--font-mono);
  font-size: 16px;
}
details.panel[open] > summary::before { content: "−"; }
details.panel[open] > summary { margin-bottom: 12px; }
</style>
</head>
<body>
__CHROME_TOPBAR__
<main class="shell shell--narrow">
<__STEPS__>
<h1 class="headline">__TITLE__</h1>
<p class="lede">__LEDE__</p>
__BODY__
</main>
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
        modal.innerHTML = '<div class="banner banner--bad">Browse failed: ' + err + "</div>";
      });
  }
})();
</script>
"""

_CAMERA_SCAN_JS = """
<script>
(function () {
  const btn = document.getElementById("scan-btn");
  const out = document.getElementById("scan-result");
  const list = document.getElementById("scan-list");
  if (!btn || !out || !list) return;

  const ipField = document.getElementById("camera-ip");
  const nameField = document.getElementById("camera-name");
  const typeField = document.getElementById("camera-type");

  // Default the config section name from the model, so most people never
  // have to invent one. Sanitised to what an INI section key allows.
  function suggestName(device) {
    const raw = device.name || device.hardware || ("cam-" + device.ip.split(".").pop());
    return raw.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  }

  function select(device, card) {
    list.querySelectorAll(".path-chip").forEach((c) => c.classList.remove("active"));
    card.classList.add("active");
    ipField.value = device.ip;
    if (device.vendor === "Reolink") typeField.value = "reolink";
    else if (device.vendor === "Dahua") typeField.value = "dahua";
    if (!nameField.value) nameField.value = suggestName(device);
    out.textContent = "Selected " + device.ip + ". Enter the username and password, then Test connection.";
  }

  function render(devices) {
    list.innerHTML = "";
    devices.forEach((d) => {
      const card = document.createElement("button");
      card.type = "button";
      card.className = "path-chip";
      const detail = d.label || "Unrecognised device";
      card.innerHTML =
        '<strong>' + d.ip + '</strong> <span class="faint">' + detail + '</span>';
      card.addEventListener("click", () => select(d, card));
      list.appendChild(card);
    });
  }

  const bar = document.getElementById("scan-bar");
  const fill = document.getElementById("scan-bar-fill");

  function showProgress(done, total) {
    // Only claim a percentage while there is a real count behind it.
    if (!total) return;
    bar.hidden = false;
    bar.classList.remove("progress--indeterminate");
    const pct = Math.round((done / total) * 100);
    fill.style.width = pct + "%";
    if (done >= total) {
      // The sweep is finished but the ONVIF listen window has not closed.
      // Say so rather than let a full bar sit there looking stuck.
      out.textContent = "Listening for ONVIF replies…";
      // Clear the inline width so the indeterminate rule can take over.
      fill.style.width = "";
      bar.classList.add("progress--indeterminate");
    } else {
      out.textContent = "Scanning… " + pct + "%";
    }
  }

  function hideProgress() {
    bar.hidden = true;
    bar.classList.remove("progress--indeterminate");
    fill.style.width = "0%";
  }

  function finish(devices) {
    hideProgress();
    if (devices.length === 0) {
      out.textContent =
        "No cameras answered. They may be on another network, or blocked by " +
        "this machine's firewall. Enter the camera manually below.";
      const manual = document.querySelector("details.panel");
      if (manual) manual.open = true;
    } else {
      out.textContent =
        devices.length === 1 ? "Found 1 camera." : "Found " + devices.length + " cameras.";
      render(devices);
    }
    btn.disabled = false;
  }

  function scanStreaming() {
    // EventSource gives progress as it happens. The count is real: the sweep
    // knows every address it intends to check before it starts.
    const src = new EventSource("/setup/camera/scan/stream");
    let settled = false;

    src.addEventListener("progress", (e) => {
      const d = JSON.parse(e.data);
      showProgress(d.done, d.total);
    });
    src.addEventListener("done", (e) => {
      settled = true;
      src.close();
      finish(JSON.parse(e.data).devices || []);
    });
    src.addEventListener("failed", (e) => {
      settled = true;
      src.close();
      hideProgress();
      out.textContent = JSON.parse(e.data).message || "Scan failed.";
      btn.disabled = false;
    });
    src.onerror = () => {
      if (settled) return;
      // The stream died. Fall back to the plain request rather than leaving
      // the user with a stalled bar.
      src.close();
      scanPlain();
    };
  }

  async function scanPlain() {
    bar.hidden = false;
    fill.style.width = "";
    bar.classList.add("progress--indeterminate");
    out.textContent = "Looking for cameras on this network…";
    try {
      const r = await fetch("/setup/camera/scan", { method: "POST" });
      const data = await r.json();
      if (!data.ok) {
        hideProgress();
        out.textContent = data.message || "Scan failed.";
        btn.disabled = false;
        return;
      }
      finish(data.devices || []);
    } catch (e) {
      hideProgress();
      out.textContent = "Scan failed: " + e;
      btn.disabled = false;
    }
  }

  btn.addEventListener("click", () => {
    btn.disabled = true;
    list.innerHTML = "";
    hideProgress();
    if (window.EventSource) scanStreaming();
    else scanPlain();
  });

})();
</script>
"""

_CAMERA_TEST_JS = """
<script>
(function () {
  const btn = document.getElementById("test-btn");
  const out = document.getElementById("test-result");
  if (!btn || !out) return;
  const typeField = document.getElementById("camera-type");

  btn.addEventListener("click", async () => {
    const form = new FormData();
    form.set("camera_ip", document.getElementById("camera-ip").value);
    form.set("camera_username", document.getElementById("camera-username").value);
    form.set("camera_password", document.getElementById("camera-password").value);
    btn.disabled = true;
    out.textContent = "Testing…";
    out.style.color = "";
    try {
      // Identify rather than test against a chosen make: the camera can say
      // what it is, which keeps the make dropdown out of the common path.
      const r = await fetch("/setup/camera/identify", { method: "POST", body: form });
      const data = await r.json();
      if (data.ok && data.camera_type) typeField.value = data.camera_type;
      out.textContent = (data.ok ? "\\u2713 " : "\\u2717 ") + (data.message || "");
      out.style.color = data.ok
        ? "var(--color-success)"
        : "var(--color-danger)";
    } catch (e) {
      out.textContent = "\\u2717 " + e;
      out.style.color = "var(--color-danger)";
    } finally {
      btn.disabled = false;
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


def _page(
    active: str,
    title: str,
    lede: str,
    body: str,
    onboarding_complete: bool = False,
) -> str:
    # Defaults to False: this is the wizard, so assume setup is unfinished
    # unless the caller knows better. Status is dropped from the nav while
    # that holds, because "/" would redirect straight back here.
    return (
        _PAGE_TEMPLATE.replace("__CHROME_HEAD__", chrome.head(f"Setup · {title}"))
        .replace(
            "__CHROME_TOPBAR__",
            chrome.tally()
            + chrome.topbar("/setup", onboarding_complete=onboarding_complete),
        )
        .replace("<__STEPS__>", _render_steps(active))
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


def _camera_body(state) -> str:
    """Return the camera step's form markup for a wizard state.

    Split out from the route so it can be rendered without a request --
    the preview harness and tests both build this page directly.
    """
    # Scanning is the primary path: most people do not know their camera's
    # address, and the camera can simply be asked. Typing a name and address
    # is still here, folded away, for a camera on another subnet or with
    # ONVIF discovery turned off.
    manual_open = " open" if state.camera_ip else ""
    return (
        '<form method="post" action="/setup/camera" id="camera-form">'
        '<section class="panel">'
        '<div class="btn-row">'
        '<button type="button" class="btn" id="scan-btn">'
        "Scan for cameras</button>"
        '<span id="scan-result" class="hint" role="status" aria-live="polite">'
        "</span>"
        "</div>"
        '<div class="progress" id="scan-bar" hidden>'
        '<div class="progress-fill" id="scan-bar-fill"></div>'
        "</div>"
        '<div id="scan-list" class="path-list" style="margin-top:12px"></div>'
        "</section>"
        "<label>Username"
        f'<input name="camera_username" id="camera-username" type="text" value="{html.escape(state.camera_username)}" required></label>'
        "<label>Password"
        '<input name="camera_password" id="camera-password" type="password" value="" placeholder="(set on save)" required>'
        "</label>"
        f'<details class="panel"{manual_open}>'
        "<summary>Enter the camera manually</summary>"
        '<p class="hint">For a camera on a different network, or one with '
        "ONVIF discovery switched off.</p>"
        "<label>IP address"
        f'<input name="camera_ip" id="camera-ip" type="text" value="{html.escape(state.camera_ip)}" placeholder="192.168.1.100" required>'
        "</label>"
        "<label>Name"
        f'<input name="camera_name" id="camera-name" type="text" value="{html.escape(state.camera_name)}" required>'
        '<span class="hint">Names the [CAMERA.&lt;name&gt;] section in '
        "config.ini. Anything you like, e.g. <code>field</code>.</span></label>"
        "<label>Make"
        '<select name="camera_type" id="camera-type" required>'
        f'<option value="dahua" {"selected" if state.camera_type == "dahua" else ""}>Dahua</option>'
        f'<option value="reolink" {"selected" if state.camera_type == "reolink" else ""}>Reolink</option>'
        "</select>"
        '<span class="hint">Set for you when you pick a scanned camera.</span>'
        "</label>"
        "</details>"
        '<div class="row">'
        '<button type="button" class="btn btn-secondary" id="test-btn">'
        "Test connection</button>"
        '<span id="test-result" class="hint"></span>'
        "</div>"
        '<div class="row">'
        '<a class="btn-ghost btn" href="/setup/storage">Back</a>'
        '<button class="btn" type="submit">Next</button>'
        "</div></form>" + _CAMERA_SCAN_JS + _CAMERA_TEST_JS
    )


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
            '<a href="/config">Settings</a>.</p>'
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
            "padding:12px; border:1px solid var(--color-border); "
            'border-radius:4px; background:var(--color-bg-card);"></div>'
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
    def storage_browse(at: str | None = Query(None)) -> HTMLResponse:
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
                + f'<div class="banner banner--bad">Cannot access: {html.escape(str(path_obj))} '
                + f"&mdash; {html.escape(str(exc))}</div>"
            )
        if not is_dir:
            return HTMLResponse(
                header_html
                + f'<div class="banner banner--bad">Not a directory: {html.escape(str(path_obj))}</div>'
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
        body = _camera_body(state)
        resp = HTMLResponse(
            _page(
                "camera",
                "Camera",
                "Find the camera on your network, or enter it yourself.",
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

    @router.post("/camera/scan")
    async def camera_scan() -> dict:
        """Ask the local network which cameras are on it.

        Two methods at once, because neither is sufficient alone:

        * ONVIF WS-Discovery -- gives a model name without credentials, but
          only when the owner has enabled ONVIF. Reolink ships with it **off**
          (``GetNetPort`` reports ``onvifEnable: 0``), so on its own this finds
          nothing for most Reolink owners.
        * A sweep of the attached networks, fingerprinting whatever answers on
          port 80. Both vendors identify themselves in how they reject an
          unauthenticated request, so this works with ONVIF off.

        No credentials are involved either way. ``/camera/identify`` is what
        confirms the make once the user supplies them.
        """
        from video_grouper.cameras.discovery import discover_cameras

        try:
            devices = await discover_cameras(3.0)
        except Exception as exc:
            logger.warning("Camera scan failed: %s", exc)
            return {"ok": False, "devices": [], "message": f"Scan failed: {exc}"}

        return {
            "ok": True,
            "devices": [
                {
                    "ip": d.ip,
                    "name": d.name,
                    "hardware": d.hardware,
                    "vendor": d.vendor,
                    "label": d.label,
                }
                for d in devices
            ],
        }

    @router.get("/camera/scan/stream")
    async def camera_scan_stream() -> StreamingResponse:
        """Stream scan progress, then the result, as server-sent events.

        The progress is real, not a pacifier: the sweep knows every address it
        intends to check before it starts, and reports each one as it lands.
        The ONVIF probe cannot be measured that way -- it is a fixed listen
        window -- so once the sweep finishes the client is told it is waiting
        on ONVIF rather than being shown a bar that invents movement.

        GET because EventSource only issues GETs. Safe: the scan changes
        nothing on this machine, and the same-origin Host allowlist still
        applies (see auth_server's middleware).
        """
        from video_grouper.cameras.discovery import discover_cameras

        # Updated from the sweep callback, sampled by the generator. A shared
        # cell rather than a queue: 1270 addresses would mean 1270 events, and
        # the client only ever needs the latest number.
        state = {"done": 0, "total": 0}

        def on_progress(done: int, total: int) -> None:
            state["done"] = done
            state["total"] = total

        async def events():
            task = asyncio.create_task(discover_cameras(3.0, on_progress))
            try:
                while not task.done():
                    yield _sse("progress", state)
                    await asyncio.sleep(0.15)

                devices = await task
            except Exception as exc:
                logger.warning("Camera scan failed: %s", exc)
                yield _sse("failed", {"message": f"Scan failed: {exc}"})
                return

            yield _sse(
                "done",
                {
                    "devices": [
                        {
                            "ip": d.ip,
                            "name": d.name,
                            "hardware": d.hardware,
                            "vendor": d.vendor,
                            "label": d.label,
                        }
                        for d in devices
                    ]
                },
            )

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.post("/camera/identify")
    async def camera_identify(
        camera_ip: str = Form(...),
        camera_username: str = Form(...),
        camera_password: str = Form(...),
    ) -> dict:
        """Confirm what the device at this address is, using credentials.

        Removes the type dropdown from the common path: the camera tells us
        whether it is Reolink or Dahua, and a successful authenticated probe
        is proof where an ONVIF scope was only a hint.
        """
        from video_grouper.cameras.discovery import identify_camera

        ip = camera_ip.strip()
        if not ip:
            return {"ok": False, "message": "Enter an address first."}

        try:
            result = await identify_camera(ip, camera_username, camera_password)
        except Exception as exc:
            logger.warning("Identify failed for %s: %s", ip, exc)
            return {"ok": False, "message": f"Could not reach {ip}: {exc}"}

        if result is None:
            return {
                "ok": False,
                "message": (
                    f"Reached {ip}, but neither a Reolink nor a Dahua answered. "
                    "Check the username and password."
                ),
            }

        camera_type, info = result
        return {
            "ok": True,
            "camera_type": camera_type,
            "name": info.name,
            "model": info.model,
            "message": " ".join(
                p for p in (info.manufacturer, info.model, info.name) if p
            )
            or f"{camera_type} at {ip}",
        }

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
            ) from exc
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

        # Merge into whatever is already on disk. Re-running the wizard on a
        # configured install must not reset the integrations it never asks
        # about; a config that cannot be read is treated as absent rather than
        # blocking the user out of setup.
        existing = None
        if config_path.exists():
            try:
                existing = load_config(config_path)
            except Exception as exc:
                logger.warning(
                    "SETUP: could not read %s (%s); writing a fresh config.",
                    config_path,
                    exc,
                )

        config = _build_config(state, existing)
        try:
            save_config(config, config_path)
        except OSError as exc:
            logger.error("SETUP: save failed: %s", exc)
            raise HTTPException(
                status_code=500,
                detail=f"Could not write {config_path}: {exc}",
            ) from exc

        discard(token)
        # Land on /config so the user can immediately tweak integrations.
        resp = RedirectResponse(url="/config?saved=1", status_code=303)
        resp.delete_cookie(cookie_name())
        return resp

    return router


def _upsert_camera(cameras: list[dict], state) -> list[dict]:
    """Return ``cameras`` with the wizard's camera added or updated in place.

    Matched on name, which is what keys the ``[CAMERA.<name>]`` section. An
    install with a second camera configured by hand keeps it; re-running the
    wizard for "field" updates "field" rather than replacing the list.
    """
    entry = CameraConfig(
        name=state.camera_name,
        type=state.camera_type,
        device_ip=state.camera_ip,
        username=state.camera_username,
        password=state.camera_password,
    ).model_dump()

    merged = [dict(c) for c in cameras]
    for existing in merged:
        if str(existing.get("name", "")).lower() == state.camera_name.lower():
            existing.update(entry)
            return merged
    merged.append(entry)
    return merged


def _build_config(state, existing: Config | None = None) -> Config:
    """Fold the wizard's answers into ``existing``, or into defaults.

    The wizard asks for six things: a storage path and one camera. Everything
    else in config.ini -- NTFY, TeamSnap, PlayMetrics, TTT, AutoCam, cloud
    sync, the pipeline, YouTube playlists -- it never mentions, so re-running
    it must not reset them. Before this merged, finishing the wizard on a
    configured install rebuilt every section from defaults and silently
    discarded the lot.

    Only these are written:
      * ``STORAGE.path``            -- not the section, so min_free_gb survives
      * ``cameras``                 -- upserted by name, not replaced
      * ``YOUTUBE.enabled``         -- not the section, so playlists survive
      * ``SETUP.onboarding_completed``
      * ``PIPELINE``                -- seeded only when there is not one yet
    """
    if existing is not None:
        data = existing.model_dump(by_alias=True)
    else:
        data = {
            "cameras": [],
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
            "SETUP": SetupConfig().model_dump(),
            "PIPELINE": {},
        }

    # Seed a starting [PIPELINE] from the homegrown preset so a fresh install
    # has a real, hand-editable scaffold (stitch -> detect -> track -> render)
    # rather than a blank section. Left DISABLED: the detect step needs a model
    # source the wizard doesn't collect (TTT login resolves a model_key, or the
    # user points model_path at a local .onnx), so the user finishes wiring it
    # up on /config before flipping enabled = true.
    #
    # Only when there isn't one already -- a pipeline the user has since wired
    # up is exactly the kind of work re-running setup must not throw away.
    if not (data.get("PIPELINE") or {}).get("steps"):
        data["PIPELINE"] = apply_preset("homegrown", enabled=False).model_dump()

    # Storage: the path only. Replacing the section would reset min_free_gb.
    storage = dict(data.get("STORAGE") or {})
    storage["path"] = state.storage_path
    data["STORAGE"] = storage

    # YOUTUBE.enabled tracks whether the user completed the OAuth flow
    # (token.json exists). Skipping the YouTube step leaves it disabled. Only
    # the flag: privacy_status, the playlists and playlist_map are the user's.
    yt_token = Path(state.storage_path) / "youtube" / "token.json"
    youtube = dict(data.get("YOUTUBE") or {})
    youtube["enabled"] = yt_token.exists()
    data["YOUTUBE"] = youtube

    data["cameras"] = _upsert_camera(data.get("cameras") or [], state)

    setup = dict(data.get("SETUP") or {})
    setup["onboarding_completed"] = True
    data["SETUP"] = setup

    return Config.model_validate(data, by_alias=True, by_name=True)


# Optional helper used by the dashboard to detect "no config yet" and
# redirect to the wizard. Kept here so the auth_server doesn't grow a
# new responsibility.
def needs_setup(config_path: Path | None) -> bool:
    return config_path is None or not config_path.exists()
