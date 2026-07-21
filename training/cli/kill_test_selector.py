"""Phase-1 KILL TEST for the learned game-ball selector (go/no-go for the A-bet).

Trains the listwise selector (context features only) on selection-level distillation
labels from 1-2 TRAINING games' dumps, then evaluates on the HELD-OUT dumps:

  1. **learned-argmax** (no tracker): pick argmax P(candidate) per frame — compare to raw
     score-argmax (0.24 far / 0.30 near, EXP-DIST-22).
  2. **tracker replay**: plug ``-w*log p`` in as the rerank emission (``priors``; alpha=0,
     static_w=0 — those signals are inside the features) — compare SELECTED to the current
     0.61 far / 0.54 near and the AutoCam bar (0.845 / 0.978).

GO = learned argmax >= +0.15 absolute on far AND near, on BOTH held-out games, ceiling
unchanged (the candidate set is fixed by the dump, so the ceiling CANNOT move here — it is
printed as the reference bar). Feature-family knockouts (--knockouts) decide whether the
person-density head is justified next.

    python -m training.cli.kill_test_selector \
      --train "dumpA.pkl;labelsA.json" "dumpB.pkl;labelsB.json" \
      --eval cands_spc_hn2.pkl cands_iron_hn2.pkl
"""

from __future__ import annotations

import argparse
import json
import pickle
from dataclasses import replace
from pathlib import Path

import numpy as np

# Spencerport 05.31 + Irondequoit 06.15 are EVAL-ONLY, never trained (plan §6).
# Matched against the label file's game_dir (lowercased). "irondequoit" alone is NOT
# a held-out token — Irondequoit 06.04 is a legitimate training game.
HELD_OUT_TOKENS = ("spencerport", "2026.06.15")


def check_not_held_out(dump_path: str, game_dir: str) -> None:
    """Refuse a training pair whose dump/game matches a held-out token."""
    for hay in (dump_path.lower(), game_dir.lower()):
        for t in HELD_OUT_TOKENS:
            if t in hay:
                raise SystemExit(
                    f"HELD-OUT game in training set ({t!r}): {dump_path} — "
                    "Spencerport 05.31 / Irondequoit 06.15 are eval-only"
                )


def split_train_pair(pair: str) -> tuple[str, str]:
    """Split a ``dump;labels`` CLI pair. The separator is ``;`` (never ``:``) because
    Windows paths contain drive colons — a ``:`` separator split inside ``G:\\...``."""
    if ";" not in pair:
        raise SystemExit(f"--train pair needs 'dump;labels' (got: {pair!r})")
    a, b = pair.split(";", 1)
    return a, b


def _load_dump(path):
    """Load an ``eval_detector --dump-cands`` pickle OR a ``dump_game_candidates``
    output DIRECTORY (marathon artifact; polygon comes from the game.json its
    meta.json points at)."""
    from training.world_model.geometry import build_field_geometry
    from training.world_model.tbd import Candidate

    p = Path(path)
    if p.is_dir():
        from training.cli.build_selector_labels import load_fullgame_candidates

        ef, cands, meta = load_fullgame_candidates(p)
        gj = json.loads(
            (Path(meta["game_dir"]) / "game.json").read_text(
                encoding="utf-8", errors="ignore"
            )
        )
        d = {"ef": ef, "cands": cands, "polygon": gj["field_polygon"]}
    else:
        with open(path, "rb") as fh:
            d = pickle.load(fh)
    frames = [
        [Candidate(x=x, y=y, score=s, size_px=sz) for (x, y, s, sz) in d["cands"][f]]
        for f in d["ef"]
    ]
    geom = build_field_geometry(np.asarray(d["polygon"], float))
    return d, frames, geom


def _features_packed(frames, geom, keep, ef):
    from training.models.selector_net import pack_frames
    from training.world_model.selector_features import build_features

    feats = build_features(frames, geom, ef=ef)
    feats = [x[:, keep] for x in feats]
    return pack_frames(feats)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--train",
        nargs="+",
        required=True,
        help="'dump.pkl;selector_labels.json' pairs (training games; ';' separator "
        "because Windows paths contain drive colons)",
    )
    ap.add_argument(
        "--eval", nargs="+", required=True, help="held-out dumps (contain GT)"
    )
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument(
        "--knockouts",
        nargs="*",
        default=[],
        help="feature families to REMOVE (or 'sweep' = one run per family)",
    )
    ap.add_argument("--emission-weights", nargs="+", type=float, default=[1.0, 2.0])
    ap.add_argument("--miss-costs", nargs="+", type=float, default=[0.5, 0.9, 1.5])
    ap.add_argument(
        "--pnone-scales",
        nargs="*",
        type=float,
        default=[],
        help="ALSO replay with per-frame miss cost = scale * w * -log P(none) "
        "(the learned selector's none-head driving the miss state)",
    )
    ap.add_argument(
        "--bridge-w",
        nargs="+",
        type=float,
        default=[0.0],
        help="aerial-bridge weights to sweep (0 = legacy distance-blind re-entry)",
    )
    ap.add_argument(
        "--save-net",
        default=None,
        help="persist the FIRST run's trained net (+feature mask) for replay_fullgame",
    )
    ap.add_argument(
        "--depth-balance",
        type=float,
        default=0.0,
        help="inverse-frequency reweight POSITIVE samples across --depth-bands "
        "quantile bands of the labeled ball's depth (0=off, 1=full inverse-freq, "
        "0.5=mild). Fixes near-band selector UNDER-CONFIDENCE: near balls are "
        "scarce in the gold, so the selector's P(ball) stays below P(none) even "
        "when the ball is candidate #1 (near-ball stage autopsy 2026-07-20: 11 "
        "of 19 near misses were the Viterbi taking the miss-state over a rank-1 "
        "near ball). Up-weights the rare near band so it learns confidence there.",
    )
    ap.add_argument("--depth-bands", type=int, default=4)
    args = ap.parse_args()

    from training.cli.sweep_tracker import (
        _ceiling,
        _hits,
        _score,
        continuity_line,
        track_continuity,
    )
    from training.models.selector_net import predict_probs, train_selector
    from training.world_model.reranker import RerankConfig, kalman_smooth, rerank
    from training.world_model.selector_features import (
        FEATURE_FAMILIES,
        FEATURE_NAMES,
        feature_mask,
    )

    # --knockouts sweep [extra...] = one run per family, with [extra...] features
    # ALWAYS dropped (e.g. size_ratio when training dumps carry no sizes)
    if args.knockouts and args.knockouts[0] == "sweep":
        always = args.knockouts[1:]
        runs = [always, *[[*always, f] for f in FEATURE_FAMILIES]]
    elif args.knockouts:
        runs = [args.knockouts]
    else:
        runs = [[]]

    # ---- load training pairs once -------------------------------------------------
    train_sets = []
    for pair in args.train:
        dump_path, labels_path = split_train_pair(pair)
        payload = json.loads(open(labels_path, encoding="utf-8").read())
        check_not_held_out(dump_path, str(payload.get("game_dir", "")))
        d, frames, geom = _load_dump(dump_path)
        lab = payload["labels"]
        train_sets.append((d, frames, geom, lab, dump_path))
        print(f"train {dump_path}: {len(lab)} labeled frames")
    evals = [(p, *_load_dump(p)) for p in args.eval]

    def line(name, det, near, far):
        a, nr, fr = _hits(det), _hits(near), _hits(far)
        print(
            f"  {name:<32} ALL R15 {str(a['R15']):<6} NEAR {str(nr['R15']):<6} "
            f"FAR {str(fr['R15']):<6} | med {a['med']}"
        )

    for knock in runs:
        keep = feature_mask(knock)
        kept = [n for n, k in zip(FEATURE_NAMES, keep, strict=True) if k]
        print(f"\n=== run: knockout={knock or 'none'} ({len(kept)} features) ===")

        # depth of the labeled ball (feature col, small=far) for depth-balancing;
        # None for 'none-visible' rows (no candidate) — those keep their weight.
        depth_col = kept.index("depth") if "depth" in kept else None
        fx, mx, lx, wx, dx = [], [], [], [], []
        for _d, frames, geom, lab, _p in train_sets:
            if not lab:
                print(f"WARNING: {_p} contributed 0 labels")
            feats, mask = _features_packed(frames, geom, keep, _d["ef"])
            top_k = feats.shape[1]
            for i_str, (cand, w) in lab.items():
                i = int(i_str)
                fx.append(feats[i])
                mx.append(mask[i])
                lx.append(top_k if cand < 0 else cand)
                wx.append(w)
                dx.append(
                    float(feats[i][cand][depth_col])
                    if cand >= 0 and depth_col is not None
                    else None
                )
        if not fx:
            raise SystemExit(
                "NO training labels at all — check the label builder's stats "
                "(teacher/dump frame-axis mismatch was the failure mode here once)"
            )
        feats = np.stack(fx)
        mask = np.stack(mx)
        labels = np.asarray(lx)
        weights = np.asarray(wx, np.float32)
        if args.depth_balance > 0 and depth_col is not None:
            pos = np.array([d for d in dx if d is not None])
            if len(pos):
                edges = np.quantile(pos, np.linspace(0, 1, args.depth_bands + 1))
                edges[0], edges[-1] = -np.inf, np.inf
                band = lambda d: int(np.searchsorted(edges, d, side="right") - 1)  # noqa: E731
                counts = np.zeros(args.depth_bands)
                for d in dx:
                    if d is not None:
                        counts[band(d)] += 1
                ref = counts[counts > 0].mean()
                for k2, d in enumerate(dx):
                    if d is not None and counts[band(d)] > 0:
                        weights[k2] *= float(
                            (ref / counts[band(d)]) ** args.depth_balance
                        )
                print(
                    f"depth-balance^{args.depth_balance}: band counts "
                    f"{counts.astype(int).tolist()} (far..near), weight range "
                    f"[{weights.min():.2f}, {weights.max():.2f}]"
                )
        n_none = int((labels == feats.shape[1]).sum())
        print(f"training frames {len(feats)} (none={n_none}), F={feats.shape[2]}")

        net, hist = train_selector(
            feats, mask, labels, weights, epochs=args.epochs, seed=args.seed
        )
        print(
            f"trained: best val loss {hist['best']:.4f} @ {hist['epochs_run']} epochs, "
            f"T={float(net.temperature):.2f}"
        )
        if args.save_net and knock == runs[0]:
            from training.models.selector_net import save_selector

            save_selector(net, keep, args.save_net)
            print(f"saved net -> {args.save_net}")

        for path, d, frames, geom in evals:
            ef, gaps, balls = d["ef"], d["gaps"], d["balls"]
            far_px, stride = d["far_size_px"], d["stride"]
            print(f"\n--- held-out {path} ---")
            cd, cn, cf = _ceiling(frames, ef, balls, geom, far_px, stride)
            line("ceiling (fixed by dump)", cd, cn, cf)
            raw = {
                i: (max(fr, key=lambda c: c.score).x, max(fr, key=lambda c: c.score).y)
                for i, fr in enumerate(frames)
                if fr
            }
            line(
                "raw score-argmax",
                *_score(raw, frames, ef, balls, geom, far_px, stride),
            )

            efeats, emask = _features_packed(frames, geom, keep, ef)
            probs = predict_probs(net, efeats, emask)
            learned = {}
            for i, fr in enumerate(frames):
                if not fr:
                    continue
                j = int(np.argmax(probs[i, : len(fr)]))
                learned[i] = (fr[j].x, fr[j].y)
            line(
                "LEARNED argmax",
                *_score(learned, frames, ef, balls, geom, far_px, stride),
            )
            print(
                "      "
                + continuity_line(track_continuity(learned, ef, balls, geom, stride))
            )

            base = replace(RerankConfig(), alpha=0.0, static_w=0.0, motion_w=0.0)
            p_none = probs[:, -1]
            for w in args.emission_weights:
                priors = [
                    w * -np.log(np.maximum(probs[i, : len(fr)], 1e-6))
                    if fr
                    else np.zeros(0)
                    for i, fr in enumerate(frames)
                ]
                # miss variants: flat cost, and the learned none-head per frame
                variants: list[tuple[str, float | None, list[float] | None]] = [
                    (f"miss={mc}", mc, None) for mc in args.miss_costs
                ]
                for s in args.pnone_scales:
                    mc_list = [
                        float(s * w * -np.log(max(float(p_none[i]), 1e-6)))
                        for i in range(len(frames))
                    ]
                    variants.append((f"pnone*{s}", None, mc_list))
                for mlabel, mc, mc_list in variants:
                    for bw in args.bridge_w:
                        cfg = replace(
                            base,
                            miss_cost=(mc or 0.9) * w,
                            bridge_w=bw,
                        )
                        sel = rerank(
                            frames,
                            geom,
                            frame_gaps=gaps,
                            priors=priors,
                            miss_costs=mc_list,
                            config=cfg,
                        )
                        track = kalman_smooth(sel, geom)
                        line(
                            f"tracker w={w} {mlabel} br={bw}",
                            *_score(track, frames, ef, balls, geom, far_px, stride),
                        )
                        print(
                            "      "
                            + continuity_line(
                                track_continuity(track, ef, balls, geom, stride)
                            )
                        )


if __name__ == "__main__":
    main()
