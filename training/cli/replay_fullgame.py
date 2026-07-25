"""Replay the LEARNED selector over a FULL-GAME candidate dump — the integration eval.

This is where the three 2026-07-06 threads meet: learned listwise emission
(EXP-DIST-29) x physical transitions (EXP-DIST-32) x aerial bridge/ballistic cone
(EXP-DIST-30/31) — over an entire game instead of label-span eval dumps (which are
too short to contain a flight, so they cannot exercise the aerial machinery).

Scores each config against the game's consolidated human labels (R15 by band +
run-structure continuity + raw-teleport count) and, when ``autocam_viewport.jsonl``
exists, against AutoCam's own viewport: ball-in-viewport agreement uses a nominal
rendered-viewport ELLIPSE in source pixels — NOT raw far-band meters, which are
dominated by the homography's exploding depth derivative (an 8 px offset at the far
line reads as tens of meters; the spc_eval_spans vision check proved the viewport was
on the action while "48 m away").

    python -m training.cli.replay_fullgame \
      --net G:/ballresearch/selector/selector_v4.pt \
      --fullgame-dir G:/ballresearch/selector/fullgame_heldout/heat__2026.05.31_vs_Spencerport_gold_2_away \
      --game-dir "F:/Heat_2012s/2026.05.31 - vs Spencerport gold 2 (away)" \
      --phys-sigma-px 0 5 --bridge-w 0 1
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

import numpy as np

from training.cli.kill_test_selector import HELD_OUT_TOKENS


def load_viewport(game_dir: Path, offs: dict) -> dict[int, tuple[float, float]]:
    """``{global_frame: (x, y) source px}`` from autocam_viewport.jsonl (empty if none)."""
    p = game_dir / "autocam_viewport.jsonl"
    vp: dict[int, tuple[float, float]] = {}
    if not p.exists():
        return vp
    for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not ln.strip():
            continue
        r = json.loads(ln)
        g = offs.get(r.get("seg"))
        if g is not None:
            vp[g + int(r["f"])] = (float(r["x"]), float(r["y"]))
    return vp


def viewport_agreement(
    track_px: dict[int, tuple[float, float]],
    ef: list[int],
    vp: dict[int, tuple[float, float]],
    *,
    half_w: float = 1200.0,
    half_h: float = 500.0,
    min_run_s: float = 2.0,
    fps: float = 20.0,
) -> dict:
    """Our track vs AutoCam's viewport center: fraction of frames our pick falls
    inside AutoCam's nominal rendered ellipse, plus sustained-divergence windows
    (global-frame spans) for adjudication. Divergence != error — it is WHERE the
    two systems disagree; human GT decides who was right there."""
    if not vp:
        return {}
    vk = np.asarray(sorted(vp), int)
    events: list[tuple[int, bool]] = []
    for i, g in enumerate(ef):
        if i not in track_px:
            continue
        j = int(np.clip(np.searchsorted(vk, g), 0, len(vk) - 1))
        j = min((j - 1, j), key=lambda k: abs(int(vk[max(k, 0)]) - g))
        gv = int(vk[max(j, 0)])
        if abs(gv - g) > 4:
            continue
        vx, vy = vp[gv]
        x, y = track_px[i]
        inside = ((x - vx) / half_w) ** 2 + ((y - vy) / half_h) ** 2 <= 1.0
        events.append((g, bool(inside)))
    if not events:
        return {}
    runs: list[list] = []
    for g, ok in events:
        if runs and runs[-1][0] == ok and g - runs[-1][2] <= 48:
            runs[-1][2] = g
        else:
            runs.append([ok, g, g])
    min_len = int(min_run_s * fps)
    div = [(r[1], r[2]) for r in runs if not r[0] and (r[2] - r[1]) >= min_len]
    return {
        "agree": sum(1 for _g, ok in events if ok) / len(events),
        "n": len(events),
        "divergence_windows": div,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--net", required=True)
    ap.add_argument("--fullgame-dir", required=True)
    ap.add_argument("--game-dir", required=True)
    ap.add_argument("--emission-weight", type=float, default=1.0)
    ap.add_argument(
        "--alpha",
        type=float,
        default=0.0,
        help="hand-tuned detector-score emission term (hybrid with the learned prior)",
    )
    ap.add_argument(
        "--static-w",
        type=float,
        default=0.0,
        help="hand-tuned static-persistence emission term (hybrid)",
    )
    ap.add_argument("--miss-cost", type=float, default=0.9)
    ap.add_argument(
        "--oob-w",
        nargs="+",
        type=float,
        default=[0.0],
        help="out-of-bounds pin weights to sweep (0 = off)",
    )
    ap.add_argument("--pnone-scale", type=float, default=None)
    ap.add_argument("--bridge-w", nargs="+", type=float, default=[0.0, 1.0])
    ap.add_argument("--phys-sigma-px", nargs="+", type=float, default=[0.0, 5.0])
    ap.add_argument("--far-px", type=float, default=8.0)
    ap.add_argument(
        "--benchmark",
        default=None,
        help="viewport_benchmark.jsonl (best-of AutoCam + human) to score against "
        "game-wide; defaults to the game dir's file when present",
    )
    ap.add_argument("--out", default=None, help="also write a JSON report here")
    args = ap.parse_args()

    from training.cli.build_selector_labels import load_fullgame_candidates
    from training.cli.sweep_tracker import continuity_line, track_continuity
    from training.data_prep import distill_dataset as dd
    from training.models.selector_net import load_selector, pack_frames, predict_probs
    from training.world_model.geometry import build_field_geometry
    from training.world_model.reranker import RerankConfig, kalman_smooth, rerank
    from training.world_model.selector_features import build_features
    from training.world_model.tbd import Candidate

    gd = Path(args.game_dir)
    ef, cands, _meta = load_fullgame_candidates(Path(args.fullgame_dir))
    gj = json.loads((gd / "game.json").read_text(encoding="utf-8", errors="ignore"))
    geom = build_field_geometry(np.asarray(gj["field_polygon"], float))
    if not geom.valid:
        raise SystemExit("field polygon does not fit a valid homography")
    offs = dd.seg_offsets(gj["segments"])
    hb, _hn = (
        dd.load_human_labels(gd / "ball_labels.jsonl", offs)
        if (gd / "ball_labels.jsonl").exists()
        else ({}, set())
    )
    vp = load_viewport(gd, offs)
    bench_path = (
        Path(args.benchmark) if args.benchmark else gd / "viewport_benchmark.jsonl"
    )
    bench: dict[int, dict] = {}
    if bench_path.exists():
        for ln in bench_path.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                r = json.loads(ln)
                if r.get("tier") != "none":
                    bench[int(r["g"])] = r
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
    mc_list = None
    if args.pnone_scale is not None:
        mc_list = [
            float(args.pnone_scale * w * -np.log(max(float(probs[i, -1]), 1e-6)))
            for i in range(len(frames))
        ]

    def wpt(p):
        return geom.image_to_world(np.asarray([p], float))[0]

    is_eval_only = any(t in str(gd).lower() for t in HELD_OUT_TOKENS)
    print(
        f"{gd.name}: {len(ef)} frames, {len(hb)} human labels"
        f"{' [HELD-OUT: eval-only]' if is_eval_only else ''}, "
        f"viewport rows {len(vp)}"
    )
    report = []
    base = replace(
        RerankConfig(),
        alpha=args.alpha,
        static_w=args.static_w,
        motion_w=0.0,
        miss_cost=args.miss_cost * w,
    )
    import itertools

    for phys, bw, ow in itertools.product(
        args.phys_sigma_px, args.bridge_w, args.oob_w
    ):
        if True:
            cfg = replace(base, phys_sigma_px=phys, bridge_w=bw, oob_w=ow)
            sel = rerank(
                frames,
                geom,
                frame_gaps=gaps,
                priors=priors,
                miss_costs=mc_list,
                config=cfg,
            )
            track = kalman_smooth(sel, geom)
            sk = sorted(sel)
            tele = sum(
                1
                for a, b in zip(sk, sk[1:], strict=False)
                if ef[b] - ef[a] <= 24
                and float(np.linalg.norm(wpt(sel[b]) - wpt(sel[a])))
                / max(ef[b] - ef[a], 1)
                > 2.5
            )
            ef_arr = np.asarray(ef, int)
            hits = {"near": [0, 0], "far": [0, 0]}
            for g, xy in hb.items():
                k = int(np.searchsorted(ef_arr, g))
                opts = [j for j in (k - 1, k) if 0 <= j < len(ef_arr)]
                i = min(opts, key=lambda j: abs(int(ef_arr[j]) - g))
                if abs(int(ef_arr[i]) - g) > 4 or i not in track:
                    continue
                size = float(geom.expected_ball_diameter_px(np.asarray([xy], float))[0])
                band = "far" if size < args.far_px else "near"
                hits[band][1] += 1
                if float(np.linalg.norm(wpt(track[i]) - wpt(xy))) <= 15.0:
                    hits[band][0] += 1
            cont = track_continuity(track, ef, hb, geom, stride=8)
            va = viewport_agreement(track, ef, vp)
            # game-wide benchmark scoring: our pick inside the nominal viewport
            # ellipse around the best-of reference, by tier
            bm: dict = {}
            if bench:
                bk = np.asarray(sorted(bench), int)
                hw2, hh2 = 1200.0, 500.0
                tally = {"human": [0, 0], "autocam": [0, 0]}
                bevents: list[tuple[int, bool]] = []
                for i, g in enumerate(ef):
                    if i not in track:
                        continue
                    j = int(np.clip(np.searchsorted(bk, g), 1, len(bk) - 1))
                    gb = (
                        int(bk[j - 1])
                        if abs(int(bk[j - 1]) - g) <= abs(int(bk[j]) - g)
                        else int(bk[j])
                    )
                    if abs(gb - g) > 4:
                        continue
                    r = bench[gb]
                    x, y = track[i]
                    inside = ((x - r["x"]) / hw2) ** 2 + (
                        (y - r["y"]) / hh2
                    ) ** 2 <= 1.0
                    tally[r["tier"]][1] += 1
                    tally[r["tier"]][0] += int(inside)
                    bevents.append((g, inside))
                miss_runs: list[tuple[int, int]] = []
                run_s = None
                prev_g = None
                for g, ok in bevents:
                    if not ok:
                        if run_s is None or (prev_g is not None and g - prev_g > 48):
                            if run_s is not None and prev_g - run_s >= 40:
                                miss_runs.append((run_s, prev_g))
                            run_s = g
                    else:
                        if (
                            run_s is not None
                            and prev_g is not None
                            and prev_g - run_s >= 40
                        ):
                            miss_runs.append((run_s, prev_g))
                        run_s = None
                    prev_g = g
                if run_s is not None and prev_g is not None and prev_g - run_s >= 40:
                    miss_runs.append((run_s, prev_g))
                bm = {
                    "human": tally["human"],
                    "autocam": tally["autocam"],
                    "n": len(bevents),
                    "our_loss_windows": miss_runs,
                }
            n_n, n_f = hits["near"], hits["far"]
            row = {
                "phys": phys,
                "bridge": bw,
                "near": n_n[0] / max(n_n[1], 1),
                "far": n_f[0] / max(n_f[1], 1),
                "teleports": tele,
                "miss_frames": len(ef) - len(sel),
                "continuity": cont,
                "viewport": va,
                "benchmark": bm,
            }
            report.append(row)
            print(
                f"  phys={phys} br={bw} oob={ow}: NEAR {row['near']:.3f} ({n_n[1]}) "
                f"FAR {row['far']:.3f} ({n_f[1]}) | teleports {tele} "
                f"miss {row['miss_frames']}"
            )
            print("      " + continuity_line(cont))
            if va:
                print(
                    f"      viewport agree {va['agree']:.2f} (n={va['n']}), "
                    f"divergence windows {len(va['divergence_windows'])}: "
                    f"{va['divergence_windows'][:6]}"
                )
            if bm:
                bh, ba = bm["human"], bm["autocam"]
                print(
                    f"      BENCHMARK ball-in-viewport: human {bh[0]}/{bh[1]} = "
                    f"{bh[0] / max(bh[1], 1):.3f}  autocam-tier {ba[0]}/{ba[1]} = "
                    f"{ba[0] / max(ba[1], 1):.3f}  | our-loss windows "
                    f"{len(bm['our_loss_windows'])}: {bm['our_loss_windows'][:5]}"
                )
    if args.out:
        Path(args.out).write_text(json.dumps(report, default=str))


if __name__ == "__main__":
    main()
