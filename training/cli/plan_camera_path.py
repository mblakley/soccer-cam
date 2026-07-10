"""Plan the camera path from the champion track and SCORE it against the benchmark.

The camera path is a first-class artifact (dumb-renderer architecture, Mark
2026-07-09): per-source-frame ``{center_px, hfov_deg}`` commands. Because it is
pure data, the viewport benchmark can grade the PLANNED camera — what viewers
will actually get — before any pixel is rendered. Two scores are reported:

- **fixed ellipse** (1200x500 source px): apples-to-apples with every track
  score published so far;
- **planned-view ellipse** (derived per frame from the command's own hfov, 16:9):
  the honest "is the ball inside the frame we intend to render".

    python -m training.cli.plan_camera_path \
      --net G:/ballresearch/selector/selector_v5.pt \
      --fullgame-dir .../fullgame_heldout/heat__2026.05.31_vs_Spencerport_gold_2_away \
      --game-dir "F:/Heat_2012s/2026.05.31 - vs Spencerport gold 2 (away)" \
      --out G:/ballresearch/selector/spc_camera_path.json
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

import numpy as np


def depth_from_polygon(
    traj: list[tuple[float, float] | None], polygon: np.ndarray
) -> list[float | None]:
    """Field depth per frame (0 = far touchline, 1 = near) from the image y of the
    ball between the polygon's far (points 5-9) and near (0-4) line means."""
    y_near = float(np.mean(polygon[0:5, 1]))
    y_far = float(np.mean(polygon[5:10, 1])) if len(polygon) >= 10 else y_near - 1.0
    span = max(y_near - y_far, 1e-6)
    out: list[float | None] = []
    for p in traj:
        if p is None:
            out.append(None)
        else:
            out.append(float(np.clip((p[1] - y_far) / span, 0.0, 1.0)))
    return out


def score_plan(
    plan: list[tuple[float, float, float]],
    g_start: int,
    bench: dict[int, dict],
    src_w: int,
    *,
    traj: list | None = None,
    fixed: tuple[float, float] | None = (1200.0, 500.0),
) -> dict:
    """Ball-in-planned-view rates by benchmark tier + sustained loss windows.

    Rows where the planner had NO input (``traj[i] is None`` — outside active
    play / track coverage) are excluded and counted as ``uncovered``: the render
    is phase-gated there, so the camera cannot be graded on them."""
    tally = {"human": [0, 0], "autocam": [0, 0]}
    events: list[tuple[int, bool]] = []
    uncovered = 0
    for g, r in sorted(bench.items()):
        i = g - g_start
        if not (0 <= i < len(plan)):
            continue
        if traj is not None and traj[i] is None:
            uncovered += 1
            continue
        cx, cy, hfov = plan[i]
        if fixed is not None:
            hw, hh = fixed
        else:
            hw = src_w * (hfov / 180.0) / 2.0
            hh = hw * (1080.0 / 1920.0)
        inside = ((r["x"] - cx) / hw) ** 2 + ((r["y"] - cy) / hh) ** 2 <= 1.0
        tally[r["tier"]][1] += 1
        tally[r["tier"]][0] += int(inside)
        events.append((g, inside))
    runs: list[list] = []
    for g, ok in events:
        if runs and runs[-1][0] == ok and g - runs[-1][2] <= 48:
            runs[-1][2] = g
        else:
            runs.append([ok, g, g])
    loss = [(r[1], r[2]) for r in runs if not r[0] and (r[2] - r[1]) >= 40]
    return {
        "human": tally["human"],
        "autocam": tally["autocam"],
        "loss": loss,
        "uncovered": uncovered,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--net", required=True)
    ap.add_argument("--fullgame-dir", required=True)
    ap.add_argument("--game-dir", required=True)
    ap.add_argument("--out", required=True, help="camera_path/1 artifact path")
    ap.add_argument("--emission-weight", type=float, default=1.0)
    ap.add_argument("--pnone-scale", type=float, default=1.0)
    ap.add_argument("--phys-sigma-px", type=float, default=5.0)
    ap.add_argument("--bridge-w", type=float, default=2.0)
    ap.add_argument("--oob-w", type=float, default=2.0)
    ap.add_argument("--static-w", type=float, default=2.0)
    args = ap.parse_args()

    from training.cli.build_selector_labels import load_fullgame_candidates
    from training.models.selector_net import load_selector, pack_frames, predict_probs
    from training.world_model.camera_planner import (
        plan_camera,
        save_camera_path,
        upsample_track,
    )
    from training.world_model.geometry import build_field_geometry
    from training.world_model.reranker import RerankConfig, kalman_smooth, rerank
    from training.world_model.selector_features import build_features
    from training.world_model.tbd import Candidate

    gd = Path(args.game_dir)
    ef, cands, _meta = load_fullgame_candidates(Path(args.fullgame_dir))
    gj = json.loads((gd / "game.json").read_text(encoding="utf-8", errors="ignore"))
    polygon = np.asarray(gj["field_polygon"], float)
    geom = build_field_geometry(polygon)
    seg0 = gj["segments"][0]
    src_w, src_h = int(seg0["w"]), int(seg0["h"])
    frames = [
        [Candidate(x=x, y=y, score=s, size_px=None) for (x, y, s, _z) in cands[g]]
        for g in ef
    ]
    gaps = [1] + [ef[i] - ef[i - 1] for i in range(1, len(ef))]
    net, keep = load_selector(args.net)
    feats = [x[:, keep] for x in build_features(frames, geom, ef=ef)]
    packed, mask = pack_frames(feats)
    probs = predict_probs(net, packed, mask)
    w = args.emission_weight
    priors = [
        w * -np.log(np.maximum(probs[i, : len(fr)], 1e-6)) if fr else np.zeros(0)
        for i, fr in enumerate(frames)
    ]
    mc = [
        float(args.pnone_scale * w * -np.log(max(float(probs[i, -1]), 1e-6)))
        for i in range(len(frames))
    ]
    cfg = replace(
        RerankConfig(),
        alpha=0.0,
        static_w=args.static_w,
        motion_w=0.0,
        phys_sigma_px=args.phys_sigma_px,
        bridge_w=args.bridge_w,
        oob_w=args.oob_w,
    )
    sel = rerank(
        frames, geom, frame_gaps=gaps, priors=priors, miss_costs=mc, config=cfg
    )
    track = kalman_smooth(sel, geom)

    g_start, g_end = int(ef[0]), int(ef[-1]) + 1
    traj = upsample_track(track, ef, g_start, g_end)
    depth01 = depth_from_polygon(traj, polygon)
    plan = plan_camera(traj, src_w=src_w, src_h=src_h, depth01=depth01)
    save_camera_path(
        args.out,
        plan,
        g_start=g_start,
        src_w=src_w,
        src_h=src_h,
        fps=float(gj.get("fps", 20.0)),
    )
    # Debug sidecar: the dense per-frame BALL track (source px, null when uncovered),
    # for the eval renderer's minimap ball marker + trail. Not a product artifact.
    track_path = Path(args.out).with_suffix(".track.json")
    track_path.write_text(
        json.dumps(
            {
                "g_start": g_start,
                "frames": [
                    [round(float(p[0]), 1), round(float(p[1]), 1)] if p else None
                    for p in traj
                ],
            }
        )
    )
    print(f"{gd.name}: camera path {len(plan)} frames -> {args.out}")

    bench_path = gd / "viewport_benchmark.jsonl"
    if not bench_path.exists():
        print("no viewport_benchmark.jsonl — skipping score")
        return
    bench: dict[int, dict] = {}
    for ln in bench_path.read_text(encoding="utf-8").splitlines():
        if ln.strip():
            r = json.loads(ln)
            if r.get("tier") != "none":
                bench[int(r["g"])] = r
    for label, fixed in (("fixed 1200x500", (1200.0, 500.0)), ("planned-view", None)):
        s = score_plan(plan, g_start, bench, src_w, traj=traj, fixed=fixed)
        bh, ba = s["human"], s["autocam"]
        print(
            f"  {label:14s} human {bh[0]}/{bh[1]} = {bh[0] / max(bh[1], 1):.3f}  "
            f"autocam-tier {ba[0]}/{ba[1]} = {ba[0] / max(ba[1], 1):.3f}  "
            f"loss-windows {len(s['loss'])}  uncovered {s['uncovered']}"
        )


if __name__ == "__main__":
    main()
