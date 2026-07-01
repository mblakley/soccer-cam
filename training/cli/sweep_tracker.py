"""Fast tracker-config sweep over a cached candidate dump — no re-decode.

``eval_detector --dump-cands`` writes per-frame candidates (+ observed size) + human GT once (~40 min,
decode-bound). This replays ``world_model.reranker`` over that dump under many ``RerankConfig`` / prior
variants in *seconds* each, so we find what actually closes the ceiling→selected gap without a loop of
40-minute evals. The candidate set is fixed by the dump, so the **ceiling is config-independent** — it's
printed once as the bar; each variant reports only what the tracker *selects*.

Includes a **score-argmax (no tracker)** reference: pick the highest-score candidate per frame, no
continuity. If that beats the full tracker, the tracker is actively hurting.

    python -m training.cli.sweep_tracker --dump G:/ballresearch/distill/cands_spencerport.pkl
"""

from __future__ import annotations

import argparse
import pickle
from dataclasses import replace

import numpy as np


def _hits(errs, radii=(5, 10, 15)):
    if not errs:
        return {f"R{r}": None for r in radii} | {"n": 0, "med": None}
    e = np.asarray(errs)
    return {f"R{r}": round(float((e <= r).mean()), 3) for r in radii} | {
        "n": len(e),
        "med": round(float(np.median(e)), 1),
    }


def _prior_size(frames, geom, w, ramp=1.5):
    """Soft, asymmetric size penalty (additive COST): penalize candidates whose observed diameter is
    > ``ramp`` * the perspective-expected ball size at their location. Soft (never removes) so it can't
    drop the ceiling; asymmetric (too-big only) so a real ball measured a bit large is untouched."""
    out = []
    for cs in frames:
        if not cs:
            out.append(np.zeros(0))
            continue
        p = []
        for c in cs:
            exp = float(
                geom.expected_ball_diameter_px(np.asarray([(c.x, c.y)], float))[0]
            )
            r = (c.size_px / exp) if (exp > 0 and c.size_px) else 0.0
            p.append(w * max(0.0, r - ramp) ** 2)
        out.append(np.asarray(p))
    return out


def _prior_support(frames, geom, w, margin=120.0):
    out = []
    for cs in frames:
        if not cs:
            out.append(np.zeros(0))
            continue
        xy = np.asarray([(c.x, c.y) for c in cs], float)
        inside = geom.is_in_support(xy, margin_px=margin)
        out.append(w * (~np.asarray(inside)).astype(float))
    return out


def _sum_priors(*plists):
    plists = [p for p in plists if p is not None]
    if not plists:
        return None
    return [sum(pl[t] for pl in plists) for t in range(len(plists[0]))]


def _score(track, frames, ef, balls, geom, far_px, stride):
    fidx = {f: i for i, f in enumerate(ef)}
    det, far, near = [], [], []
    for g, gt in balls.items():
        nf = min(ef, key=lambda f: abs(f - g))
        if abs(nf - g) > stride or fidx[nf] not in track:
            continue
        gw = geom.image_to_world(np.asarray([gt], float))[0]
        tw = geom.image_to_world(np.asarray([track[fidx[nf]]], float))[0]
        err = float(np.linalg.norm(tw - gw))
        size = float(geom.expected_ball_diameter_px(np.asarray([gt], float))[0])
        det.append(err)
        (far if size < far_px else near).append(err)
    return det, near, far


def _ceiling(frames, ef, balls, geom, far_px, stride):
    fidx = {f: i for i, f in enumerate(ef)}
    det, far, near = [], [], []
    for g, gt in balls.items():
        nf = min(ef, key=lambda f: abs(f - g))
        if abs(nf - g) > stride or not frames[fidx[nf]]:
            continue
        gw = geom.image_to_world(np.asarray([gt], float))[0]
        cs = frames[fidx[nf]]
        ce = min(
            float(
                np.linalg.norm(
                    geom.image_to_world(np.asarray([(c.x, c.y)], float))[0] - gw
                )
            )
            for c in cs
        )
        size = float(geom.expected_ball_diameter_px(np.asarray([gt], float))[0])
        det.append(ce)
        (far if size < far_px else near).append(ce)
    return det, near, far


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", required=True)
    args = ap.parse_args()

    from training.world_model.geometry import build_field_geometry
    from training.world_model.reranker import RerankConfig, kalman_smooth, rerank
    from training.world_model.tbd import Candidate

    with open(args.dump, "rb") as fh:
        d = pickle.load(fh)
    geom = build_field_geometry(np.asarray(d["polygon"], float))
    ef, gaps, balls = d["ef"], d["gaps"], d["balls"]
    far_px, stride = d["far_size_px"], d["stride"]
    frames = [
        [Candidate(x=x, y=y, score=s, size_px=sz) for (x, y, s, sz) in d["cands"][f]]
        for f in ef
    ]

    def line(name, det, near, far):
        a, nr, fr = _hits(det), _hits(near), _hits(far)
        print(
            f"  {name:<34} ALL R15 {str(a['R15']):<6} NEAR {str(nr['R15']):<6} "
            f"FAR {str(fr['R15']):<6} | ALL med {a['med']}"
        )

    print(
        f"\n=== dump: {sum(len(f) for f in frames)} cands / {len(ef)} frames, "
        f"{len(balls)} GT (far<{far_px}px) ==="
    )
    cd, cn, cf = _ceiling(frames, ef, balls, geom, far_px, stride)
    print("CEILING (config-independent bar):")
    line("candidate ceiling", cd, cn, cf)

    base = RerankConfig()
    print("\nselected R15m by variant:")
    # score-argmax (no tracker): trust the detector's top peak per frame
    argmax = {
        i: (max(fr, key=lambda c: c.score).x, max(fr, key=lambda c: c.score).y)
        for i, fr in enumerate(frames)
        if fr
    }
    line(
        "score-argmax (no tracker)",
        *_score(argmax, frames, ef, balls, geom, far_px, stride),
    )

    variants = [
        ("baseline (a0.3 static2)", base, None),
        ("alpha 1.0", replace(base, alpha=1.0), None),
        ("alpha 3.0", replace(base, alpha=3.0), None),
        ("alpha 10", replace(base, alpha=10.0), None),
        ("static_w 0.5", replace(base, static_w=0.5), None),
        ("static_w 0.0", replace(base, static_w=0.0), None),
        (
            "loose teleport (mj20 v8)",
            replace(base, max_jump_m_per_frame=20.0, vmax_m_per_frame=8.0),
            None,
        ),
        ("a3 + static0.5", replace(base, alpha=3.0, static_w=0.5), None),
        (
            "a3 + loose teleport",
            replace(base, alpha=3.0, max_jump_m_per_frame=20.0, vmax_m_per_frame=8.0),
            None,
        ),
        ("a3 + size prior", replace(base, alpha=3.0), ("size", 2.0)),
        ("a3 + support prior", replace(base, alpha=3.0), ("support", 2.0)),
        (
            "a3 static0.5 loose + size",
            replace(
                base,
                alpha=3.0,
                static_w=0.5,
                max_jump_m_per_frame=20.0,
                vmax_m_per_frame=8.0,
            ),
            ("size", 2.0),
        ),
    ]
    for name, cfg, prior in variants:
        pr = None
        if prior and prior[0] == "size":
            pr = _prior_size(frames, geom, prior[1])
        elif prior and prior[0] == "support":
            pr = _prior_support(frames, geom, prior[1])
        sel = rerank(frames, geom, frame_gaps=gaps, priors=pr, config=cfg)
        track = kalman_smooth(sel, geom)
        line(name, *_score(track, frames, ef, balls, geom, far_px, stride))


if __name__ == "__main__":
    main()
