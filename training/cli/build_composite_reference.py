"""Composite viewport reference — AutoCam is the default truth; GT overrides by precision.

Implements THE COMPOSITE REFERENCE STANDARD (DECISIONS.md 2026-07-26 (w),
evolving ``build_viewport_benchmark.py``'s best-of tiers): the per-frame
reference is a precedence ladder

- **tier ``ball``** — GT ball detections (``ball_labels.jsonl`` in the game
  dir): the most precise source (single coordinate), outranks everything;
- **tier ``view``** — human viewport labels (``--viewport-set-dir`` sets in the
  scoreboard's ``labels.json`` format; repeatable, later sets take precedence
  within the tier);
- **tier ``ac``** — a VALIDATED AutoCam reference (``--ac-source``), densely
  covering the game: a ``camera_path/1`` JSON, an aim jsonl (rows ``{f, x, y}``
  keyed by global/combined frame), or a validated seg-keyed viewport jsonl —
  the format is auto-detected.

Override gates (pre-registered, the (w) gate; tier-1 WINDOWED per the
EXP-OP-13 amendment): AutoCam's viewport TRAILS the ball by ~1 s (measured
lag-scan minimum at +20 frames on spc ball GT), so **ball GT overrides AC only
where ``min(|ball_x - ac_x(f+k)|) over k in [-20, +20] > 600 px`` — a trailing
follower stays within its window; a park on the wrong object does not.** The
tier-2 (viewport GT) gate stays INSTANTANEOUS at 600 px. Where a GT source
agrees within its gate, AC's dense signal stands and the row is marked
``corroborated: true``. Ball GT outranks viewport GT wherever both exist on a
frame (its agree/diverge verdict wins). Human ``not_visible``/``out_of_play``
frames are removed entirely — nobody should be scored there.

Hard-fails (CLAUDE.md rule 8) on: a missing/empty/unrecognizable AC source; the
BANNED legacy Reolink viewport class (EXP-OP-05: the legacy seg-keyed
``autocam_viewport.jsonl`` on 7680-wide games decorrelates from GT, corr ~0.28
vs the fresh aim's 0.773; condemned again by direct ball-GT evidence —
EXP-OP-13's lag scan is flat ~1600 px at ALL lags on spc and fair); and zero
output rows.

Output: ``composite_reference/1`` JSONL — one ``_meta`` provenance line
(EXP-OP-05 correction: the AC reference must name its source), then per frame
``{g, x, y, tier: "ball"|"view"|"ac", src}`` (+ ``corroborated: true`` where a
GT point agreed with AC within the gate). Consumed by
``training/cli/operator_scoreboard.py --composite``. Idempotent (--force to
rebuild).

    python -m training.cli.build_composite_reference \
      --game-dir "F:/Heat_2012s/2026.05.31 - vs Spencerport gold 2 (away)" \
      --viewport-set-dir D:/training_data/viewport_label/spc_viewport_worst \
      --ac-source "F:/.../autocam_aim.jsonl" \
      --out G:/ballresearch/operator/spc_composite.jsonl
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from training.cli.build_viewport_label_queue import load_cams
from training.data_prep import distill_dataset as dd

SCHEMA = "composite_reference/1"

# The (w) gate: a GT source overrides the AC tier only beyond this x-divergence.
DIVERGENCE_GATE_PX = 600.0

# EXP-OP-13 amendment: AutoCam's viewport TRAILS the ball by ~1 s (lag-scan
# minimum at +20 frames on spc ball GT), so the tier-1 (ball GT) gate is
# WINDOWED: ball GT overrides AC only where min(|ball_x - ac_x(f+k)|) over
# k in [-WINDOW, +WINDOW] exceeds the gate — a trailing follower stays within
# its window; a park on the wrong object does not. The tier-2 (viewport GT)
# gate stays instantaneous.
BALL_GATE_WINDOW_FRAMES = 20

# EXP-OP-05 ban: on Reolink-width games the legacy seg-keyed viewport jsonl is
# the documented-BAD reference class (corr ~0.28 vs GT; EXP-OP-13's ball-GT lag
# scan is flat ~1600 px at ALL lags on spc and fair).
BANNED_LEGACY_SRC_W = 7680


def _fail(msg: str) -> None:
    raise SystemExit(f"build_composite_reference: {msg}")


def _ban_msg(path: Path) -> str:
    return (
        f"BANNED legacy Reolink viewport reference: {path} -- EXP-OP-05: on "
        f"{BANNED_LEGACY_SRC_W}-wide (Reolink) games the legacy seg-keyed "
        "autocam_viewport.jsonl decorrelates from GT (corr ~0.28 vs the fresh "
        "aim's 0.773; EXP-OP-13 ball-GT lag scan flat ~1600 px at ALL lags on "
        "spc and fair); use autocam_aim.jsonl, a fresh CLI aim, or a validated "
        "camera_path/1 artifact"
    )


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_ac_source(
    path: Path, offs: dict[str, int], src_w: int
) -> tuple[dict[int, tuple[float, float]], str]:
    """Auto-detect + load the validated AC reference as ``{g: (x, y)}``.

    Formats: ``camera_path/1`` JSON (via :func:`load_cams`; the constant-pad
    head ``[0, g_start)`` is NOT reference data and is excluded), an aim jsonl
    (rows ``{f, x, y}``, f = global/combined frame), or a seg-keyed viewport
    jsonl (rows ``{seg, f, x, y}``, mapped through ``offs``). Hard-fails on a
    missing/empty/unrecognizable source and on the banned legacy Reolink
    viewport class (EXP-OP-05): a seg-keyed viewport jsonl — or any file named
    ``autocam_viewport.jsonl`` — on a 7680-wide game.
    """
    if not path.exists():
        _fail(f"AC source does not exist: {path} -- the composite REQUIRES one")
    if path.name == "autocam_viewport.jsonl" and src_w == BANNED_LEGACY_SRC_W:
        _fail(_ban_msg(path))
    text = path.read_text(encoding="utf-8", errors="ignore")
    if not text.strip():
        _fail(f"AC source is empty: {path}")

    obj = None
    if text.lstrip().startswith("{"):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            obj = None  # multi-line JSONL, not a single JSON document
    if isinstance(obj, dict):
        if obj.get("schema") == "camera_path/1":
            cams, g0 = load_cams(str(path))
            ac = {
                g: (float(cams[g][0]), float(cams[g][1])) for g in range(g0, len(cams))
            }
            if not ac:
                _fail(f"camera_path/1 AC source has zero planned frames: {path}")
            return ac, "camera_path"
        if not ({"f", "seg"} & obj.keys()):
            # a one-row jsonl ALSO parses as a single dict — only a document
            # that is neither a campath nor a data row is unrecognizable
            _fail(
                f"unrecognizable AC source (single JSON document that is not a "
                f"camera_path/1 artifact): {path}"
            )

    fmt: str | None = None
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or "_meta" in ln[:12]:
            continue
        try:
            row = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        fmt = "viewport" if "seg" in row else "aim" if "f" in row else None
        break
    if fmt is None:
        _fail(f"unrecognizable AC source format: {path}")

    if fmt == "viewport":
        if src_w == BANNED_LEGACY_SRC_W:
            _fail(_ban_msg(path))
        ac = dd.load_viewport(path, offs)
        if not ac:
            _fail(f"viewport AC source matched no game.json segment: {path}")
        return ac, "viewport"

    ac = {}
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or "_meta" in ln[:12]:
            continue
        try:
            row = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or "f" not in row:
            continue
        if row.get("x") is None or row.get("y") is None:
            continue
        ac[int(row["f"])] = (float(row["x"]), float(row["y"]))
    if not ac:
        _fail(f"aim AC source has zero usable rows: {path}")
    return ac, "aim"


def load_view_set(set_dir: Path) -> dict[int, tuple[float, float | None]]:
    """Human viewport labels of one set: ``{global_frame: (fx, fy | None)}``
    from ``labels.json`` rows with ``action == "view"`` and a focal ``fx``.
    Hard-fails on a missing file or an empty usable set (rule 8)."""
    p = set_dir / "labels.json"
    if not p.exists():
        _fail(f"missing labels.json in {set_dir}")
    out: dict[int, tuple[float, float | None]] = {}
    for r in json.loads(p.read_text(encoding="utf-8")):
        if r.get("action") == "view" and r.get("fx") is not None:
            fy = r.get("fy")
            out[int(r["frame_idx"])] = (
                float(r["fx"]),
                None if fy is None else float(fy),
            )
    if not out:
        _fail(f"{set_dir}: 0 usable viewport labels (action=='view' with fx)")
    return out


# ---------------------------------------------------------------------------
# Composition (the (w) precedence ladder)
# ---------------------------------------------------------------------------


def compose(
    ac: dict[int, tuple[float, float]],
    ac_src: str,
    balls: dict[int, tuple[float, float]],
    novis: set[int],
    views: list[tuple[str, dict[int, tuple[float, float | None]]]],
    gate_px: float = DIVERGENCE_GATE_PX,
    ball_window: int = BALL_GATE_WINDOW_FRAMES,
) -> tuple[list[dict], dict]:
    """The (w) ladder: ball > view > ac with the ``gate_px`` divergence gate.

    AC rows stand densely. A viewport-GT point on an AC-covered frame
    overrides only where ``|fx - ac_x| > gate_px`` (instantaneous). A ball-GT
    point's gate is WINDOWED (EXP-OP-13: AC trails the ball by ~1 s): it
    overrides only where ``min(|ball_x - ac_x(g+k)|)`` over ``k in
    [-ball_window, +ball_window]`` exceeds ``gate_px`` — a trailing follower
    stays within its window; a park on the wrong object does not. Within a
    gate the AC row stands, marked ``corroborated``. Ball GT outranks viewport
    GT wherever both exist (its verdict wins, including restoring a
    corroborated AC row over a view override). GT on frames AC does not cover
    stands on its own (GT is always highest priority; there is no AC x to gate
    against). ``not_visible`` frames are removed. Returns ``(rows sorted by g,
    counts)``.
    """

    def _ac_row(g: int) -> dict:
        x, y = ac[g]
        return {
            "g": int(g),
            "x": round(float(x), 1),
            "y": round(float(y), 1),
            "tier": "ac",
            "src": ac_src,
        }

    def _ball_within_window(g: int, bx: float) -> bool:
        return any(
            abs(bx - ac[k][0]) <= gate_px
            for k in range(g - ball_window, g + ball_window + 1)
            if k in ac
        )

    rows: dict[int, dict] = {g: _ac_row(g) for g in ac}
    # tier 2 (view): later sets take precedence within the tier
    for src_name, vlab in views:
        for g, (fx, fy) in vlab.items():
            if g in ac and abs(fx - ac[g][0]) <= gate_px:
                rows[g] = {**_ac_row(g), "corroborated": True}
            else:
                rows[g] = {
                    "g": int(g),
                    "x": round(float(fx), 1),
                    "y": None if fy is None else round(float(fy), 1),
                    "tier": "view",
                    "src": src_name,
                }
    # tier 1 (ball): outranks the view tier wherever both exist on a frame
    for g, (bx, by) in balls.items():
        if g in ac and _ball_within_window(g, bx):
            rows[g] = {**_ac_row(g), "corroborated": True}
        else:
            rows[g] = {
                "g": int(g),
                "x": round(float(bx), 1),
                "y": round(float(by), 1),
                "tier": "ball",
                "src": "ball_labels.jsonl",
            }
    removed = 0
    for g in novis:  # human "no findable ball": nobody is scored here
        if rows.pop(g, None) is not None:
            removed += 1
    ordered = [rows[g] for g in sorted(rows)]
    counts = {
        "ball": sum(1 for r in ordered if r["tier"] == "ball"),
        "view": sum(1 for r in ordered if r["tier"] == "view"),
        "ac": sum(1 for r in ordered if r["tier"] == "ac"),
        "corroborated": sum(1 for r in ordered if r.get("corroborated")),
        "novis_removed": removed,
    }
    return ordered, counts


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--game-dir",
        required=True,
        help="game dir with game.json (+ optional ball_labels.jsonl GT)",
    )
    ap.add_argument(
        "--viewport-set-dir",
        action="append",
        default=None,
        help="human viewport label set dir (labels.json); repeatable; later "
        "sets take precedence within the view tier",
    )
    ap.add_argument(
        "--ac-source",
        required=True,
        help="validated AC reference: camera_path/1 JSON, aim jsonl "
        "({f,x,y} global-frame keyed) or validated seg-keyed viewport jsonl "
        "-- auto-detected; legacy Reolink viewport jsonls are BANNED "
        "(EXP-OP-05)",
    )
    ap.add_argument("--out", required=True, help="composite_reference/1 jsonl path")
    ap.add_argument("--force", action="store_true", help="rebuild an existing --out")
    args = ap.parse_args(argv)

    gd = Path(args.game_dir)
    gjp = gd / "game.json"
    if not gjp.exists():
        _fail(f"missing game.json in {gd}")
    gj = json.loads(gjp.read_text(encoding="utf-8", errors="ignore"))
    out = Path(args.out)
    if out.exists() and not args.force:
        _fail(f"{out} exists (idempotent) -- use --force to rebuild")

    offs = dd.seg_offsets(gj["segments"])
    src_w = int(gj["segments"][0].get("w") or 0)
    src = Path(args.ac_source)
    ac, fmt = load_ac_source(src, offs, src_w)

    blp = gd / "ball_labels.jsonl"
    balls, novis = dd.load_human_labels(blp, offs) if blp.exists() else ({}, set())
    views = [
        (Path(s).name, load_view_set(Path(s))) for s in (args.viewport_set_dir or [])
    ]

    rows, counts = compose(ac, src.name, balls, novis, views)
    if not rows:
        _fail("composite has ZERO rows -- nothing to reference (rule 8)")

    meta = {
        "schema": SCHEMA,
        "game_dir": str(gd),
        "ac_source": str(src),
        "ac_format": fmt,
        "gate_px": DIVERGENCE_GATE_PX,
        "ball_gate_window_frames": BALL_GATE_WINDOW_FRAMES,
        "viewport_sets": [name for name, _ in views],
        "counts": counts,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"_meta": meta}) + "\n")
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    print(
        f"{gd.name}: composite {len(rows)} rows -- ball {counts['ball']}, "
        f"view {counts['view']}, ac {counts['ac']} "
        f"(corroborated {counts['corroborated']}), "
        f"novis removed {counts['novis_removed']} "
        f"[ac source: {fmt}] -> {out}"
    )


if __name__ == "__main__":
    main()
