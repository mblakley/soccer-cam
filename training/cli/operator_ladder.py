"""W1 oracle-ladder runner (EXP-OP-01): replay banked inputs through the champion
camera chain and its hindsight oracles. CPU-only; every guard hard-fails
(CLAUDE.md rule 8 — no warnings in automated chains).

Subcommands:

- ``run-a``: cached ``fullgame_candidates/1`` dump -> the CURRENT champion chain
  (selector -> rerank/bridge/kalman -> upsample -> depth -> plan), the SAME code
  ``plan_camera_path`` runs (imported ``replay_champion_chain``, not forked).
  Baseline A; also the v1-family champion campath source.
- ``run-b``: human GT ball positions as the ONLY candidates (validate_tracker's
  ``build_frames`` GT branch: one Candidate per GT frame, score 1.0) ->
  ``track_ball`` -> ``upsample_track`` -> ``plan_camera``. B − A = the ceiling
  for ALL input work.
- ``run-c``: freeze-pan oracle — inside each AMENDED hold cluster (gap-40,
  n >= 4, GT fx swing < 200 px; EXP-OP-02 final correction) of a viewport label
  set, hold cx,cy at their values at the cluster span's first frame (hfov
  untouched). C − B prices play-state awareness.
- ``run-d``: lookahead pricing — (i) perfect-lead-Δ: the planner consumes the
  trajectory shifted forward, ``input[t] = points[min(t + round(Δ·fps), N-1)]``
  (nulls stay nulls); (ii) zero-phase: forward-then-backward EMA (alpha = the
  midpoint of the planner's pan_smoothing_min/max) of a planned campath given
  via ``--campath``. D − A per band = the lookahead build's price. No scipy.

Artifacts: run-a / run-b save dense trajectories as ``trajectory/2`` JSON
``{schema, g_start, fps, points, state, conf[, disp]}`` — the W2 seam: per-frame
tracker state 'T'/'C'/'M', emission-derived confidence, optional candidate
dispersion. Readers accept legacy ``trajectory/1`` too (neutral all-'T').
Campaths stay the existing ``camera_path/1`` (``save_camera_path``).

W2 stoppage-HOLD: ``run-a`` / ``run-b`` take ``--enable-hold`` (turns on the
LIVE/HOLD/REACQUIRE planner FSM) plus repeatable ``--hold-knob NAME=VALUE``
overrides for any ``PlannerConfig`` field (e.g. ``hold_entry_frames=30``).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from training.world_model import operator_metrics as om


def _fail(msg: str) -> None:
    raise SystemExit(f"operator_ladder: {msg}")


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


def _base_name(path: Path, *suffixes: str) -> str:
    """Filename with the first matching multi-part suffix stripped."""
    for suf in suffixes:
        if path.name.endswith(suf):
            return path.name[: -len(suf)]
    return path.stem


def load_game(game_dir: Path) -> dict:
    """``game.json`` with the fields every ladder rung needs — hard-fails on a
    missing file, missing ``field_polygon`` or missing ``segments``."""
    gj_path = game_dir / "game.json"
    if not gj_path.exists():
        _fail(f"missing game.json in {game_dir}")
    gj = json.loads(gj_path.read_text(encoding="utf-8", errors="ignore"))
    if not gj.get("field_polygon"):
        _fail(f"{game_dir}: game.json has no field_polygon")
    if not gj.get("segments"):
        _fail(f"{game_dir}: game.json has no segments")
    return gj


def _src_dims(gj: dict, game_dir: Path) -> tuple[int, int]:
    seg0 = gj["segments"][0]
    if "w" not in seg0 or "h" not in seg0:
        _fail(f"{game_dir}: game.json segments[0] has no w/h source dims")
    return int(seg0["w"]), int(seg0["h"])


def save_trajectory(
    path: Path,
    traj: list[tuple[float, float] | None],
    *,
    g_start: int,
    fps: float,
    state: list[str] | None = None,
    conf: list[float] | None = None,
    disp: list[float | None] | None = None,
) -> None:
    """Dense per-source-frame ball trajectory artifact: ``trajectory/2`` when the
    per-frame ``state`` channel is given (the W2 seam), legacy ``trajectory/1``
    without. Channel misalignment hard-fails; ``conf`` defaults to the neutral
    1.0-for-'T' / 0.0 otherwise."""
    payload: dict = {
        "schema": "trajectory/2" if state is not None else "trajectory/1",
        "g_start": int(g_start),
        "fps": float(fps),
        "points": [
            None if p is None else [round(float(p[0]), 1), round(float(p[1]), 1)]
            for p in traj
        ],
    }
    if state is not None:
        if len(state) != len(traj):
            _fail(f"{path}: {len(state)} states for {len(traj)} trajectory points")
        if conf is not None and len(conf) != len(traj):
            _fail(f"{path}: {len(conf)} conf values for {len(traj)} trajectory points")
        payload["state"] = [str(s) for s in state]
        payload["conf"] = (
            [round(float(c), 4) for c in conf]
            if conf is not None
            else [1.0 if s == "T" else 0.0 for s in state]
        )
        if disp is not None:
            if len(disp) != len(traj):
                _fail(
                    f"{path}: {len(disp)} disp values for {len(traj)} trajectory points"
                )
            payload["disp"] = [None if v is None else round(float(v), 1) for v in disp]
    path.write_text(json.dumps(payload))


def load_trajectory(path: Path) -> dict:
    """Load a ``trajectory/1`` OR ``trajectory/2`` artifact — hard-fails on a
    missing file, a wrong/absent schema, missing fps, or fewer than 2 points.
    v1 artifacts get the neutral channels (all-'T' where points exist, no
    disp) via ``parse_trajectory_artifact``."""
    if not path.exists():
        _fail(f"missing trajectory {path}")
    from video_grouper.inference.camera_planner import parse_trajectory_artifact

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        _fail(f"{path}: not a trajectory/1 or trajectory/2 artifact")
    try:
        art = parse_trajectory_artifact(raw)
    except ValueError as e:
        _fail(f"{path}: {e}")
    if len(art["points"]) < 2:
        _fail(f"{path}: trajectory too short ({len(art['points'])} points)")
    if art["fps"] is None:
        _fail(f"{path}: trajectory artifact has no fps")
    return art


def load_campath_artifact(path: Path) -> dict:
    """Load a ``camera_path/1`` JSON artifact — hard-fails on a missing file, a
    legacy pickle (it carries no g_start/src/fps metadata, so the ladder cannot
    emit a valid derived artifact from it), a wrong schema, or an empty path."""
    if not path.exists():
        _fail(f"missing campath {path}")
    raw = path.read_bytes()
    if raw[:1] != b"{":
        _fail(
            f"{path}: legacy pickle campath has no g_start/src/fps metadata -- "
            "the ladder needs a camera_path/1 JSON artifact"
        )
    art = json.loads(raw)
    if art.get("schema") != "camera_path/1":
        _fail(f"{path}: not a camera_path/1 artifact")
    if not art["frames"]:
        _fail(f"{path}: campath has 0 frames")
    return art


# ---------------------------------------------------------------------------
# Oracle transforms (pure; unit-tested directly)
# ---------------------------------------------------------------------------


def shift_trajectory(
    points: list[tuple[float, float] | None], shift: int
) -> list[tuple[float, float] | None]:
    """Perfect-lead input: ``input[t] = points[min(t + shift, N - 1)]``. Nulls
    stay nulls — a ``None`` source entry passes through as ``None`` (the oracle
    never interpolates over gaps the causal planner also cannot see into)."""
    n = len(points)
    return [points[min(t + shift, n - 1)] for t in range(n)]


def zero_phase_ema(values: list[float], alpha: float) -> list[float]:
    """Forward-then-backward EMA — a zero-phase (hindsight-only) smoother with
    no lag in either direction. No scipy."""

    def _ema(xs: list[float]) -> list[float]:
        out: list[float] = []
        acc: float | None = None
        for v in xs:
            acc = v if acc is None else acc + alpha * (v - acc)
            out.append(acc)
        return out

    return _ema(_ema(values)[::-1])[::-1]


def freeze_campath(
    frames_global: list[list[float]], clusters: list[list[int]]
) -> list[list[float]]:
    """Freeze cx,cy across each cluster's frame span ``[first, last]`` at their
    values at the span's FIRST frame; hfov untouched. ``frames_global`` is
    indexed by global source frame. Hard-fails when a cluster extends past the
    campath end (the campath must cover every hold span it claims to freeze)."""
    out = [list(fr) for fr in frames_global]
    n = len(out)
    for cl in clusters:
        lo, hi = int(cl[0]), int(cl[-1])
        if hi >= n:
            _fail(
                f"hold cluster span [{lo}, {hi}] extends past the campath end "
                f"({n} frames)"
            )
        cx0, cy0 = out[lo][0], out[lo][1]
        for f in range(lo, hi + 1):
            out[f][0] = cx0
            out[f][1] = cy0
    return out


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def _planner_config_from_args(args: argparse.Namespace):
    """``--enable-hold`` / ``--hold-knob NAME=VALUE`` -> a ``PlannerConfig``
    (None when neither is given = the pipeline defaults). Knob names must be
    ``PlannerConfig`` fields; values are cast to the field's type (bool fields
    take true/false/1/0). Unknown names or uncastable values hard-fail."""
    knobs: list[str] = getattr(args, "hold_knob", None) or []
    if not args.enable_hold and not knobs:
        return None
    from dataclasses import replace

    from video_grouper.inference.camera_planner import PlannerConfig

    defaults = PlannerConfig()
    overrides: dict = {}
    for kv in knobs:
        name, eq, val = kv.partition("=")
        name = name.strip()
        if not eq:
            _fail(f"--hold-knob {kv!r}: expected NAME=VALUE")
        if not hasattr(defaults, name):
            _fail(f"--hold-knob {name}: not a PlannerConfig field")
        cur = getattr(defaults, name)
        try:
            if isinstance(cur, bool):
                low = val.strip().lower()
                if low in ("1", "true", "yes"):
                    cast: bool | int | float = True
                elif low in ("0", "false", "no"):
                    cast = False
                else:
                    raise ValueError(val)
            elif isinstance(cur, int):
                cast = int(val)
            else:
                cast = float(val)
        except ValueError:
            _fail(f"--hold-knob {name}={val!r}: cannot cast to {type(cur).__name__}")
        overrides[name] = cast
    enable = bool(args.enable_hold) or bool(overrides.pop("enable_hold", False))
    return replace(defaults, **overrides, enable_hold=enable)


def cmd_run_a(args: argparse.Namespace) -> None:
    """Baseline A: cached candidate dump through the CURRENT champion chain."""
    fg, gd, outd = Path(args.fullgame_dir), Path(args.game_dir), Path(args.out_dir)
    if not (fg / "meta.json").exists():
        _fail(f"missing meta.json in {fg} -- not a fullgame_candidates dump")
    if not Path(args.net).exists():
        _fail(f"missing selector net {args.net}")
    load_game(gd)  # fail fast, before the heavy imports
    pcfg = _planner_config_from_args(args)
    from training.cli.plan_camera_path import replay_champion_chain
    from video_grouper.inference.camera_planner import save_camera_path

    res = replay_champion_chain(
        fg,
        gd,
        args.net,
        stride=args.stride,
        planner_config=pcfg,
        static_filter_frac=args.static_filter_frac,
        static_filter_cell=args.static_filter_cell,
        static_filter_radius=args.static_filter_radius,
        static_filter_offfield_only=args.static_filter_offfield_only,
        miss_entry_near_k=args.miss_entry_near_k,
        miss_entry_margin_k=args.miss_entry_margin_k,
        emission_weight=args.emission_weight,
        pnone_scale=args.pnone_scale,
        pnone_far_scale=args.pnone_far_scale,
        pnone_far_diam=args.pnone_far_diam,
        pnone_far_near_diam=args.pnone_far_near_diam,
        pnone_depr_far_deg=args.pnone_depr_far_deg,
        pnone_depr_near_deg=args.pnone_depr_near_deg,
        world_model=args.world_model,
    )
    if len(res["plan"]) < 2:
        _fail(f"champion chain produced {len(res['plan'])} frames -- need >= 2")
    if args.static_filter_frac and not res["static_centers"]:
        print("run-a NOTE: static filter enabled but found no static cells")
    gid = fg.name + (f".{args.tag}" if args.tag else "")
    outd.mkdir(parents=True, exist_ok=True)
    tpath = outd / f"{gid}.trajectory.json"
    cpath = outd / f"{gid}.campath.json"
    save_trajectory(
        tpath,
        res["traj"],
        g_start=res["g_start"],
        fps=res["fps"],
        state=res["states"],
        conf=res["conf"],
        disp=res["disp"],
    )
    save_camera_path(
        cpath,
        res["plan"],
        g_start=res["g_start"],
        src_w=res["src_w"],
        src_h=res["src_h"],
        fps=res["fps"],
    )
    n_pts = sum(1 for p in res["traj"] if p is not None)
    print(
        f"run-a {gid}: dump {fg} + net {Path(args.net).name} (stride "
        f"{args.stride}) -> {tpath.name} + {cpath.name} in {outd} "
        f"({len(res['plan'])} frames, {n_pts} tracked points"
        + (
            f", static filter: {len(res['static_centers'])} centers "
            f"@frac={args.static_filter_frac}"
            if args.static_filter_frac
            else ""
        )
        + ")"
    )


def cmd_run_b(args: argparse.Namespace) -> None:
    """GT-input oracle B: human GT ball positions as the ONLY candidates."""
    bl, gd, outd = Path(args.ball_labels), Path(args.game_dir), Path(args.out_dir)
    if not bl.exists():
        _fail(f"missing ball labels {bl}")
    gj = load_game(gd)
    src_w, src_h = _src_dims(gj, gd)
    pcfg = _planner_config_from_args(args)
    from training.cli.plan_camera_path import depth_from_polygon
    from training.cli.validate_tracker import build_frames
    from training.data_prep import distill_dataset as dd
    from video_grouper.inference.ball_tracker import track_ball
    from video_grouper.inference.camera_planner import (
        plan_camera,
        save_camera_path,
        upsample_track,
    )
    from video_grouper.inference.world_geometry import build_field_geometry

    polygon = np.asarray(gj["field_polygon"], float)
    geom = build_field_geometry(polygon)
    if not geom.valid:
        _fail(f"{gd.name}: field polygon fits no valid geometry")
    offs = dd.seg_offsets(gj["segments"])
    balls, _novis = dd.load_human_labels(bl, offs)
    if len(balls) < 2:
        _fail(f"{bl}: {len(balls)} GT ball labels -- need >= 2")
    gframes = sorted(balls)
    # validate_tracker's GT branch exactly: one Candidate per GT frame, score 1.0
    frames = build_frames({}, gframes, 0.0, gt=balls)
    gaps = [
        (gframes[i + 1] - gframes[i]) if i + 1 < len(gframes) else 4
        for i in range(len(gframes))
    ]
    track, g_states, g_conf = track_ball(
        frames, geom, frame_gaps=gaps, return_states=True
    )
    if not track:
        _fail("track_ball returned an empty track")
    g_start, g_end = int(gframes[0]), int(gframes[-1]) + 1
    traj, states, conf = upsample_track(
        track, gframes, g_start, g_end, states=g_states, conf=g_conf
    )
    depth01 = depth_from_polygon(traj, polygon)
    # No disp channel: GT labels are not a detections artifact (the dispersion
    # voter is defined off it — design doc section 3.3), and a single GT
    # candidate per frame would degenerately read as a zero-spread scramble.
    plan = plan_camera(
        traj,
        src_w=src_w,
        src_h=src_h,
        depth01=depth01,
        states=states,
        config=pcfg,
    )
    fps = float(gj.get("fps", 20.0))
    gid = gd.name
    outd.mkdir(parents=True, exist_ok=True)
    tpath = outd / f"{gid}.gtoracle.trajectory.json"
    cpath = outd / f"{gid}.gtoracle.campath.json"
    save_trajectory(tpath, traj, g_start=g_start, fps=fps, state=states, conf=conf)
    save_camera_path(cpath, plan, g_start=g_start, src_w=src_w, src_h=src_h, fps=fps)
    print(
        f"run-b {gid}: {len(balls)} GT balls ({bl.name}) -> {tpath.name} + "
        f"{cpath.name} in {outd} ({len(plan)} frames)"
    )


def cmd_run_c(args: argparse.Namespace) -> None:
    """Freeze-pan oracle C: hold cx,cy across the AMENDED hold clusters."""
    cp, sdir, outd = Path(args.campath), Path(args.set_dir), Path(args.out_dir)
    art = load_campath_artifact(cp)
    from training.cli.operator_scoreboard import load_labels

    labels = load_labels(sdir)  # hard-fails on missing/empty (action=='view', fx)
    gt_fx = {f: float(r["fx"]) for f, r in labels.items()}
    clusters = om.hold_clusters(gt_fx)
    if not clusters:
        _fail(
            f"{sdir.name}: no qualifying hold cluster (gap-{om.HOLD_GAP}, "
            f"n>={om.HOLD_MIN_FRAMES}, GT swing < {om.HOLD_GT_SWING_MAX_PX:g} px)"
            " -- nothing to freeze"
        )
    g0 = int(art["g_start"])
    frames = [list(fr) for fr in art["frames"]]
    # same pad semantics as build_viewport_label_queue.load_cams: the head
    # [0, g_start) is a constant fill so cluster frames index globally
    cams_global = [list(frames[0]) for _ in range(g0)] + frames
    frozen = freeze_campath(cams_global, clusters)
    from video_grouper.inference.camera_planner import save_camera_path

    base = _base_name(cp, ".campath.json", ".json")
    outd.mkdir(parents=True, exist_ok=True)
    outp = outd / f"{base}.freeze.campath.json"
    save_camera_path(
        outp,
        [(float(fr[0]), float(fr[1]), float(fr[2])) for fr in frozen[g0:]],
        g_start=g0,
        src_w=int(art["src_w"]),
        src_h=int(art["src_h"]),
        fps=float(art["fps"]),
    )
    n_frozen = sum(int(cl[-1]) - int(cl[0]) + 1 for cl in clusters)
    print(
        f"run-c {base}: froze {len(clusters)} hold clusters ({n_frozen} frames) "
        f"over a {len(frames)}-frame campath -> {outp}"
    )


def cmd_run_d(args: argparse.Namespace) -> None:
    """Lookahead pricing D: perfect-lead-Δ plans + zero-phase smoothing."""
    tp, gd, outd = Path(args.trajectory), Path(args.game_dir), Path(args.out_dir)
    traj_art = load_trajectory(tp)
    gj = load_game(gd)
    src_w, src_h = _src_dims(gj, gd)
    polygon = np.asarray(gj["field_polygon"], float)
    fps = args.fps if args.fps is not None else traj_art["fps"]
    if fps <= 0:
        _fail(f"non-positive fps {fps}")
    from training.cli.plan_camera_path import depth_from_polygon
    from video_grouper.inference.camera_planner import (
        PlannerConfig,
        plan_camera,
        save_camera_path,
    )

    pts = traj_art["points"]
    base = _base_name(tp, ".trajectory.json", ".json")
    outd.mkdir(parents=True, exist_ok=True)
    outs: list[str] = []
    for lead in args.lead_s:
        shift = round(lead * fps)
        shifted = shift_trajectory(pts, shift)
        # same PlannerConfig defaults as the pipeline preset (plan_camera_path)
        plan = plan_camera(
            shifted,
            src_w=src_w,
            src_h=src_h,
            depth01=depth_from_polygon(shifted, polygon),
        )
        outp = outd / f"{base}.lead{lead:g}.campath.json"
        save_camera_path(
            outp,
            plan,
            g_start=traj_art["g_start"],
            src_w=src_w,
            src_h=src_h,
            fps=traj_art["fps"],
        )
        outs.append(outp.name)
    if args.campath:
        art = load_campath_artifact(Path(args.campath))
        cams = [list(fr) for fr in art["frames"]]
        if len(cams) < 2:
            _fail(f"{args.campath}: campath too short ({len(cams)} frames)")
        # alpha = midpoint of the planner's error-adaptive pan smoothing range
        alpha = (PlannerConfig.pan_smoothing_min + PlannerConfig.pan_smoothing_max) / 2
        cxs = zero_phase_ema([fr[0] for fr in cams], alpha)
        cys = zero_phase_ema([fr[1] for fr in cams], alpha)
        zp = [(cx, cy, fr[2]) for cx, cy, fr in zip(cxs, cys, cams, strict=True)]
        cbase = _base_name(Path(args.campath), ".campath.json", ".json")
        outp = outd / f"{cbase}.zerophase.campath.json"
        save_camera_path(
            outp,
            zp,
            g_start=int(art["g_start"]),
            src_w=int(art["src_w"]),
            src_h=int(art["src_h"]),
            fps=float(art["fps"]),
        )
        outs.append(outp.name)
    print(
        f"run-d {base}: {len(pts)} trajectory frames, leads "
        f"{[f'{v:g}' for v in args.lead_s]} s @ {fps:g} fps -> "
        f"{', '.join(outs)} in {outd}"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="operator_ladder", description=__doc__.splitlines()[0]
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser(
        "run-a", help="cached candidate dump -> current champion chain (baseline)"
    )
    a.add_argument("--fullgame-dir", required=True)
    a.add_argument("--net", required=True, help="selector .pt checkpoint")
    a.add_argument("--game-dir", required=True)
    a.add_argument("--out-dir", required=True)
    a.add_argument(
        "--stride",
        type=int,
        default=1,
        help="subsample the dump's candidate grid (default 1 = full grid, the "
        "pipeline behavior)",
    )
    a.add_argument(
        "--static-filter-frac",
        type=float,
        default=None,
        help="enable session-scoped static-object suppression: drop candidates "
        "near cells occupied in >= this fraction of frames (EXP-OP-10 arm F; "
        "e.g. 0.20)",
    )
    a.add_argument("--static-filter-cell", type=float, default=50.0)
    a.add_argument("--static-filter-radius", type=float, default=60.0)
    a.add_argument(
        "--miss-entry-near-k",
        type=float,
        default=0.0,
        help="W3 stage-1 arm N (task #22): miss-ENTRY cost multiplier weight for "
        "a NEAR+SLOW top candidate (0 = off, shipped chain)",
    )
    a.add_argument(
        "--miss-entry-margin-k",
        type=float,
        default=0.0,
        help="W3 stage-1 arm M (task #22): miss-ENTRY cost multiplier weight for "
        "a clearly-separated rank-1 candidate (0 = off, shipped chain)",
    )
    a.add_argument(
        "--pnone-scale",
        type=float,
        default=1.0,
        help="EXP-OP-25: scale the LEARNED miss cost (-log p_none). >1 makes the "
        "miss state more expensive -> the tracker HOLDS a detected candidate "
        "longer (the far detected-but-lost lever, EXP-OP-24). 1.0 = shipped.",
    )
    a.add_argument(
        "--emission-weight",
        type=float,
        default=1.0,
        help="scale the selector emission prior AND the miss cost together "
        "(1.0 = shipped)",
    )
    a.add_argument(
        "--pnone-far-scale",
        type=float,
        default=1.0,
        help="EXP-OP-26: DEPTH-GATED miss-cost hold — applied only when the top "
        "candidate is FAR (diam < 8px). Holds detected far balls WITHOUT "
        "over-holding near clutter (the near cost of global --pnone-scale). "
        "1.0 = off; overrides pnone-scale on far frames only.",
    )
    a.add_argument(
        "--pnone-far-diam",
        type=float,
        default=8.0,
        help="EXP-OP-29: expected-diameter px threshold for the far-hold gate "
        "(default 8 = the far band). Tighten (e.g. 6) to hold only VERY far "
        "candidates and trim the far->near boundary near-dip.",
    )
    a.add_argument(
        "--pnone-far-near-diam",
        type=float,
        default=0.0,
        help="EXP-OP-30 (geometry-conditioned, Mark): when > --pnone-far-diam, "
        "the hold strength RAMPS smoothly with depth from 1x at this diameter "
        "(near) to --pnone-far-scale at --pnone-far-diam (far), instead of a "
        "binary switch. Dissolves the near/far tension. 0 = binary (fd6).",
    )
    a.add_argument(
        "--pnone-depr-far-deg",
        type=float,
        default=0.0,
        help="EXP-OP-33 (#19, lens-compensated axis): full --pnone-far-scale hold "
        "at ray-geometry DEPRESSION angle <= this (far field). Ramps to 1x at "
        "--pnone-depr-near-deg. Active when near_deg>far_deg; supersedes the "
        "diam path with the geometry-correct farness axis (EXP-OP-32).",
    )
    a.add_argument(
        "--pnone-depr-near-deg",
        type=float,
        default=0.0,
        help="Depression angle (deg) at/above which no hold (near field). "
        "See --pnone-depr-far-deg.",
    )
    a.add_argument(
        "--world-model",
        choices=("homography", "ray"),
        default="homography",
        help="EXP-OP-34 (#19): world model for the TRACKER's meters (physics "
        "gate, measurement noise, Kalman, oob/bridge). 'ray' = ray-ground "
        "intersection from the polygon-leveled orientation (correct meters); "
        "'homography' = the planar ruler (bows +/-35%%, EXP-OP-32). Selector "
        "features always stay on the trained planar geometry.",
    )
    a.add_argument(
        "--static-filter-offfield-only",
        action="store_true",
        help="drop only static centers whose WORLD position is outside the "
        "field rectangle (+2m) -- in-field statics (keepers) are left to the "
        "world-cell static_w penalty (EXP-OP-11 arm F2)",
    )
    a.add_argument(
        "--tag",
        default=None,
        help="suffix for output basenames (arm separation in one out-dir)",
    )
    a.set_defaults(fn=cmd_run_a)

    b = sub.add_parser(
        "run-b", help="GT ball positions as the ONLY candidates (input ceiling)"
    )
    b.add_argument("--ball-labels", required=True, help="ball_labels.jsonl")
    b.add_argument("--game-dir", required=True)
    b.add_argument("--out-dir", required=True)
    b.set_defaults(fn=cmd_run_b)

    for hold_sub in (a, b):
        hold_sub.add_argument(
            "--enable-hold",
            action="store_true",
            help="turn on the W2 stoppage-HOLD planner FSM (PlannerConfig.enable_hold)",
        )
        hold_sub.add_argument(
            "--hold-knob",
            action="append",
            default=[],
            metavar="NAME=VALUE",
            help="override a PlannerConfig field (repeatable), e.g. "
            "hold_entry_frames=30",
        )

    c = sub.add_parser("run-c", help="freeze-pan oracle over the amended hold clusters")
    c.add_argument("--campath", required=True, help="camera_path/1 JSON artifact")
    c.add_argument("--set-dir", required=True, help="viewport label set dir")
    c.add_argument("--out-dir", required=True)
    c.set_defaults(fn=cmd_run_c)

    d = sub.add_parser(
        "run-d", help="lookahead pricing: perfect-lead-Δ + zero-phase smoothing"
    )
    d.add_argument("--trajectory", required=True, help="trajectory/1 JSON artifact")
    d.add_argument("--game-dir", required=True)
    d.add_argument("--out-dir", required=True)
    d.add_argument("--lead-s", type=float, nargs="+", default=[0.5, 1.0, 2.0, 3.0])
    d.add_argument(
        "--fps",
        type=float,
        default=None,
        help="fps for the lead-frame conversion (default: the trajectory "
        "artifact's own fps -- an explicit value overrides it)",
    )
    d.add_argument(
        "--campath",
        default=None,
        help="optional run-a campath: also emit its zero-phase smoothed twin",
    )
    d.set_defaults(fn=cmd_run_d)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
