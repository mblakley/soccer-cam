"""Render every web page to static HTML and serve it, for design review.

Looking at the UI should not require a camera, a config file, or a running
pipeline. This renders each page with representative data and serves the lot
on a throwaway port:

    uv run python -m video_grouper.web.preview

Add ``--out DIR`` to write the files without serving, or ``--port N`` to pick
the port. Pages are rendered with auto-refresh stripped so they hold still
while you look at them.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import shutil
import socketserver
import tempfile
import webbrowser
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from video_grouper.utils.config import Config
from video_grouper.web import chrome

# Representative live state: one connected camera, work in flight, so the
# tally shows its recording state rather than an empty page's hairline.
_STATUS = {
    "cameras": [
        {"name": "Field cam", "ip": "192.168.1.50", "connected": True},
        {"name": "End line", "ip": "192.168.1.51", "connected": False},
    ],
    "queue_sizes": {"download": 2, "video": 0, "upload": 1},
}


def _sample_config() -> Config:
    """A Config with every section at its defaults."""
    sections: dict[str, Any] = {}
    for name, field in Config.model_fields.items():
        section_type = field.annotation
        if section_type is None:
            continue
        try:
            sections[name] = section_type()
        except ValidationError:
            # STORAGE requires a path; nothing else has a required field.
            sections[name] = section_type(path="C:/soccer-cam/data")
    return Config(**sections)


def render_all() -> dict[str, str]:
    """Return ``{filename: html}``: every served page, plus notable states
    and the four tally swatches. See ``_GROUPS`` for which is which."""
    from video_grouper.web import auth_server, config_editor, stitch_calibration
    from video_grouper.web.setup import router as setup_router

    dash = auth_server._DASHBOARD_BODY
    for placeholder, value in {
        "__AUTH_FLAGS_BANNER__": "",
        "__AUTH_BLOCK__": '<p><span class="status-dot on"></span>'
        "Signed in as coach@example.com</p>",
        "__YOUTUBE_BLOCK__": '<p class="muted">Connected. Uploads go to '
        "&ldquo;Rochester Flash 2013&rdquo;.</p>",
        "__TRAY_BLOCK__": '<p><span class="status-dot on"></span>'
        "Running &mdash; AutoCam parented.</p>",
        "__QUEUES_BLOCK__": auth_server._render_queues_section(_STATUS),
        "__CAMERAS_BLOCK__": auth_server._render_cameras_section(_STATUS),
        "__GAMES_BLOCK__": "<table><tr><th>Game</th><th>State</th></tr>"
        '<tr><td>2026.08.16-10.00.00</td><td><span class="status-dot on">'
        "</span>Complete</td></tr>"
        '<tr><td>2026.08.22-09.30.00</td><td><span class="status-dot recording">'
        "</span>Downloading</td></tr></table>",
    }.items():
        dash = dash.replace(placeholder, value)

    pages = {
        "dashboard.html": chrome.page(
            "Status",
            dash,
            active="/",
            capture_state=auth_server._capture_state(_STATUS),
        ),
        "config.html": config_editor._render_page(_sample_config(), ""),
        "config-error.html": config_editor._render_page(
            _sample_config(),
            '<div class="banner banner--bad"><strong>Validation failed:</strong>'
            "<ul><li>RECORDING.min_duration: must be a positive integer</li></ul>"
            "</div>",
        ),
        "setup.html": setup_router._page(
            "camera",
            "Camera",
            "Point Soccer-Cam at the recorder on your field.",
            '<label class="field-label" for="addr">Camera address</label>'
            '<input id="addr" type="text" value="192.168.1.50" class="mono">'
            '<p class="hint">The IP or hostname of the Dahua or Reolink unit.</p>'
            '<div class="btn-row" style="margin-top:24px">'
            '<a class="btn" href="#">Continue</a>'
            '<a class="btn btn-ghost" href="#">Back</a></div>',
        ),
        "signed-in.html": auth_server._SUCCESS_PAGE,
        "sign-in-failed.html": auth_server._error_page(
            "The identity provider rejected the sign-in. Check that the "
            "provider is enabled for this deployment, then try again."
        ),
        # The seam tool needs no camera to render. Go through its own
        # renderer rather than the raw template, so no __BANNER__ placeholder
        # leaks into the preview. Its JS finds no live feed here, but the
        # layout is real.
        "stitch.html": stitch_calibration._render_page({"errors": []}),
    }

    # Tally states, so all four are reviewable side by side.
    for state in ("idle", "armed", "recording", "error"):
        pages[f"tally-{state}.html"] = chrome.page(
            f"Tally · {state}",
            f'<main class="shell shell--narrow"><section class="panel">'
            f"<h1>Tally: {state}</h1>"
            f'<p class="muted">The strip at the top of this page is in the '
            f"<code>{state}</code> state.</p></section></main>",
            capture_state=state,
        )
    return pages


#: How the index groups what it lists. Routes are real pages the orchestrator
#: serves; states are those same pages in a condition that is awkward to
#: reproduce on demand; swatches are not pages at all.
_GROUPS: list[tuple[str, str, list[tuple[str, str, str]]]] = [
    (
        "Routes",
        "Pages the orchestrator serves, at the paths it serves them on.",
        [
            ("dashboard.html", "Status", "/"),
            ("config.html", "Settings", "/config"),
            ("setup.html", "Setup wizard", "/setup/camera"),
            ("stitch.html", "Seam calibration", "/stitch"),
            ("signed-in.html", "Signed in", "after OAuth"),
        ],
    ),
    (
        "States",
        "The same pages in conditions that are awkward to reproduce for real.",
        [
            ("config-error.html", "Settings — validation failed", ""),
            ("sign-in-failed.html", "Sign-in failed", ""),
        ],
    ),
    (
        "Tally swatches",
        "Not pages. One per capture state, so the four can be compared.",
        [
            ("tally-idle.html", "idle — asserts nothing", ""),
            ("tally-armed.html", "armed — cameras up, nothing in flight", ""),
            ("tally-recording.html", "recording — live, pulsing", ""),
            ("tally-error.html", "error — a camera is down", ""),
        ],
    ),
]


def _index(names: list[str]) -> str:
    listed = {n for _, _, items in _GROUPS for n, _, _ in items}
    sections = []
    for title, blurb, items in _GROUPS:
        rows = "".join(
            f'<li><a href="{f}">{label}</a>'
            + (f' <span class="faint mono">{path}</span>' if path else "")
            + "</li>"
            for f, label, path in items
            if f in names
        )
        sections.append(
            f'<section class="panel"><h2>{title}</h2>'
            f'<p class="hint">{blurb}</p>'
            f'<ul class="path-list">{rows}</ul></section>'
        )

    # Anything rendered but not grouped above still gets a home, so the index
    # cannot quietly under-report what was generated.
    rest = sorted(n for n in names if n not in listed and n != "index.html")
    if rest:
        rows = "".join(
            f'<li><a href="{n}">{n.removesuffix(".html")}</a></li>' for n in rest
        )
        sections.append(
            '<section class="panel"><h2>Ungrouped</h2>'
            f'<ul class="path-list">{rows}</ul></section>'
        )

    return chrome.page(
        "Preview",
        '<main class="shell shell--narrow">'
        '<div class="page-header"><div><h1>Page preview</h1>'
        '<p class="lede">Sample renders of the Soccer-Cam web UI, for design '
        "review. Static HTML with stand-in data &mdash; nothing here is live, "
        "and no camera, config or pipeline is involved. Not part of the "
        "shipped app.</p></div></div>" + "".join(sections) + "</main>",
    )


def write_to(out: Path) -> Path:
    """Render every page into ``out`` alongside the stylesheet."""
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy(chrome.STATIC_DIR / "soccer-cam.css", out / "soccer-cam.css")
    pages = render_all()
    pages["index.html"] = _index(list(pages))
    for name, html in pages.items():
        # Rewrite the absolute stylesheet path for file/static serving, and
        # drop auto-refresh so pages hold still while you look at them.
        html = html.replace("/static/soccer-cam.css", "soccer-cam.css")
        html = html.replace('<meta http-equiv="refresh" content="10">', "")
        # Point the topbar at the rendered files so the preview is browsable
        # -- otherwise every page's nav 404s on a static server, which is
        # exactly where you notice it: on a phone.
        html = html.replace(
            '<a class="brand" href="/">', '<a class="brand" href="index.html">'
        )
        html = html.replace(
            '<a href="/" aria-current="page">',
            '<a href="dashboard.html" aria-current="page">',
        )
        html = html.replace(
            '<a href="/">Status</a>', '<a href="dashboard.html">Status</a>'
        )
        html = html.replace(
            '<a href="/config" aria-current="page">',
            '<a href="config.html" aria-current="page">',
        )
        html = html.replace(
            '<a href="/config">Settings</a>', '<a href="config.html">Settings</a>'
        )
        (out / name).write_text(html, encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, help="write here instead of serving")
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)

    if args.out:
        print(f"Wrote {len(render_all()) + 1} pages to {write_to(args.out)}")
        return 0

    out = write_to(Path(tempfile.mkdtemp(prefix="soccer-cam-preview-")))
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(out)
    )
    with socketserver.TCPServer(("127.0.0.1", args.port), handler) as httpd:
        url = f"http://127.0.0.1:{args.port}/index.html"
        print(f"Preview at {url}   (Ctrl+C to stop)")
        if not args.no_browser:
            webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
