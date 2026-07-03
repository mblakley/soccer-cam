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
      --train dumpA.pkl:labelsA.json dumpB.pkl:labelsB.json \
      --eval cands_spc_hn2.pkl cands_iron_hn2.pkl
"""

from __future__ import annotations

import argparse
import json
import pickle
from dataclasses import replace

import numpy as np


def _load_dump(path):
    from training.world_model.geometry import build_field_geometry
    from training.world_model.tbd import Candidate

    with open(path, "rb") as fh:
        d = pickle.load(fh)
    frames = [
        [Candidate(x=x, y=y, score=s, size_px=sz) for (x, y, s, sz) in d["cands"][f]]
        for f in d["ef"]
    ]
    geom = build_field_geometry(np.asarray(d["polygon"], float))
    return d, frames, geom


def _features_packed(frames, geom, keep):
    from training.models.selector_net import pack_frames
    from training.world_model.selector_features import build_features

    feats = build_features(frames, geom)
    feats = [x[:, keep] for x in feats]
    return pack_frames(feats)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--train",
        nargs="+",
        required=True,
        help="dump.pkl:selector_labels.json pairs (training games)",
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
    args = ap.parse_args()

    from training.cli.sweep_tracker import _ceiling, _hits, _score
    from training.models.selector_net import predict_probs, train_selector
    from training.world_model.reranker import RerankConfig, kalman_smooth, rerank
    from training.world_model.selector_features import (
        FEATURE_FAMILIES,
        FEATURE_NAMES,
        feature_mask,
    )

    runs = (
        [[]]
        if not args.knockouts
        else (
            [[], *[[f] for f in FEATURE_FAMILIES]]
            if args.knockouts == ["sweep"]
            else [args.knockouts]
        )
    )

    # ---- load training pairs once -------------------------------------------------
    train_sets = []
    for pair in args.train:
        dump_path, labels_path = pair.rsplit(":", 1)
        d, frames, geom = _load_dump(dump_path)
        lab = json.loads(open(labels_path, encoding="utf-8").read())["labels"]
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

        fx, mx, lx, wx = [], [], [], []
        for _d, frames, geom, lab, _p in train_sets:
            feats, mask = _features_packed(frames, geom, keep)
            top_k = feats.shape[1]
            for i_str, (cand, w) in lab.items():
                i = int(i_str)
                fx.append(feats[i])
                mx.append(mask[i])
                lx.append(top_k if cand < 0 else cand)
                wx.append(w)
        feats = np.stack(fx)
        mask = np.stack(mx)
        labels = np.asarray(lx)
        weights = np.asarray(wx, np.float32)
        n_none = int((labels == feats.shape[1]).sum())
        print(f"training frames {len(feats)} (none={n_none}), F={feats.shape[2]}")

        net, hist = train_selector(
            feats, mask, labels, weights, epochs=args.epochs, seed=args.seed
        )
        print(
            f"trained: best val loss {hist['best']:.4f} @ {hist['epochs_run']} epochs, "
            f"T={float(net.temperature):.2f}"
        )

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

            efeats, emask = _features_packed(frames, geom, keep)
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

            base = replace(RerankConfig(), alpha=0.0, static_w=0.0, motion_w=0.0)
            for w in args.emission_weights:
                priors = [
                    w * -np.log(np.maximum(probs[i, : len(fr)], 1e-6))
                    if fr
                    else np.zeros(0)
                    for i, fr in enumerate(frames)
                ]
                for mc in args.miss_costs:
                    cfg = replace(base, miss_cost=mc * w)
                    sel = rerank(
                        frames, geom, frame_gaps=gaps, priors=priors, config=cfg
                    )
                    track = kalman_smooth(sel, geom)
                    line(
                        f"tracker w={w} miss={mc}",
                        *_score(track, frames, ef, balls, geom, far_px, stride),
                    )


if __name__ == "__main__":
    main()
