"""Contract test: every TTT call soccer-cam makes exists in TTT's API.

``TTTApiClient`` (``video_grouper/api_integrations/ttt_api.py``) is read with the
AST, so each (HTTP method, path) comes from the call site itself: the
``self._request(...)`` / ``self._http.*`` call and the f-string URL it is given.
Each one must be an operation in TTT's API, and where the client sends a literal
JSON body, its keys must be fields that operation accepts.

A route TTT renames, removes or never built fails here instead of failing
silently on a camera manager's machine.

``ttt_api_subset.json`` is cut from TTT's OpenAPI schema and holds only what this
test needs about the paths this client already calls: which methods exist on
them and the request-body field names. TTT's full API is not open source, so its
schema is never vendored here. Refresh it from a team-tech-tools checkout next to
this one (then re-run this test and update ``KNOWN_MISSING``)::

    git -C ../team-tech-tools fetch origin
    git -C ../team-tech-tools show origin/development:openapi.json > openapi.json
    uv run python -m tests.contracts.test_ttt_api_contract --refresh openapi.json
    rm openapi.json
"""

from __future__ import annotations

import ast
import functools
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLIENT = ROOT / "video_grouper" / "api_integrations" / "ttt_api.py"
SUBSET = Path(__file__).with_name("ttt_api_subset.json")

_HTTP_VERBS = {"get", "post", "put", "patch", "delete"}

# (METHOD, normalized path) -> why it is missing from TTT. Every entry has been
# checked against TTT's backend source. test_known_lists_are_current fails once
# an entry exists in TTT or is no longer called, so this list only shrinks.
_PIPELINE_QUESTIONS = (
    "never built in TTT (planned after the core loop, decided 2026-11-27), and dead "
    "here: TTTQuestionService, the only caller, is never instantiated"
)
_DEVICE_AUTH = (
    "added in mblakley/team-tech-tools#173 (soccer-cam device sign-in), not yet on "
    "development; refresh the subset once it merges"
)

KNOWN_MISSING: dict[tuple[str, str], str] = {
    ("POST", "/api/device-auth/token"): _DEVICE_AUTH,
    ("POST", "/api/device-auth/refresh"): _DEVICE_AUTH,
    ("POST", "/api/pipeline-questions"): _PIPELINE_QUESTIONS,
    ("GET", "/api/pipeline-questions/{}"): _PIPELINE_QUESTIONS,
    ("PATCH", "/api/pipeline-questions/{}/respond"): _PIPELINE_QUESTIONS,
}

# (METHOD, normalized path, body key) -> why TTT does not accept the key.
KNOWN_EXTRA_BODY_KEYS: dict[tuple[str, str, str], str] = {
    ("PATCH", "/api/highlights/{}", "file_path"): (
        "TTT dropped highlight_reels.file_path in the media redesign; "
        "HighlightReelUpdate ignores it, so complete_highlight sends a dead field"
    ),
}


@dataclass(frozen=True)
class Call:
    function: str
    method: str
    base: str  # "ttt" | "supabase"
    path: str  # normalized: every path parameter is "{}"
    body_keys: frozenset[str] | None = field(default=None, compare=False)
    line: int = field(default=0, compare=False)


def _normalize(path: str) -> str:
    return re.sub(r"\{[^}]*\}", "{}", path.split("?", 1)[0])


def _url_template(node: ast.expr) -> tuple[str, str] | None:
    """(base, path) for an f-string URL rooted at one of the client's base URLs."""
    if not isinstance(node, ast.JoinedStr) or not node.values:
        return None
    head = node.values[0]
    if not (
        isinstance(head, ast.FormattedValue)
        and isinstance(head.value, ast.Attribute)
        and isinstance(head.value.value, ast.Name)
        and head.value.value.id == "self"
    ):
        return None
    base = {"api_base_url": "ttt", "supabase_url": "supabase"}.get(head.value.attr)
    if base is None:
        return None
    path = ""
    for part in node.values[1:]:
        path += str(part.value) if isinstance(part, ast.Constant) else "{}"
    return base, _normalize(path)


def _dict_keys(node: ast.expr) -> set[str] | None:
    if isinstance(node, ast.Dict) and all(
        isinstance(k, ast.Constant) and isinstance(k.value, str) for k in node.keys
    ):
        return {k.value for k in node.keys}  # type: ignore[union-attr]
    return None


def _request_calls(fn: ast.FunctionDef) -> list[ast.Call]:
    calls = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        target = node.func
        if (
            target.attr == "_request"
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
        ):
            calls.append(node)
        elif (
            (target.attr == "request" or target.attr in _HTTP_VERBS)
            and isinstance(target.value, ast.Attribute)
            and target.value.attr == "_http"
        ):
            calls.append(node)
    return calls


def _function_calls(fn: ast.FunctionDef) -> list[Call]:
    urls: dict[str, tuple[str, str]] = {}
    bodies: dict[str, set[str] | None] = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                template = _url_template(node.value)
                if template:
                    urls[target.id] = template
                bodies[target.id] = _dict_keys(node.value)
            elif (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and isinstance(target.slice, ast.Constant)
                and isinstance(target.slice.value, str)
                and bodies.get(target.value.id) is not None
            ):
                bodies[target.value.id].add(target.slice.value)  # type: ignore[union-attr]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                bodies[node.target.id] = _dict_keys(node.value)

    out = []
    for call in _request_calls(fn):
        verb = call.func.attr  # type: ignore[attr-defined]
        if verb in ("_request", "request"):
            method_node, url_node = call.args[0], call.args[1]
            assert isinstance(method_node, ast.Constant), (fn.name, call.lineno)
            method = str(method_node.value).upper()
        else:
            method, url_node = verb.upper(), call.args[0]
        if isinstance(url_node, ast.Name):
            template = urls.get(url_node.id)
        else:
            template = _url_template(url_node)
        assert template is not None, f"{fn.name}:{call.lineno}: unresolved URL"
        body_keys: set[str] | None = None
        for kw in call.keywords:
            if kw.arg == "json":
                if isinstance(kw.value, ast.Name):
                    body_keys = bodies.get(kw.value.id)
                else:
                    body_keys = _dict_keys(kw.value)
        out.append(
            Call(
                function=fn.name,
                method=method,
                base=template[0],
                path=template[1],
                body_keys=frozenset(body_keys) if body_keys is not None else None,
                line=call.lineno,
            )
        )
    return out


def _client_methods() -> list[ast.FunctionDef]:
    tree = ast.parse(CLIENT.read_text(encoding="utf-8"))
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "TTTApiClient"
    )
    # _request() forwards whatever method/url it is given.
    return [
        n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name != "_request"
    ]


@functools.cache
def extract_calls() -> tuple[Call, ...]:
    return tuple(call for fn in _client_methods() for call in _function_calls(fn))


def _ttt_calls() -> list[tuple[str, str]]:
    return sorted({(c.method, c.path) for c in extract_calls() if c.base == "ttt"})


@functools.cache
def _operations() -> dict[tuple[str, str], tuple[str, ...] | None]:
    """(METHOD, path) -> accepted body fields, or None when TTT's body schema is
    absent or free-form."""
    data = json.loads(SUBSET.read_text(encoding="utf-8"))
    out: dict[tuple[str, str], tuple[str, ...] | None] = {}
    for key, body in data["operations"].items():
        method, path = key.split(" ", 1)
        out[(method, path)] = tuple(body) if body is not None else None
    return out


# --------------------------------------------------------------------------
# Building the subset from TTT's OpenAPI schema (--refresh)
# --------------------------------------------------------------------------


def _resolve(schema: dict, node: dict) -> dict:
    while "$ref" in node:
        node = schema["components"]["schemas"][node["$ref"].rsplit("/", 1)[-1]]
    return node


def _body_properties(schema: dict, op: dict) -> list[str] | None:
    content = op.get("requestBody", {}).get("content", {})
    body = content.get("application/json", {}).get("schema")
    if body is None:
        return None
    node = _resolve(schema, body)
    props: set[str] = set()
    for variant in node.get("anyOf") or node.get("oneOf") or [node]:
        variant = _resolve(schema, variant)
        if variant.get("type") == "null":
            continue
        if "properties" not in variant or variant.get("additionalProperties"):
            return None
        props |= set(variant["properties"])
    return sorted(props)


def build_subset(openapi: dict, ttt_commit: str) -> dict:
    """Cut TTT's OpenAPI schema down to the paths this client calls."""
    called = set(_ttt_calls())
    called_paths = {path for _, path in called}
    operations: dict[str, list[str] | None] = {}
    for raw_path, item in openapi["paths"].items():
        path = _normalize(raw_path)
        if path not in called_paths:
            continue
        for method, op in item.items():
            if method in _HTTP_VERBS:
                key = (method.upper(), path)
                operations[f"{key[0]} {key[1]}"] = (
                    _body_properties(openapi, op) if key in called else None
                )
    return {
        "ttt_commit": ttt_commit,
        "note": "Generated by tests/contracts/test_ttt_api_contract.py --refresh; "
        "do not edit by hand.",
        "operations": dict(sorted(operations.items())),
    }


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


def test_every_http_call_in_the_client_is_extracted():
    """Guards the extractor: it must see every request the client issues, so the
    contract can't pass by extracting nothing."""
    source = CLIENT.read_text(encoding="utf-8")
    issued = re.findall(
        r"self\._request\(|self\._http\.(?:request|get|post|put|patch|delete)\(",
        source,
    )
    # Minus the one inside _request() itself, which forwards whatever it is given.
    assert len(extract_calls()) == len(issued) - 1
    assert len(_ttt_calls()) > 50


def test_client_never_calls_supabase():
    """Soccer-cam knows only TTT's API (decided 2026-11-27). Sign-in and refresh
    go through TTT's /api/device-auth/*; Supabase is TTT's implementation detail."""
    assert not [c for c in extract_calls() if c.base == "supabase"]
    assert "supabase" not in CLIENT.read_text(encoding="utf-8").lower()


@pytest.mark.parametrize(("method", "path"), _ttt_calls())
def test_ttt_route_exists(method, path):
    if (method, path) in KNOWN_MISSING:
        pytest.xfail(KNOWN_MISSING[(method, path)])
    ops = _operations()
    callers = sorted(
        c.function for c in extract_calls() if (c.method, c.path) == (method, path)
    )
    same_path = sorted(m for (m, p) in ops if p == path)
    assert (method, path) in ops, (
        f"{method} {path} (called by {callers}) is not in TTT's API"
        + (f"; that path has {same_path}" if same_path else "")
        + ". If TTT changed, refresh ttt_api_subset.json (see module docstring)."
    )


def _body_cases():
    for c in extract_calls():
        if c.base == "ttt" and c.body_keys:
            yield pytest.param(c, id=f"{c.function}:{c.method} {c.path}")


@pytest.mark.parametrize("call", list(_body_cases()))
def test_json_body_keys_are_accepted(call: Call):
    ops = _operations()
    if (call.method, call.path) not in ops:
        pytest.skip("route missing; reported by test_ttt_route_exists")
    allowed = ops[(call.method, call.path)]
    if allowed is None:
        pytest.skip("TTT's request schema is free-form")
    extra = {
        key
        for key in call.body_keys or ()
        if key not in allowed
        and (call.method, call.path, key) not in KNOWN_EXTRA_BODY_KEYS
    }
    assert not extra, (
        f"{call.function} sends {sorted(extra)} to {call.method} {call.path}; "
        f"TTT accepts {list(allowed)}"
    )


def test_known_lists_are_current():
    ops = _operations()
    calls = set(_ttt_calls())
    exists_now = sorted(key for key in KNOWN_MISSING if key in ops)
    assert not exists_now, f"now in TTT; remove from KNOWN_MISSING: {exists_now}"
    not_called = sorted(key for key in KNOWN_MISSING if key not in calls)
    assert not not_called, f"no longer called; remove from KNOWN_MISSING: {not_called}"

    sent = {
        (c.method, c.path, key)
        for c in extract_calls()
        if c.base == "ttt"
        for key in c.body_keys or ()
    }
    stale = sorted(
        key
        for key in KNOWN_EXTRA_BODY_KEYS
        if key not in sent or key[2] in (ops.get(key[:2]) or ())
    )
    assert not stale, f"remove from KNOWN_EXTRA_BODY_KEYS: {stale}"


def test_subset_holds_only_paths_the_client_calls():
    """TTT's API is closed source: the vendored subset may describe only paths
    this open-source client already names."""
    called_paths = {path for _, path in _ttt_calls()}
    assert {path for _, path in _operations()} <= called_paths


if __name__ == "__main__":
    if sys.argv[1:2] != ["--refresh"] or len(sys.argv) != 3:
        sys.exit("usage: --refresh <path to TTT's openapi.json>")
    commit = subprocess.run(
        [
            "git",
            "-C",
            str(ROOT.parent / "team-tech-tools"),
            "rev-parse",
            "origin/development",
        ],
        capture_output=True,
        text=True,
    ).stdout.strip()
    openapi = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    subset = build_subset(openapi, commit or "unknown")
    with SUBSET.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(subset, indent=2) + "\n")
    print(f"wrote {SUBSET} ({len(subset['operations'])} operations)")
