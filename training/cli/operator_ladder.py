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

Artifacts: dense trajectories as ``trajectory/1`` JSON
``{schema, g_start, fps, points: [[x, y] | null, ...]}``; campaths as the
existing ``camera_path/1`` (``save_camera_path``).
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
    path: Path, traj: list[tuple[float, float] | None], *, g_start: int, fps: float
) -> None:
    """Dense per-source-frame ball trajectory as a ``trajectory/1`` artifact."""
    payload = {
        "schema": "trajectory/1",
        "g_start": int(g_start),
        "fps": float(fps),
        "points": [
            None if p is None else [round(float(p[0]), 1), round(float(p[1]), 1)]
            for p in traj
        ],
    }
    path.write_text(json.dumps(payload))


def load_trajectory(path: Path) -> dict:
    """Load a ``trajectory/1`` artifact — hard-fails on a missing file, a wrong
    schema, or fewer than 2 points."""
    if not path.exists():
        _fail(f"missing trajectory {path}")
    art = json.loads(path.read_text(encoding="utf-8"))
    if art.get("schema") != "trajectory/1":
        _fail(f"{path}: not a trajectory/1 artifact")
    pts = [None if p is None else (float(p[0]), float(p[1])) for p in art["points"]]
    if len(pts) < 2:
        _fail(f"{path}: trajectory too short ({len(pts)} points)")
    return {"g_start": int(art["g_start"]), "fps": float(art["fps"]), "points": pts}


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


def cmd_run_a(args: argparse.Namespace) -> None:
    """Baseline A: cached candidate dump through the CURRENT champion chain."""
    fg, gd, outd = Path(args.fullgame_dir), Path(args.game_dir), Path(args.out_dir)
    if not (fg / "meta.json").exists():
        _fail(f"missing meta.json in {fg} -- not a fullgame_candidates dump")
    if not Path(args.net).exists():
        _fail(f"missing selector net {args.net}")
    load_game(gd)  # fail fast, before the heavy imports
    from training.cli.plan_camera_path import replay_champion_chain
    from video_grouper.inference.camera_planner import save_camera_path

    res = replay_champion_chain(fg, gd, args.net, stride=args.stride)
    if len(res["plan"]) < 2:
        _fail(f"champion chain produced {len(res['plan'])} frames -- need >= 2")
    gid = fg.name
    outd.mkdir(parents=True, exist_ok=True)
    tpath = outd / f"{gid}.trajectory.json"
    cpath = outd / f"{gid}.campath.json"
    save_trajectory(tpath, res["traj"], g_start=res["g_start"], fps=res["fps"])
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
        f"({len(res['plan'])} frames, {n_pts} tracked points)"
    )


def cmd_run_b(args: argparse.Namespace) -> None:
    """GT-input oracle B: human GT ball positions as the ONLY candidates."""
    bl, gd, outd = Path(args.ball_labels), Path(args.game_dir), Path(args.out_dir)
    if not bl.exists():
        _fail(f"missing ball labels {bl}")
    gj = load_game(gd)
    src_w, src_h = _src_dims(gj, gd)
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
    track = track_ball(frames, geom, frame_gaps=gaps)
    if not track:
        _fail("track_ball returned an empty track")
    g_start, g_end = int(gframes[0]), int(gframes[-1]) + 1
    traj = upsample_track(track, gframes, g_start, g_end)
    depth01 = depth_from_polygon(traj, polygon)
    plan = plan_camera(traj, src_w=src_w, src_h=src_h, depth01=depth01)
    fps = float(gj.get("fps", 20.0))
    gid = gd.name
    outd.mkdir(parents=True, exist_ok=True)
    tpath = outd / f"{gid}.gtoracle.trajectory.json"
    cpath = outd / f"{gid}.gtoracle.campath.json"
    save_trajectory(tpath, traj, g_start=g_start, fps=fps)
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
        [tuple(fr) for fr in frozen[g0:]],
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
    a.set_defaults(fn=cmd_run_a)

    b = sub.add_parser(
        "run-b", help="GT ball positions as the ONLY candidates (input ceiling)"
    )
    b.add_argument("--ball-labels", required=True, help="ball_labels.jsonl")
    b.add_argument("--game-dir", required=True)
    b.add_argument("--out-dir", required=True)
    b.set_defaults(fn=cmd_run_b)

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
