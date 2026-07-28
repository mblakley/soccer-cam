"""W5 (P5) operator-imitation fit — search PlannerConfig knobs to CLONE the
composite reference's discipline, re-planning cached trajectories (DECISIONS (w)/(k)).

The planner is the ONLY thing that varies: each game's cached ``trajectory/2``
(points + tracker states + candidate dispersion), depth, and source dims are
fixed inputs, so a knob sample just re-runs :func:`plan_camera` (cheap, no
tracker) and scores the resulting camera path against that game's composite
reference with the SAME metric the scoreboard uses
(:func:`operator_metrics.capture_contain_stats` — capture@600 + planned-view
containment). Fit on one game, report the winner's score on the HELD-OUT games
(never fit and score the same game — DECISIONS (k), the generality rule).

Random search over bounded ranges around the shipped defaults (no gradients at
this dimensionality; the design's pick). Every run first HARD-FAILS unless the
harness reproduces the baseline: re-planning at the shipped PlannerConfig must
match the banked A0 campath's composite capture to 1e-6 (rule 8 — a fit built
on a broken scorer is worthless). ``--out`` gets the search trace + the winner;
NO config is promoted here — a winner goes through the standard scoreboard +
null gate separately.

    python -m training.cli.fit_planner \
      --game "spc|TRAJ|GAMEDIR|COMPOSITE|A0_CAMPATH" --game "fair|..." \
      --fit-on spc --samples 400 --seed 72 --out G:/ballresearch/operator/fit_spc.json

Fields are ``|``-separated (NOT ``:`` — Windows paths carry drive colons). The
5th field (a banked A0 ``camera_path/1``) is optional but recommended: it arms
the bit-identity gate (re-planning that game's trajectory at the shipped config
must reproduce it exactly, or the run hard-fails).
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

import numpy as np

from training.cli.operator_ladder import load_trajectory
from training.cli.operator_scoreboard import load_composite
from training.cli.plan_camera_path import depth_from_polygon
from training.world_model import operator_metrics as om
from video_grouper.inference.camera_planner import (
    PlannerConfig,
    plan_camera,
)

# The knobs the fit is allowed to move, each as (min, max) SEARCH bounds around
# the shipped defaults. Deliberately the framing/follow knobs only — NOT the
# hold-FSM gate (enable_hold stays off here; W2 owns that) and NOT fps.
SEARCH_SPACE: dict[str, tuple[float, float]] = {
    "pan_smoothing_min": (0.01, 0.10),
    "pan_smoothing_max": (0.06, 0.30),
    "pitch_smoothing": (0.02, 0.12),
    "velocity_ema": (0.1, 0.6),
    "lead_frames": (0.0, 16.0),
    "max_lead_room_fraction": (0.05, 0.35),
    "zoom_base_deg": (44.0, 52.0),
    "zoom_scale": (0.80, 1.00),
    "zoom_speed_gain_deg": (2.0, 14.0),
    "zoom_smoothing": (0.01, 0.10),
}


def _fail(msg: str) -> None:
    raise SystemExit(f"fit_planner: {msg}")


def load_game_inputs(spec: str) -> dict:
    """``name|trajectory|game_dir|composite[|a0_campath]`` -> the fixed re-plan
    inputs + the composite ref dict (+ optional identity-gate campath),
    hard-failing on any missing piece."""
    parts = spec.split("|")
    if len(parts) not in (4, 5):
        _fail(
            f"--game must be name|trajectory|game_dir|composite[|a0_campath], "
            f"got {spec!r}"
        )
    name, tp, gd, comp = parts[:4]
    a0_campath = parts[4] if len(parts) == 5 else None
    art = load_trajectory(Path(tp))
    gjp = Path(gd) / "game.json"
    if not gjp.exists():
        _fail(f"{name}: missing game.json in {gd}")
    gj = json.loads(gjp.read_text(encoding="utf-8", errors="ignore"))
    seg0 = gj["segments"][0]
    src_w, src_h = int(seg0.get("w") or 0), int(seg0.get("h") or 0)
    if src_w <= 0 or src_h <= 0:
        _fail(f"{name}: game.json segments[0] has no w/h")
    if not gj.get("field_polygon"):
        _fail(f"{name}: game.json has no field_polygon")
    polygon = np.asarray(gj["field_polygon"], float)
    rows, _meta = load_composite(Path(comp))
    refs: dict[int, tuple[float, float | None]] = {
        int(r["g"]): (float(r["x"]), None if r.get("y") is None else float(r["y"]))
        for r in rows
    }
    return {
        "name": name,
        "points": art["points"],
        "states": art["state"],
        "disp": art["disp"],
        "g_start": int(art["g_start"]),
        "src_w": src_w,
        "src_h": src_h,
        "polygon": polygon,
        "refs": refs,
        "a0_campath": a0_campath,
    }


def _replan(game: dict, cfg: PlannerConfig) -> list:
    pts = game["points"]
    return plan_camera(
        pts,
        src_w=game["src_w"],
        src_h=game["src_h"],
        depth01=depth_from_polygon(pts, game["polygon"]),
        states=game["states"],
        disp=game["disp"],
        config=cfg,
    )


# The saved trajectory/2 rounds points to 1 decimal (save_trajectory); the
# planner's finite-difference velocity x lead_frames amplifies that +-0.05 px
# into a couple px on the campath. So re-planning the SAVED trajectory matches
# the banked A0 only to this rounding floor, not to the bit. The gate stays
# tight enough to catch a gross harness bug (a wrong fps or depth shifts the
# campath by tens of px) while tolerating the save rounding.
IDENTITY_TOL_PX = 5.0


def verify_identity(game: dict) -> None:
    """Identity gate (rule 8): re-planning this game's trajectory at the shipped
    PlannerConfig must reproduce its banked A0 campath to within
    ``IDENTITY_TOL_PX`` (the trajectory-save rounding floor), or the fit is
    built on a scorer that does not match how A0 was made. No-op when no
    a0_campath was supplied (but then the fit is un-anchored — a warning)."""
    from training.cli.operator_ladder import load_campath_artifact

    cp = game.get("a0_campath")
    if not cp:
        print(f"NOTE {game['name']}: no A0 campath -> identity gate NOT armed")
        return
    banked = load_campath_artifact(Path(cp))["frames"]
    replan = _replan(game, PlannerConfig())
    if len(banked) != len(replan):
        _fail(
            f"identity gate {game['name']}: re-plan len {len(replan)} != banked "
            f"{len(banked)} -- trajectory/config mismatch"
        )
    a = np.asarray([list(fr) for fr in banked], float)
    b = np.asarray([list(fr) for fr in replan], float)
    md = float(np.max(np.abs(a - b))) if len(a) else 0.0
    if md > IDENTITY_TOL_PX:
        _fail(
            f"identity gate {game['name']}: re-plan diverges from banked A0 by "
            f"{md:.3g} px (> {IDENTITY_TOL_PX}) -- the harness does not reproduce A0"
        )
    print(
        f"identity gate {game['name']}: OK (re-plan ~= banked A0, max {md:.2g} px "
        f"<= {IDENTITY_TOL_PX} rounding floor)"
    )


def score_config(game: dict, cfg: PlannerConfig) -> dict:
    """Re-plan the game's fixed trajectory with ``cfg`` and score the campath
    against its composite (capture@600 + containment over the composite
    frames the campath covers)."""
    plan = _replan(game, cfg)
    g0 = game["g_start"]
    plans: dict[int, tuple[float, float | None, float | None]] = {
        g0 + i: (float(fr[0]), float(fr[1]), float(fr[2])) for i, fr in enumerate(plan)
    }
    refs = game["refs"]
    frames = [g for g in refs if g in plans]
    return om.capture_contain_stats(refs, plans, float(game["src_w"]), frames)


def _objective(st: dict) -> float:
    """Scalar to MAXIMISE: capture@600 plus containment when available. Both
    are 'be inside the reference' proxies; containment folds in the zoom."""
    if st["cap600"] is None:
        return -1.0
    c = st["cap600"]
    if st["contain"] is not None:
        c = 0.5 * (c + st["contain"])
    return float(c)


def sample_config(rng: np.random.Generator) -> PlannerConfig:
    kw = {k: float(rng.uniform(lo, hi)) for k, (lo, hi) in SEARCH_SPACE.items()}
    # keep the smoothing range ordered (min <= max) so the EMA stays sane
    if kw["pan_smoothing_min"] > kw["pan_smoothing_max"]:
        kw["pan_smoothing_min"], kw["pan_smoothing_max"] = (
            kw["pan_smoothing_max"],
            kw["pan_smoothing_min"],
        )
    return replace(PlannerConfig(), **kw)  # type: ignore[arg-type]


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--game",
        action="append",
        required=True,
        help="name:trajectory:game_dir:composite (repeatable)",
    )
    ap.add_argument("--fit-on", required=True, help="the --game name to FIT on")
    ap.add_argument("--samples", type=int, default=400)
    ap.add_argument("--seed", type=int, default=72)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    games = {g["name"]: g for g in (load_game_inputs(s) for s in args.game)}
    if args.fit_on not in games:
        _fail(f"--fit-on {args.fit_on!r} not among {sorted(games)}")
    holdouts = [n for n in games if n != args.fit_on]

    # identity gate (rule 8): re-planning at the shipped config must reproduce
    # each game's banked A0 campath before any search result is trusted.
    for g in games.values():
        verify_identity(g)
    base = {n: score_config(g, PlannerConfig()) for n, g in games.items()}
    for n, st in base.items():
        if st["cap600"] is None:
            _fail(f"identity check: game {n} scored no composite frames")
    base_obj = {n: _objective(base[n]) for n in games}
    print(
        "baseline (shipped PlannerConfig): "
        + ", ".join(f"{n} {base_obj[n]:.4f}" for n in games)
    )

    rng = np.random.default_rng(args.seed)
    fit_game = games[args.fit_on]
    base_fit = base_obj[args.fit_on]
    best_obj = base_fit
    best_cfg: PlannerConfig | None = None
    best_sample = -1
    for i in range(args.samples):
        cfg = sample_config(rng)
        obj = _objective(score_config(fit_game, cfg))
        if obj > best_obj:
            best_obj, best_cfg, best_sample = obj, cfg, i

    result: dict = {
        "fit_on": args.fit_on,
        "samples": args.samples,
        "seed": args.seed,
        "baseline_obj": base_obj,
        "search_space": SEARCH_SPACE,
    }
    if best_cfg is None:
        result["winner"] = None
        result["note"] = "no sample beat the shipped baseline on the fit game"
    else:
        knobs = {k: round(getattr(best_cfg, k), 4) for k in SEARCH_SPACE}
        fit_cells = {n: score_config(games[n], best_cfg) for n in games}
        result["winner"] = {
            "sample": best_sample,
            "fit_obj": round(best_obj, 5),
            "fit_gain": round(best_obj - base_fit, 5),
            "knobs": knobs,
            "cells": {n: fit_cells[n] for n in games},
            "holdout_gain": {
                n: round(_objective(fit_cells[n]) - base_obj[n], 5) for n in holdouts
            },
        }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1))
    w = result["winner"]
    if w is None:
        print(f"NO WINNER -- baseline holds on {args.fit_on}. -> {out}")
    else:
        print(
            f"WINNER sample {w['sample']}: fit {args.fit_on} "
            f"{base_obj[args.fit_on]:.4f} -> {w['fit_obj']:.4f} "
            f"(+{w['fit_gain']:.4f}); holdout gains "
            + ", ".join(f"{n} {g:+.4f}" for n, g in w["holdout_gain"].items())
            + f" -> {out}"
        )


if __name__ == "__main__":
    main()
