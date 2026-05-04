"""Schema-driven config editor at ``/config``.

Walks the Pydantic ``Config`` model on each request, renders one
``<fieldset>`` per section with one input per scalar field, and on POST
validates with ``Config.model_validate(...)`` + persists with
``save_config(...)``. Replaces the PyQt6 config UI as users move to the
web app.

Scope: scalar fields (``str``, ``int``, ``float``, ``bool``,
``Optional[X]`` thereof). List + dict fields (per-team configs,
playlist maps, plugin signing keys) are intentionally skipped in v1 —
they need richer editors that ship with the wizard rebuild.
"""

from __future__ import annotations

import html
import logging
import typing
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ValidationError

from video_grouper.utils.config import Config, load_config, save_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Field classification
# ---------------------------------------------------------------------------

# Field names that always get redacted (write-only). The form posts back the
# previous value if the user leaves it blank, so they don't get clobbered to
# empty when editing a different field.
_SENSITIVE_FIELDS = frozenset(
    {
        "password",
        "client_secret",
        "anon_key",
        "service_role_key",
        "plugin_signing_key",
        "access_token",
        "refresh_token",
    }
)


def _is_scalar_field(annotation: Any) -> bool:
    """Return True for scalar types (incl. Optional[scalar])."""
    origin = typing.get_origin(annotation)
    if origin is None:
        return annotation in (str, int, float, bool, type(None))
    if origin is typing.Union:  # Optional[X] -> Union[X, None]
        return all(_is_scalar_field(a) for a in typing.get_args(annotation))
    return False


def _input_type_for(annotation: Any, name: str) -> str:
    """HTML <input type=...> attr for a Python field."""
    args = typing.get_args(annotation)
    base = annotation
    if typing.get_origin(annotation) is typing.Union:
        base = next((a for a in args if a is not type(None)), str)

    if name in _SENSITIVE_FIELDS:
        return "password"
    if base is bool:
        return "checkbox"
    if base in (int, float):
        return "number"
    return "text"


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Soccer-Cam · Configuration</title>
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
  --accent: #fb923c;            /* broadcast amber */
  --accent-glow: rgba(251,146,60,0.16);
  --signal-on: #22c55e;
  --signal-off: #6b7280;
  --signal-warn: #fbbf24;
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
  /* faint scanline texture for the broadcast feel */
  position: relative;
}
body::before {
  content: "";
  position: fixed; inset: 0;
  background-image: repeating-linear-gradient(
    0deg, transparent 0, transparent 2px, rgba(255,255,255,0.012) 2px, rgba(255,255,255,0.012) 3px);
  pointer-events: none;
  z-index: 1;
}

/* layout */
.shell {
  position: relative; z-index: 2;
  max-width: 1180px;
  margin: 0 auto;
  padding: 32px 28px 80px;
  display: grid;
  grid-template-columns: 220px 1fr;
  gap: 48px;
  animation: page-in 320ms ease-out both;
}
@keyframes page-in { from { opacity: 0; transform: translateY(6px); } }

/* topbar above the shell */
.topbar {
  position: relative; z-index: 2;
  border-bottom: 1px solid var(--rule);
  background: rgba(10,11,15,0.72);
  backdrop-filter: blur(8px);
}
.topbar-inner {
  max-width: 1180px;
  margin: 0 auto;
  padding: 14px 28px;
  display: flex; align-items: center; justify-content: space-between;
}
.brand {
  font-family: var(--display);
  font-weight: 700;
  letter-spacing: 0.18em;
  font-size: 18px;
  text-transform: uppercase;
}
.brand .dot { color: var(--accent); }
.crumb {
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--text-mute);
}
.crumb a { color: var(--text-mute); text-decoration: none; border-bottom: 1px solid transparent; }
.crumb a:hover { color: var(--text); border-bottom-color: var(--accent); }

/* left rail nav */
.rail {
  position: sticky; top: 32px;
  align-self: start;
}
.rail h2 {
  font-family: var(--mono); font-size: 10px; font-weight: 600;
  letter-spacing: 0.22em; text-transform: uppercase;
  color: var(--text-faint);
  margin: 0 0 14px;
}
.rail ol { list-style: none; padding: 0; margin: 0; counter-reset: rail; }
.rail li { counter-increment: rail; }
.rail a {
  display: flex; align-items: baseline; gap: 10px;
  padding: 8px 0;
  font-family: var(--display);
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  font-size: 14px;
  color: var(--text-mute);
  text-decoration: none;
  border-left: 2px solid transparent;
  padding-left: 12px;
  margin-left: -14px;
  transition: color 120ms ease, border-color 120ms ease;
}
.rail a::before {
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.12em;
  color: var(--text-faint);
  content: "§ " counter(rail, decimal-leading-zero);
  flex: 0 0 auto;
}
.rail a:hover { color: var(--text); }
.rail a.active { color: var(--text); border-left-color: var(--accent); }
.rail a.active::before { color: var(--accent); }

/* main */
main { min-width: 0; }
.headline {
  font-family: var(--display);
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  font-size: clamp(40px, 5vw, 56px);
  line-height: 0.92;
  margin: 0 0 8px;
}
.lede {
  color: var(--text-mute);
  max-width: 56ch;
  margin: 0 0 32px;
}
.lede code {
  font-family: var(--mono); font-size: 12px;
  background: var(--bg-elev); padding: 1px 6px; border: 1px solid var(--rule);
}

/* sections */
section.cfg {
  margin: 40px 0 0;
  padding-top: 28px;
  border-top: 1px solid var(--rule);
  scroll-margin-top: 24px;
  animation: section-in 360ms ease-out both;
}
section.cfg:nth-of-type(1) { animation-delay: 60ms; }
section.cfg:nth-of-type(2) { animation-delay: 120ms; }
section.cfg:nth-of-type(3) { animation-delay: 180ms; }
section.cfg:nth-of-type(4) { animation-delay: 220ms; }
section.cfg:nth-of-type(5) { animation-delay: 260ms; }
section.cfg:nth-of-type(n+6) { animation-delay: 300ms; }
@keyframes section-in { from { opacity: 0; transform: translateY(4px); } }

.sec-head {
  display: flex; align-items: baseline; gap: 16px;
  margin-bottom: 24px;
}
.sec-num {
  font-family: var(--mono);
  font-size: 11px; letter-spacing: 0.18em;
  color: var(--text-faint);
  text-transform: uppercase;
}
.sec-title {
  font-family: var(--display);
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  font-size: 24px; line-height: 1;
  margin: 0;
}
.sec-title .accent { color: var(--accent); }

/* fields */
.field {
  display: grid;
  grid-template-columns: minmax(160px, 200px) 1fr;
  gap: 24px;
  align-items: start;
  padding: 14px 0;
  border-bottom: 1px solid var(--rule);
}
.field:last-child { border-bottom: none; }
.field-label {
  font-family: var(--mono);
  font-size: 11px;
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: var(--text-mute);
  padding-top: 9px;
}
.field-control { min-width: 0; }

input[type="text"],
input[type="number"],
input[type="password"] {
  width: 100%; max-width: 460px;
  font: inherit;
  font-family: var(--mono);
  font-size: 13px;
  color: var(--text);
  background: var(--bg-input);
  border: 1px solid var(--rule);
  padding: 8px 12px;
  border-radius: 0;
  outline: none;
  transition: border-color 120ms ease, box-shadow 120ms ease;
}
input[type="text"]:focus,
input[type="number"]:focus,
input[type="password"]:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-glow);
}
input::placeholder { color: var(--text-faint); font-style: italic; }

/* boolean control: on / off pill. The :has() selectors keep the
   visual highlight in sync with the underlying checkbox state on
   click — without them the user would see the initial state frozen
   until the form was submitted and reloaded. */
.toggle {
  display: inline-flex; align-items: center;
  border: 1px solid var(--rule);
  background: var(--bg-input);
  padding: 0;
  cursor: pointer;
  user-select: none;
  width: max-content;
}
.toggle input { display: none; }
.toggle .pip {
  font-family: var(--mono);
  font-size: 10px; letter-spacing: 0.18em;
  text-transform: uppercase;
  padding: 7px 14px;
  color: var(--text-faint);
  transition: color 120ms ease, background 120ms ease;
}
.toggle .pip-off { padding-left: 14px; }
.toggle .pip-on { padding-right: 14px; }
.toggle:has(input[type="checkbox"]:checked) .pip-on {
  background: var(--accent); color: #1a0e02;
}
.toggle:has(input[type="checkbox"]:not(:checked)) .pip-off {
  background: var(--bg-elev); color: var(--text);
}

/* sticky save bar */
.savebar {
  position: sticky; bottom: 0;
  margin: 56px -28px -80px;
  padding: 18px 28px;
  background: linear-gradient(180deg, transparent 0, var(--bg-base) 32%);
  display: flex; align-items: center; gap: 18px;
  border-top: 1px solid var(--rule);
  z-index: 3;
}
.btn {
  font-family: var(--mono);
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  padding: 12px 24px;
  background: var(--accent);
  color: #1a0e02;
  border: 0;
  cursor: pointer;
  transition: transform 120ms ease, filter 120ms ease;
}
.btn:hover { filter: brightness(1.08); }
.btn:active { transform: translateY(1px); }
.savebar .hint {
  font-family: var(--mono); font-size: 11px; color: var(--text-faint);
  letter-spacing: 0.06em;
}

/* flash */
.flash { font-family: var(--mono); font-size: 12px; padding: 12px 16px; margin: 18px 0; border: 1px solid; }
.flash-ok { color: var(--signal-on); border-color: rgba(34,197,94,0.4); background: rgba(34,197,94,0.06); }
.flash-err { color: var(--signal-bad); border-color: rgba(244,63,94,0.4); background: rgba(244,63,94,0.06); }
.flash-err strong { display: block; margin-bottom: 6px; }
.flash-err ul { margin: 0; padding-left: 20px; }

@media (max-width: 880px) {
  .shell { grid-template-columns: 1fr; gap: 24px; }
  .rail { position: static; }
  .field { grid-template-columns: 1fr; gap: 6px; }
  .field-label { padding-top: 0; }
  .savebar { margin: 32px -28px 0; }
}
</style>
</head>
<body>
<header class="topbar">
  <div class="topbar-inner">
    <div class="brand">SOCCER<span class="dot">·</span>CAM</div>
    <div class="crumb"><a href="/">Dashboard</a> &nbsp;/&nbsp; Configuration</div>
  </div>
</header>
<div class="shell">
  <aside class="rail">
    <h2>Sections</h2>
    <ol>__RAIL__</ol>
  </aside>
  <main>
    <h1 class="headline">Configuration</h1>
    <p class="lede">Every persisted field across the pipeline. Sensitive entries (passwords, secrets) are blanked on render &mdash; leave empty on save to keep the stored value.</p>
    __FLASH__
    <form method="post" action="/config">
      __SECTIONS__
      <div class="savebar">
        <button type="submit" class="btn">Commit changes</button>
        <span class="hint">writes to config.ini and reloads on next service tick</span>
      </div>
    </form>
  </main>
</div>
<script>
// Highlight the rail entry for whichever section is closest to the viewport top.
(function () {
  const links = document.querySelectorAll('.rail a[data-anchor]');
  const sections = Array.from(document.querySelectorAll('section.cfg'));
  if (!links.length || !sections.length) return;
  const setActive = (id) => {
    links.forEach((a) => a.classList.toggle('active', a.dataset.anchor === id));
  };
  setActive(sections[0].id);
  const io = new IntersectionObserver(
    (entries) => {
      entries.forEach((e) => { if (e.isIntersecting) setActive(e.target.id); });
    },
    { rootMargin: '-40% 0px -55% 0px' }
  );
  sections.forEach((s) => io.observe(s));
  // Smooth-scroll on anchor click
  links.forEach((a) => a.addEventListener('click', (ev) => {
    const id = a.dataset.anchor;
    const el = document.getElementById(id);
    if (!el) return;
    ev.preventDefault();
    el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    history.replaceState(null, '', '#' + id);
  }));
})();
</script>
</body>
</html>
"""


def _section_anchor(alias: str) -> str:
    """DOM id for a section — used by both the rail nav and scroll target."""
    return "sec-" + alias.lower().replace(".", "-").replace("_", "-")


def _render_field(section_alias: str, field_name: str, field_info, value: Any) -> str:
    annotation = field_info.annotation
    input_type = _input_type_for(annotation, field_name)
    name = f"{section_alias}.{field_name}"
    label = html.escape(field_name.replace("_", " "))

    if input_type == "checkbox":
        is_on = bool(value)
        # Visual highlight is driven by `.toggle:has(input:checked)` in CSS,
        # so it stays accurate when the user clicks; we don't need a render-
        # time class. Hidden input ensures unchecked posts a value at all.
        return (
            f'<div class="field">'
            f'<label class="field-label" for="{name}">{label}</label>'
            f'<div class="field-control">'
            f'<label class="toggle">'
            f'<input type="hidden" name="{name}" value="false">'
            f'<input id="{name}" type="checkbox" name="{name}" value="true"'
            f"{' checked' if is_on else ''}>"
            f'<span class="pip pip-off">Off</span>'
            f'<span class="pip pip-on">On</span>'
            f"</label>"
            f"</div></div>"
        )

    if field_name in _SENSITIVE_FIELDS:
        return (
            f'<div class="field">'
            f'<label class="field-label" for="{name}">{label}</label>'
            f'<div class="field-control">'
            f'<input id="{name}" type="password" name="{name}" value="" '
            f'placeholder="(unchanged)" autocomplete="new-password">'
            f"</div></div>"
        )

    str_val = "" if value is None else html.escape(str(value))
    return (
        f'<div class="field">'
        f'<label class="field-label" for="{name}">{label}</label>'
        f'<div class="field-control">'
        f'<input id="{name}" type="{input_type}" name="{name}" value="{str_val}">'
        f"</div></div>"
    )


def _render_section(idx: int, section_alias: str, model: BaseModel) -> str:
    rows: list[str] = []
    for field_name, field_info in type(model).model_fields.items():
        if not _is_scalar_field(field_info.annotation):
            continue  # Lists / dicts / nested models out of scope for v1
        value = getattr(model, field_name)
        rows.append(_render_field(section_alias, field_name, field_info, value))
    if not rows:
        return ""
    anchor = _section_anchor(section_alias)
    title = html.escape(section_alias)
    return (
        f'<section class="cfg" id="{anchor}">'
        f'<header class="sec-head">'
        f'<span class="sec-num">§ {idx:02d}</span>'
        f'<h2 class="sec-title">{title}</h2>'
        f"</header>" + "\n".join(rows) + "</section>"
    )


def _render_page(config: Config, flash: str = "") -> str:
    sections: list[str] = []
    rail: list[str] = []
    idx = 0
    for field_name, field_info in Config.model_fields.items():
        section_value = getattr(config, field_name)
        if not isinstance(section_value, BaseModel):
            continue  # cameras list etc. — out of scope for v1
        alias = field_info.alias or field_name.upper()
        # Skip sections that have no scalar fields (avoid empty rail entries).
        rendered = _render_section(idx + 1, alias, section_value)
        if not rendered:
            continue
        idx += 1
        sections.append(rendered)
        anchor = _section_anchor(alias)
        rail.append(
            f'<li><a data-anchor="{anchor}" href="#{anchor}">{html.escape(alias)}</a></li>'
        )
    return (
        _PAGE.replace("__FLASH__", flash)
        .replace("__SECTIONS__", "\n".join(sections))
        .replace("__RAIL__", "\n".join(rail))
    )


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------


def _coerce(annotation: Any, raw: str | None) -> Any:
    """Convert a form-string back to the field's expected type."""
    args = typing.get_args(annotation)
    base = annotation
    nullable = False
    if typing.get_origin(annotation) is typing.Union:
        nullable = type(None) in args
        base = next((a for a in args if a is not type(None)), str)

    if raw is None:
        return None if nullable else ""
    if base is bool:
        return raw.lower() in ("true", "1", "yes", "on")
    # Pass numeric strings through unchanged when they don't parse — Pydantic
    # validation will produce a clear field-level error in the UI.
    if base is int:
        if raw == "":
            return None if nullable else 0
        try:
            return int(raw)
        except ValueError:
            return raw
    if base is float:
        if raw == "":
            return None if nullable else 0.0
        try:
            return float(raw)
        except ValueError:
            return raw
    return raw  # str


def _apply_form(current: Config, form_data: dict[str, str]) -> tuple[Config, list[str]]:
    """Build a new Config from the current state + form overrides.

    Returns (new_config, errors). errors is a list of human-readable
    strings on validation failure.
    """
    # Start from the current model, layer the form values on top per section.
    overrides: dict[str, dict[str, Any]] = {}
    for key, raw in form_data.items():
        if "." not in key:
            continue
        section, field_name = key.split(".", 1)
        overrides.setdefault(section, {})[field_name] = raw

    # Merge with the current config (so unedited sections + non-scalar
    # fields stay intact).
    payload: dict[str, Any] = {}
    for cfg_field, info in Config.model_fields.items():
        section_value = getattr(current, cfg_field)
        alias = info.alias or cfg_field.upper()
        if not isinstance(section_value, BaseModel):
            payload[cfg_field] = section_value
            continue
        merged = section_value.model_dump()
        section_overrides = overrides.get(alias, {})
        for fn, finfo in type(section_value).model_fields.items():
            if not _is_scalar_field(finfo.annotation):
                continue
            if fn in section_overrides:
                raw = section_overrides[fn]
                # Sensitive fields: blank means "keep existing"
                if fn in _SENSITIVE_FIELDS and raw == "":
                    continue
                merged[fn] = _coerce(finfo.annotation, raw)
        payload[alias] = merged
    payload["cameras"] = [c.model_dump() for c in current.cameras]

    try:
        new_config = Config.model_validate(payload, by_alias=True, by_name=True)
        return new_config, []
    except ValidationError as exc:
        errors = [
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        ]
        return current, errors


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def build_router(config_path: Path) -> APIRouter:
    """Build the FastAPI router for the config editor.

    The host-allowlist + Origin/Referer middleware on the parent app
    already handles the CSRF-side defenses; this router just renders
    the form and persists on POST.
    """
    router = APIRouter()

    @router.get("/config", response_class=HTMLResponse)
    def get_config(request: Request, saved: int = 0) -> HTMLResponse:
        config = load_config(config_path)
        if config is None:
            raise HTTPException(
                status_code=500,
                detail=f"Could not load config from {config_path}",
            )
        flash = ""
        if saved:
            flash = '<div class="flash flash-ok">CONFIG WRITTEN — RELOAD ON NEXT SERVICE TICK</div>'
        return HTMLResponse(_render_page(config, flash=flash))

    @router.post("/config", response_class=HTMLResponse)
    async def post_config(request: Request) -> HTMLResponse:
        form = await request.form()
        form_data = {k: v for k, v in form.items() if isinstance(v, str)}
        config = load_config(config_path)
        if config is None:
            raise HTTPException(
                status_code=500,
                detail=f"Could not load config from {config_path}",
            )
        new_config, errors = _apply_form(config, form_data)
        if errors:
            err_items = "".join(f"<li>{html.escape(e)}</li>" for e in errors)
            flash = (
                '<div class="flash flash-err"><strong>Validation failed:</strong>'
                f"<ul>{err_items}</ul></div>"
            )
            return HTMLResponse(_render_page(config, flash=flash), status_code=422)

        try:
            save_config(new_config, config_path)
        except OSError as exc:
            logger.error("CONFIG_EDITOR: save failed: %s", exc)
            flash = (
                '<div class="flash flash-err"><strong>WRITE FAILED</strong>Could not write to '
                f"<code>{html.escape(str(config_path))}</code>: {html.escape(str(exc))}</div>"
            )
            return HTMLResponse(_render_page(new_config, flash=flash), status_code=500)

        return RedirectResponse(url="/config?saved=1", status_code=303)

    return router
