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
<title>Soccer-Cam configuration</title>
<style>
body { font-family: system-ui, sans-serif; max-width: 760px; margin: 2em auto; padding: 0 1em; color: #222; }
h1 { font-size: 1.5rem; }
fieldset { margin: 1em 0; padding: 0.75rem 1rem; border: 1px solid #e5e7eb; border-radius: 6px; }
fieldset legend { font-weight: 600; color: #475569; padding: 0 0.5rem; }
.row { display: grid; grid-template-columns: 14rem 1fr; gap: 0.5rem 1rem; align-items: center; margin: 0.4rem 0; }
.row label { color: #475569; font-size: 0.9rem; }
input[type="text"], input[type="number"], input[type="password"] { padding: 0.4rem 0.6rem; font: inherit; border: 1px solid #cbd5e1; border-radius: 4px; }
input[type="checkbox"] { width: 1.1rem; height: 1.1rem; }
.btn { padding: 0.5rem 1rem; background: #2563eb; color: white; border: 0; border-radius: 4px; font-weight: 600; cursor: pointer; font-size: 0.95rem; }
.btn:hover { background: #1d4ed8; }
.flash-ok { padding: 0.6rem 1rem; background: #d1fae5; color: #065f46; border-radius: 4px; margin: 1rem 0; }
.flash-err { padding: 0.6rem 1rem; background: #fee2e2; color: #7f1d1d; border-radius: 4px; margin: 1rem 0; }
.flash-err li { margin-left: 1.2rem; }
.muted { color: #6b7280; font-size: 0.85rem; }
nav a { color: #2563eb; text-decoration: none; }
</style>
</head>
<body>
<nav><a href="/">&larr; Dashboard</a></nav>
<h1>Configuration</h1>
__FLASH__
<form method="post" action="/config">
__SECTIONS__
<button type="submit" class="btn">Save</button>
<p class="muted">Sensitive fields (passwords, secrets) leave blank to keep the existing value.</p>
</form>
</body>
</html>
"""


def _render_field(section_alias: str, field_name: str, field_info, value: Any) -> str:
    annotation = field_info.annotation
    input_type = _input_type_for(annotation, field_name)
    name = f"{section_alias}.{field_name}"
    label = html.escape(field_name)

    if input_type == "checkbox":
        checked = "checked" if value else ""
        # Hidden field so unchecked checkboxes still POST a value
        return (
            f'<div class="row"><label for="{name}">{label}</label>'
            f'<div><input type="hidden" name="{name}" value="false">'
            f'<input id="{name}" type="checkbox" name="{name}" value="true" {checked}>'
            f"</div></div>"
        )

    if field_name in _SENSITIVE_FIELDS:
        return (
            f'<div class="row"><label for="{name}">{label}</label>'
            f'<input id="{name}" type="password" name="{name}" value="" '
            f'placeholder="(unchanged)"></div>'
        )

    str_val = "" if value is None else html.escape(str(value))
    return (
        f'<div class="row"><label for="{name}">{label}</label>'
        f'<input id="{name}" type="{input_type}" name="{name}" value="{str_val}"></div>'
    )


def _render_section(section_alias: str, model: BaseModel) -> str:
    rows: list[str] = []
    for field_name, field_info in type(model).model_fields.items():
        if not _is_scalar_field(field_info.annotation):
            continue  # Lists / dicts / nested models out of scope for v1
        value = getattr(model, field_name)
        rows.append(_render_field(section_alias, field_name, field_info, value))
    if not rows:
        return ""
    return (
        f"<fieldset><legend>{html.escape(section_alias)}</legend>"
        + "\n".join(rows)
        + "</fieldset>"
    )


def _render_page(config: Config, flash: str = "") -> str:
    sections: list[str] = []
    for field_name, field_info in Config.model_fields.items():
        section_value = getattr(config, field_name)
        if not isinstance(section_value, BaseModel):
            continue  # cameras list etc. — out of scope for v1
        alias = field_info.alias or field_name.upper()
        sections.append(_render_section(alias, section_value))
    return _PAGE.replace("__FLASH__", flash).replace(
        "__SECTIONS__", "\n".join(filter(None, sections))
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
            flash = '<div class="flash-ok">Saved.</div>'
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
                '<div class="flash-err"><strong>Validation failed:</strong>'
                f"<ul>{err_items}</ul></div>"
            )
            return HTMLResponse(_render_page(config, flash=flash), status_code=422)

        try:
            save_config(new_config, config_path)
        except OSError as exc:
            logger.error("CONFIG_EDITOR: save failed: %s", exc)
            flash = (
                '<div class="flash-err">Could not write to '
                f"<code>{html.escape(str(config_path))}</code>: {html.escape(str(exc))}</div>"
            )
            return HTMLResponse(_render_page(new_config, flash=flash), status_code=500)

        return RedirectResponse(url="/config?saved=1", status_code=303)

    return router
