"""Shared page chrome for the Soccer-Cam web UI.

Every page the orchestrator serves — dashboard, auth, config editor, setup
wizard, seam calibration — builds its ``<head>`` and topbar from here, so
there is exactly one place that decides what Soccer-Cam looks like.

Before this module each page carried its own inline ``<style>`` block. That is
why the product drifted into four background colours, two near-identical
oranges and three font stacks: there was no shared mechanism, so consistency
could not hold. Add markup here, not another ``<style>`` block.

The design system is shared verbatim with Team Tech Tools — see
``static/soccer-cam.css`` for the tokens and the rules that govern them.
"""

from __future__ import annotations

from pathlib import Path

STATIC_DIR = Path(__file__).parent / "static"

#: Google Fonts stack. Barlow Condensed is the display face shared with TTT;
#: Inter and JetBrains Mono match TTT's body and mono roles.
_FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?'
    "family=Barlow+Condensed:wght@600;700&"
    "family=Inter:wght@400;500;600&"
    "family=JetBrains+Mono:wght@400;500;600&"
    'display=swap" rel="stylesheet">'
)

#: Top-level destinations, in the order they appear in the topbar.
#:
#: Setup is here because that is where people look for it. It was left out
#: at first on the argument that, as a peer of Settings, it is a second door
#: to the same fields -- true, but it is also the guided way through them,
#: and burying it made it unfindable. Settings is the reference; Setup is the
#: walkthrough. Both are places you go on purpose.
#:
#: Still not here:
#:   /stitch  seam calibration. A maintenance job after a knock or a remount,
#:            done on a phone at the pitch, so it is launched from the camera
#:            it belongs to rather than from a global menu.
_NAV = (
    ("/", "Status"),
    ("/config", "Settings"),
    ("/setup", "Setup"),
)


def _shared_css() -> str:
    """Return the shared stylesheet's text, read once and cached.

    Used by pages that inline it instead of linking it. Same file either way,
    so an inlining page can never drift from a linking one.
    """
    global _SHARED_CSS
    if _SHARED_CSS is None:
        _SHARED_CSS = (STATIC_DIR / "soccer-cam.css").read_text(encoding="utf-8")
    return _SHARED_CSS


_SHARED_CSS: str | None = None


def head(
    title: str,
    *,
    extra_css: str = "",
    webfonts: bool = True,
    inline_css: bool = False,
    refresh: int | None = None,
) -> str:
    """Return the shared ``<head>`` for a page.

    Args:
        title: Page title, shown after the product name in the tab.
        extra_css: Page-specific CSS for genuinely one-off needs (a canvas
            layout, a video overlay). Component styling belongs in
            ``static/soccer-cam.css`` instead — if two pages need it, it is
            not page-specific.
        webfonts: Fetch Barlow Condensed / Inter / JetBrains Mono from the
            Google Fonts CDN. Pass ``False`` for pages used in the field --
            a phone at a pitch should not wait on a CDN over whatever link
            the ground has. The token fallbacks keep the condensed look on
            system faces, so the page still belongs to the same system.
        inline_css: Inline the shared stylesheet instead of linking it, so
            the page needs no follow-up request at all. Same file either
            way. Pass ``True`` alongside ``webfonts=False`` for field pages;
            everywhere else linking is better, because the browser caches
            one stylesheet across every page.
        refresh: Seconds between automatic reloads, via ``<meta
            http-equiv="refresh">``. Reloads the current URL including its
            fragment, so a reader parked on ``#cameras`` stays there. Omit
            for pages that should not reload under the user.
    """
    style = f"<style>{extra_css}</style>" if extra_css else ""
    if inline_css:
        sheet = f"<style>{_shared_css()}</style>"
    else:
        sheet = '<link rel="stylesheet" href="/static/soccer-cam.css">'
    return (
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1, '
        'viewport-fit=cover">'
        '<meta name="color-scheme" content="dark">'
        f"{f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ''}"
        f"<title>Soccer-Cam · {title}</title>"
        f"{_FONTS if webfonts else ''}"
        f"{sheet}"
        f"{style}"
    )


def tally(state: str = "idle") -> str:
    """Return the tally strip — the capture-state indicator on every page.

    Args:
        state: One of ``idle``, ``armed``, ``recording``, ``error``. Anything
            else falls back to ``idle`` so a bad value cannot claim the
            appliance is recording when it is not.
    """
    if state not in ("idle", "armed", "recording", "error"):
        state = "idle"
    return f'<div class="tally" data-state="{state}" role="presentation"></div>'


def topbar(active: str = "", *, crumb: str = "") -> str:
    """Return the shared topbar.

    Args:
        active: Path of the current page (e.g. ``/config``), marked with
            ``aria-current`` so it reads as current to assistive tech too.
        crumb: Optional trailing context, e.g. a section name.
    """
    links = []
    for href, label in _NAV:
        current = ' aria-current="page"' if href == active else ""
        links.append(f'<a href="{href}"{current}>{label}</a>')
    nav = "".join(links)

    trailing = f'<span class="crumb">{crumb}</span>' if crumb else ""

    # Nav and crumb share one right-hand group so the nav sits in the same
    # place on every page. Left as siblings under space-between, the nav slid
    # to the centre whenever a crumb was present.
    return (
        '<header class="topbar"><div class="topbar-inner">'
        '<a class="brand" href="/">Soccer<span class="dot">·</span>Cam</a>'
        '<div class="topbar-end">'
        f'<nav class="topnav">{nav}</nav>'
        f"{trailing}"
        "</div>"
        "</div></header>"
    )


def notice_page(
    title: str,
    heading: str,
    body_html: str,
    *,
    tone: str = "info",
    extra_js: str = "",
) -> str:
    """Return a single-message page: sign-in outcomes, redirects, errors.

    Args:
        title: Page title for the tab.
        heading: The one-line outcome, in the interface's voice.
        body_html: Supporting detail and any follow-on link. Pre-escaped.
        tone: ``info``, ``ok`` or ``bad``. Drives the accent rule only --
            the heading text always says what happened, so tone is never
            the sole carrier of meaning.
        extra_js: Page-specific script, injected before ``</body>``.
    """
    accent = {"ok": "panel--ok", "bad": "panel--bad"}.get(tone, "panel--accent")
    return page(
        title,
        (
            '<main class="shell shell--narrow">'
            f'<section class="panel {accent}">'
            f"<h1>{heading}</h1>"
            f"{body_html}"
            "</section></main>"
        ),
        extra_js=extra_js,
    )


def page(
    title: str,
    body: str,
    *,
    active: str = "",
    crumb: str = "",
    capture_state: str = "idle",
    extra_css: str = "",
    extra_js: str = "",
    refresh: int | None = None,
) -> str:
    """Assemble a complete page from the shared chrome.

    Args:
        title: Page title for the tab.
        body: Inner HTML, normally one ``<div class="shell">``.
        active: Path of the current page, for topbar highlighting.
        crumb: Optional trailing context in the topbar.
        capture_state: Drives the tally strip. See :func:`tally`.
        extra_css: Page-specific CSS. See :func:`head`.
        extra_js: Page-specific script, injected before ``</body>``.
        refresh: Seconds between automatic reloads. See :func:`head`.
    """
    script = f"<script>{extra_js}</script>" if extra_js else ""
    return (
        "<!DOCTYPE html>"
        '<html lang="en"><head>'
        f"{head(title, extra_css=extra_css, refresh=refresh)}"
        "</head><body>"
        f"{tally(capture_state)}"
        f"{topbar(active, crumb=crumb)}"
        f"{body}"
        f"{script}"
        "</body></html>"
    )
