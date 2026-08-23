"""Guards on the shared design system.

The web UI drifted into four background colours, two near-identical oranges and
three font stacks because nothing checked. These tests are the check. They are
cheap, they need no fixtures, and they fail loudly the first time a page starts
growing its own palette again.

The rules they enforce are written up in ``docs/UI-DESIGN.md``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from video_grouper.web import chrome

REPO = Path(__file__).resolve().parents[2]
SHEET = chrome.STATIC_DIR / "soccer-cam.css"

#: Pages served by the orchestrator, as Python modules holding inline HTML.
WEB_MODULES = [
    REPO / "video_grouper" / "web" / "auth_server.py",
    REPO / "video_grouper" / "web" / "config_editor.py",
    REPO / "video_grouper" / "web" / "stitch_calibration.py",
    REPO / "video_grouper" / "web" / "setup" / "router.py",
]

#: Annotation / labelling tools, served by training/annotation_server.py.
TRAINING_PAGES = sorted((REPO / "training" / "static").glob("*.html"))

#: Colours allowed to stay literal: pure black and white for video and canvas
#: backgrounds, where a theme token would be wrong.
NEUTRALS = {"#000", "#fff", "#000000", "#ffffff"}

#: ``&#10003;`` and friends are HTML entities, not colours.
HEX = re.compile(r"(?<!&)#[0-9a-fA-F]{3,8}\b")

#: Canvas drawing colours (``ctx.fillStyle = '#0f0'``) and categorical label
#: swatches are *data-encoding*: each class has to stay distinguishable from
#: the others, so a shared token would destroy the meaning. They are exempt --
#: see docs/UI-DESIGN.md -- and this guard only reads styling regions, which
#: is where brand colour lives.
STYLE_REGION = re.compile(
    r"<style>(.*?)</style>|_STYLE = \"\"\"(.*?)\"\"\"|style=\"([^\"]*)\"",
    re.S,
)


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _styling(src: str) -> str:
    """Return only the parts of a source file that carry styling."""
    return "\n".join(g for m in STYLE_REGION.finditer(src) for g in m.groups() if g)


# ---------------------------------------------------------------------------
# The stylesheet itself
# ---------------------------------------------------------------------------


def test_shared_stylesheet_exists_and_is_the_only_token_source():
    assert SHEET.is_file(), f"the shared stylesheet is missing: {SHEET}"
    css = _text(SHEET)
    for token in (
        "--color-bg-primary",
        "--color-accent",
        "--color-record",
        "--color-danger",
        "--color-warning",
        "--font-headline",
        "--font-body",
        "--font-mono",
    ):
        assert f"{token}:" in css, f"{token} must be defined in the shared sheet"


def test_every_token_used_by_the_sheet_is_defined_by_the_sheet():
    """A var() that resolves to nothing silently drops the whole declaration."""
    css = _text(SHEET)
    defined = set(re.findall(r"^\s*(--[a-z0-9-]+):", css, re.M))
    used = set(re.findall(r"var\((--[a-z0-9-]+)", css))
    missing = sorted(used - defined)
    assert not missing, f"used but never defined: {missing}"


def test_shared_sheet_matches_ttt_dark_theme():
    """Soccer-Cam ships TTT's dark palette. If these drift, the family breaks.

    Values are duplicated here on purpose: TTT is a separate, closed repo, so
    this test states the contract rather than importing it.
    """
    css = _text(SHEET)
    for token, value in {
        "--color-bg-primary": "#0f0f12",
        "--color-bg-secondary": "#15151a",
        "--color-bg-card": "#1a1a20",
        "--color-text-primary": "#f0f0f2",
        "--color-border": "#2a2a32",
        "--color-accent": "#5b8def",
        "--color-success": "#4ade80",
        "--color-warning": "#f59e0b",
        "--color-danger": "#ef4444",
    }.items():
        assert f"{token}: {value};" in css, (
            f"{token} must stay {value} to match TTT's dark theme"
        )


def test_record_is_not_the_accent():
    """Capture state and 'you can click this' must never be the same colour."""
    css = _text(SHEET)
    accent = re.search(r"--color-accent: *(#[0-9a-fA-F]+);", css).group(1)
    record = re.search(r"--color-record: *(#[0-9a-fA-F]+);", css).group(1)
    assert accent.lower() != record.lower()


#: Loops allowed in the stylesheet, and why each one earns it.
#:   tally-live     the capture indicator -- motion is what separates
#:                  "recording" from "error", which share a hue
#:   progress-sweep a progress bar with no measurable total; it indicates
#:                  duration, not state, so it does not compete with the tally
ALLOWED_LOOPS = {"tally-live", "progress-sweep"}


def test_no_status_indicator_loops_except_the_tally():
    """Motion is what separates 'recording' from 'error'. Keep it exclusive.

    New loops need a line in ALLOWED_LOOPS saying what they mean. Anything
    that loops next to a status colour is competing with the tally.
    """
    css = _text(SHEET)
    looping = set(re.findall(r"animation:\s*([a-z-]+)[^;]*infinite", css))
    assert looping <= ALLOWED_LOOPS, (
        f"undocumented looping animation(s): {sorted(looping - ALLOWED_LOOPS)}"
    )


def test_no_pill_radii():
    css = _text(SHEET)
    assert not re.search(r"border-radius:\s*9{3,4}px", css)


def test_no_transition_all():
    css = _text(SHEET)
    assert "transition: all" not in css, "name the properties you animate"


def test_reduced_motion_is_respected():
    assert "prefers-reduced-motion" in _text(SHEET)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", WEB_MODULES, ids=lambda p: p.name)
def test_web_pages_do_not_define_their_own_palette(path: Path):
    """No page gets its own :root token block. That is how the drift started."""
    src = _text(path)
    assert ":root {" not in src and ":root{" not in src, (
        f"{path.name} defines its own tokens; put them in the shared sheet"
    )


@pytest.mark.parametrize("path", WEB_MODULES, ids=lambda p: p.name)
def test_web_pages_do_not_hardcode_colours(path: Path):
    stray = {h for h in HEX.findall(_styling(_text(path))) if h.lower() not in NEUTRALS}
    assert not stray, f"{path.name} hardcodes {sorted(stray)}; use tokens"


@pytest.mark.parametrize("path", WEB_MODULES, ids=lambda p: p.name)
def test_web_pages_do_not_restyle_shared_classes(path: Path):
    """Page CSS may add page-specific classes, never redefine shared ones."""
    src = _text(path)
    shared = [
        ".btn",
        ".btn-primary",
        ".btn-secondary",
        ".btn-ghost",
        ".btn-sm",
        ".panel",
        ".banner",
        ".status-dot",
        ".topbar",
        ".brand",
        ".crumb",
        ".tally",
    ]
    for cls in shared:
        # A rule, not a class attribute: `.btn {` at the start of a CSS line.
        assert not re.search(rf"^\s*\{re.escape(cls)}\s*(,|\{{)", src, re.M), (
            f"{path.name} restyles {cls}; add a variant to the shared sheet"
        )


@pytest.mark.parametrize("path", TRAINING_PAGES, ids=lambda p: p.name)
def test_training_tools_use_the_shared_sheet(path: Path):
    src = _text(path)
    assert "/shared/soccer-cam.css" in src, (
        f"{path.name} must link the shared stylesheet"
    )
    assert ":root {" not in src and ":root{" not in src, (
        f"{path.name} defines its own tokens"
    )


# ---------------------------------------------------------------------------
# Chrome
# ---------------------------------------------------------------------------


def test_every_page_ships_the_stylesheet_and_a_tally():
    """Linked or inlined, but never absent -- and always with a capture strip."""
    linked = chrome.page("T", "<main></main>")
    assert '<link rel="stylesheet" href="/static/soccer-cam.css">' in linked
    assert 'class="tally"' in linked

    inlined = chrome.head("T", webfonts=False, inline_css=True)
    assert "<link " not in inlined, "a field page must issue no extra request"
    assert "--color-accent" in inlined, "the sheet must actually be inlined"


def test_tally_never_invents_a_capture_state():
    """A bad value must not let the strip claim the appliance is recording."""
    assert 'data-state="idle"' in chrome.tally("nonsense")
    assert 'data-state="idle"' in chrome.tally("")
    for good in ("idle", "armed", "recording", "error"):
        assert f'data-state="{good}"' in chrome.tally(good)


def test_navigation_lists_the_places_you_go_on_purpose():
    """Status, Settings and Setup are destinations; seam calibration is not.

    Seam calibration stays out because it acts on a specific camera and is
    launched from that camera, not from a global menu. It carries a crumb
    instead so the page still says where you are.
    """
    assert [href for href, _ in chrome._NAV] == ["/", "/config", "/setup"]

    for path in ("/", "/config", "/setup"):
        bar = chrome.topbar(path)
        assert f'href="{path}" aria-current="page"' in bar
        assert bar.count("aria-current") == 1, "exactly one current page"

    task = chrome.topbar(crumb="Seam calibration")
    assert "aria-current" not in task, "a task page marks no nav item current"
    assert '<span class="crumb">Seam calibration</span>' in task


def test_topbar_nav_position_does_not_move_between_pages():
    """Nav and crumb share one right-hand group, so nav never shifts."""
    for bar in (chrome.topbar("/"), chrome.topbar(crumb="Setup")):
        assert '<div class="topbar-end">' in bar
        assert bar.index("topbar-end") < bar.index("topnav")


def test_every_class_chrome_emits_is_styled():
    """Markup must not reference a class the stylesheet never defines.

    `.topbar-end` was added to the topbar with no base rule, so nav and crumb
    stacked vertically at every width until a phone screenshot showed it.
    """
    markup = "".join(
        [
            chrome.page("T", "<main></main>", crumb="C"),
            chrome.topbar("/config", crumb="Setup"),
            chrome.notice_page("T", "H", "<p>b</p>", tone="ok"),
            chrome.notice_page("T", "H", "<p>b</p>", tone="bad"),
        ]
    )
    used = set(re.findall(r'class="([^"]+)"', markup))
    classes = {c for group in used for c in group.split()}

    css = _text(SHEET)
    styled = set(re.findall(r"\.([a-z][a-z0-9-]*)", css))
    unstyled = sorted(c for c in classes if c not in styled)
    assert not unstyled, f"chrome emits unstyled classes: {unstyled}"


def test_mobile_collapses_the_rail_instead_of_stacking_it():
    """On a phone the rail is a horizontal strip, not a wall of links.

    Stacked vertically it put 16 section links above the first field on
    Settings -- a screen of scrolling before any content.
    """
    css = _text(SHEET)
    mobile = css[css.index("@media (max-width: 900px)") :]
    rail = mobile[mobile.index(".rail {") : mobile.index(".topbar-inner")]
    assert "overflow-x: auto" in rail
    assert "display: flex" in mobile, "rail items must lay out in a row"


def test_touch_targets_are_at_least_44px():
    """Buttons and inputs must be thumb-sized, and 16px so iOS does not zoom."""
    css = _text(SHEET)
    phone = css[css.index("@media (max-width: 767px)") :]
    assert phone.count("min-height: 44px") >= 2
    assert phone.count("font-size: 16px") >= 2
