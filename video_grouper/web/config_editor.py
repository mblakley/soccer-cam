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
from video_grouper.web import chrome

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
        "license_key",
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
__CHROME_HEAD__
<style>
/* Page-specific: the dense settings grid, the on/off control and the sticky
   save bar. Everything else -- topbar, rail, buttons, inputs, panels, banners
   -- comes from static/soccer-cam.css. Do not restyle shared classes here. */

.headline {
  font-family: var(--font-headline);
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  font-size: clamp(32px, 5vw, 48px);
  line-height: 0.92;
  margin: 0 0 8px;
}
.lede { max-width: 56ch; margin: 0 0 24px; }


section.cfg {
  margin: 32px 0 0;
  padding-top: 24px;
  border-top: 1px solid var(--color-border);
  scroll-margin-top: 88px;
}

.sec-head { display: flex; align-items: baseline; gap: 16px; margin-bottom: 20px; }
.sec-title {
  font-family: var(--font-headline);
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  font-size: 24px;
  line-height: 1;
  margin: 0;
}

/* One settings row: label left, control right. */
.cfg-field {
  display: grid;
  grid-template-columns: minmax(160px, 200px) 1fr;
  gap: 24px;
  align-items: start;
  padding: 12px 0;
  border-bottom: 1px solid var(--color-border-light);
}
.cfg-field:last-child { border-bottom: none; }
.cfg-field-label {
  font-family: var(--font-mono);
  font-size: 11px;
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: var(--color-text-secondary);
  padding-top: 10px;
}
.cfg-field-control { min-width: 0; }
.cfg-field-control input {
  max-width: 460px;
  font-family: var(--font-mono);
  font-size: 13px;
}
.cfg-field-control input::placeholder {
  color: var(--color-text-muted);
  font-style: italic;
}

/* On/off control. The :has() selectors keep the highlight in sync with the
   checkbox on click; without them the state would look frozen until save. */
.toggle {
  display: inline-flex;
  align-items: center;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  background: var(--color-bg-input);
  cursor: pointer;
  user-select: none;
  width: max-content;
  overflow: hidden;
}
.toggle input { display: none; }
.toggle .pip {
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  padding: 7px 14px;
  color: var(--color-text-muted);
  transition:
    color var(--transition-fast),
    background-color var(--transition-fast);
}
.toggle:has(input:checked) .pip-on {
  background: var(--color-accent);
  color: var(--color-text-inverse);
}
.toggle:has(input:not(:checked)) .pip-off {
  background: var(--color-bg-hover);
  color: var(--color-text-primary);
}
.toggle:focus-within { outline: 2px solid var(--color-accent); outline-offset: 2px; }

/* A section whose `enabled` toggle is off hides the rest of its controls --
   there is nothing to decide until it is switched on. Driven by :has(), the
   same mechanism the toggle already uses for its own highlight, so it
   responds to the click rather than waiting for a save-and-reload.

   Hidden, not removed: display:none inputs still post, so switching a
   section off does not quietly discard what was configured in it. */
.cfg-off-note {
  display: none;
  margin: 0 0 12px;
  font-size: 12px;
  color: var(--color-text-muted);
}
section.cfg:has(.cfg-field--gate input[type="checkbox"]:not(:checked)) .cfg-gated {
  display: none;
}
section.cfg:has(.cfg-field--gate input[type="checkbox"]:not(:checked))
  .cfg-off-note {
  display: block;
}

/* Sticky save bar. */
.savebar {
  position: sticky;
  bottom: 0;
  margin: 48px -28px -80px;
  padding: 18px 28px;
  background: linear-gradient(180deg, transparent 0, var(--color-bg-primary) 32%);
  display: flex;
  align-items: center;
  gap: 16px;
  border-top: 1px solid var(--color-border);
  z-index: var(--z-base);
}

@media (max-width: 900px) {
  .cfg-field { grid-template-columns: 1fr; gap: 6px; }
  .cfg-field-label { padding-top: 0; }
  /* Stack the bar: side by side, the hint squeezed "Save changes" onto two
     lines and shrank the tap target. */
  .savebar {
    margin: 32px -16px 0;
    padding: 16px;
    flex-direction: column;
    align-items: stretch;
    gap: 8px;
  }
  .savebar .btn { width: 100%; }
}
</style>
</head>
<body>
__CHROME_TOPBAR__
<main class="shell shell--rail">
  <aside class="rail">
    <h2>Sections</h2>
    <ol>__RAIL__</ol>
  </aside>
  <div>
    <div class="page-header">
      <h1 class="headline">Settings</h1>
      <a class="btn btn-secondary btn-sm" href="/setup/welcome">Run setup</a>
    </div>
    <p class="lede">Every setting the pipeline persists. Passwords and secrets show blank &mdash; leave them empty to keep the stored value.</p>
    <p class="lede">YouTube <code>client_secret.json</code> + token are managed on <a href="/#youtube">Status</a> &mdash; binary credential files do not fit this form.</p>
    __FLASH__
    <form method="post" action="/config">
      __SECTIONS__
      <div class="savebar">
        <button type="submit" class="btn">Save changes</button>
        <span class="hint">Writes to config.ini. The service reloads on its next tick.</span>
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
        # `enabled` gates the rest of its section -- see _render_section.
        gate = " cfg-field--gate" if field_name == "enabled" else ""
        # Visual highlight is driven by `.toggle:has(input:checked)` in CSS,
        # so it stays accurate when the user clicks; we don't need a render-
        # time class. Hidden input ensures unchecked posts a value at all.
        return (
            f'<div class="cfg-field{gate}">'
            f'<label class="cfg-field-label" for="{name}">{label}</label>'
            f'<div class="cfg-field-control">'
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
            f'<div class="cfg-field">'
            f'<label class="cfg-field-label" for="{name}">{label}</label>'
            f'<div class="cfg-field-control">'
            f'<input id="{name}" type="password" name="{name}" value="" '
            f'placeholder="(unchanged)" autocomplete="new-password">'
            f"</div></div>"
        )

    str_val = "" if value is None else html.escape(str(value))
    return (
        f'<div class="cfg-field">'
        f'<label class="cfg-field-label" for="{name}">{label}</label>'
        f'<div class="cfg-field-control">'
        f'<input id="{name}" type="{input_type}" name="{name}" value="{str_val}">'
        f"</div></div>"
    )


def _render_section(section_alias: str, model: BaseModel) -> str:
    """Render one config section.

    Where a section has an ``enabled`` field, that toggle is hoisted out and
    the rest of the section is wrapped in ``.cfg-gated``, which CSS collapses
    while the toggle is off. Nine sections work this way; TTT alone hides
    twenty fields that mean nothing until it is switched on.

    The gated fields stay in the DOM rather than being dropped at render time,
    for two reasons: the section shows and hides the instant the toggle moves,
    with no round trip; and ``display: none`` inputs still post, so turning a
    section off never silently discards what was configured in it.
    """
    gate = ""
    rows: list[str] = []
    for field_name, field_info in type(model).model_fields.items():
        if not _is_scalar_field(field_info.annotation):
            continue  # Lists / dicts / nested models out of scope for v1
        value = getattr(model, field_name)
        rendered = _render_field(section_alias, field_name, field_info, value)
        if field_name == "enabled":
            gate = rendered
        else:
            rows.append(rendered)
    if not gate and not rows:
        return ""

    anchor = _section_anchor(section_alias)
    title = html.escape(section_alias)
    body = "\n".join(rows)
    if gate and rows:
        body = (
            f"{gate}"
            f'<p class="cfg-off-note">Turn this on to configure it.</p>'
            f'<div class="cfg-gated">{body}</div>'
        )
    elif gate:
        # Only the toggle is editable here -- the section's other fields are
        # lists or dicts, which this editor does not render. Nothing to gate,
        # so do not promise settings that are not there.
        body = gate
    return (
        f'<section class="cfg" id="{anchor}">'
        f'<header class="sec-head">'
        f'<h2 class="sec-title">{title}</h2>'
        f"</header>" + body + "</section>"
    )


def _render_page(config: Config, flash: str = "") -> str:
    sections: list[str] = []
    rail: list[str] = []
    for field_name, field_info in Config.model_fields.items():
        section_value = getattr(config, field_name)
        if not isinstance(section_value, BaseModel):
            continue  # cameras list etc. — out of scope for v1
        alias = field_info.alias or field_name.upper()
        # Skip sections that have no scalar fields (avoid empty rail entries).
        rendered = _render_section(alias, section_value)
        if not rendered:
            continue
        sections.append(rendered)
        anchor = _section_anchor(alias)
        rail.append(
            f'<li><a data-anchor="{anchor}" href="#{anchor}">{html.escape(alias)}</a></li>'
        )
    return (
        _PAGE.replace("__CHROME_HEAD__", chrome.head("Settings"))
        .replace(
            "__CHROME_TOPBAR__",
            chrome.tally() + chrome.topbar("/config"),
        )
        .replace("__FLASH__", flash)
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
            flash = '<div class="banner banner--ok">Configuration saved. The service picks it up on its next tick.</div>'
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
                '<div class="banner banner--bad"><strong>Validation failed:</strong>'
                f"<ul>{err_items}</ul></div>"
            )
            return HTMLResponse(_render_page(config, flash=flash), status_code=422)

        try:
            save_config(new_config, config_path)
        except OSError as exc:
            logger.error("CONFIG_EDITOR: save failed: %s", exc)
            flash = (
                '<div class="banner banner--bad"><strong>Could not save</strong>Could not write to '
                f"<code>{html.escape(str(config_path))}</code>: {html.escape(str(exc))}</div>"
            )
            return HTMLResponse(_render_page(new_config, flash=flash), status_code=500)

        return RedirectResponse(url="/config?saved=1", status_code=303)

    return router
