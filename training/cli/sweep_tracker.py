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
    > ``ramp`` * the perspective-expected ball size at their location."""
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


def _prior_support(frames, geom, w, margin=120.0, dome_px=0.0):
    """Soft off-field COST (never a hard gate — airborne far balls sit above the far
    line; ``dome_px`` carves out that zone so a flying ball is not penalised)."""
    out = []
    for cs in frames:
        if not cs:
            out.append(np.zeros(0))
            continue
        xy = np.asarray([(c.x, c.y) for c in cs], float)
        inside = geom.is_in_support(xy, margin_px=margin, dome_px=dome_px)
        out.append(w * (~np.asarray(inside)).astype(float))
    return out


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


def track_continuity(track, ef, balls, geom, stride, radius_m=15.0):
    """Viewport-shaped eval: the RUN STRUCTURE of hits over the GT timeline, not the
    per-frame hit rate. The render can absorb a one-frame misrank; it cannot absorb a
    multi-second excursion — so what matters is how LONG the track stays on the ball
    and how bad the worst off-ball stretch is (Mark 2026-07-06: the viewport looking
    in the right area IS the goal; detection/tracking is the mechanism).

    Returns ``{n, cov, frags, longest_frac, mean_run, worst_miss}`` where ``worst_miss``
    is the top-3 consecutive-miss stretches as ``(g_start, g_end)`` global-frame spans
    (directly adjudicable / clippable), or ``None`` with no scoreable GT. Runs break
    across label-span gaps (> 6 * stride) so separate GT clips don't concatenate.
    """
    fidx = {f: i for i, f in enumerate(ef)}
    events: list[tuple[int, bool]] = []
    for g in sorted(balls):
        nf = min(ef, key=lambda f: abs(f - g))
        if abs(nf - g) > stride:
            continue
        i = fidx[nf]
        hit = False
        if i in track:
            gw = geom.image_to_world(np.asarray([balls[g]], float))[0]
            tw = geom.image_to_world(np.asarray([track[i]], float))[0]
            hit = float(np.linalg.norm(tw - gw)) <= radius_m
        events.append((g, hit))
    if not events:
        return None
    span_gap = 6 * stride
    runs: list[list] = []  # [hit, g_start, g_end, n_events]
    for g, h in events:
        if runs and h == runs[-1][0] and g - runs[-1][2] <= span_gap:
            runs[-1][2] = g
            runs[-1][3] += 1
        else:
            runs.append([h, g, g, 1])
    hit_runs = [r for r in runs if r[0]]
    miss_runs = sorted((r for r in runs if not r[0]), key=lambda r: r[1] - r[2])
    n = len(events)
    return {
        "n": n,
        "cov": sum(1 for _, h in events if h) / n,
        "frags": len(hit_runs),
        "longest_frac": max((r[3] for r in hit_runs), default=0) / n,
        "mean_run": float(np.mean([r[3] for r in hit_runs])) if hit_runs else 0.0,
        "worst_miss": [(r[1], r[2]) for r in miss_runs[:3]],
    }


def continuity_line(cont) -> str:
    """One-line render of :func:`track_continuity` for eval printouts."""
    if not cont:
        return "continuity: no scoreable GT"
    wm = cont["worst_miss"]
    worst = f"{wm[0][1] - wm[0][0]}f @g{wm[0][0]}" if wm else "none"
    return (
        f"continuity: cov {cont['cov']:.2f}  frags {cont['frags']}  "
        f"longest {cont['longest_frac']:.2f}  mean {cont['mean_run']:.0f}ev  "
        f"worst-miss {worst}"
    )


def rank_table(frames, ef, balls, geom, far_px, stride, radius_m=15.0):
    """Score-RANK of the GT ball among its frame's candidates (the A-vs-B diagnostic).

    For each GT ball: the candidate nearest in meters is "the ball" when within
    ``radius_m``; record its 1-based rank in the frame's score ordering, else
    ``None`` (absent — includes empty frames, unlike ``_ceiling`` which skips
    them). Bands split by expected apparent size, same as ``_score``/``_ceiling``.
    Ball usually rank 2-5 => a context re-ranker can fix selection; often rank
    11+/absent => the detector genuinely buries it.
    """
    fidx = {f: i for i, f in enumerate(ef)}
    ranks: dict[str, list[int | None]] = {"near": [], "far": []}
    for g, gt in balls.items():
        nf = min(ef, key=lambda f: abs(f - g))
        if abs(nf - g) > stride:
            continue
        size = float(geom.expected_ball_diameter_px(np.asarray([gt], float))[0])
        band = "far" if size < far_px else "near"
        cs = frames[fidx[nf]]
        if not cs:
            ranks[band].append(None)
            continue
        gw = geom.image_to_world(np.asarray([gt], float))[0]
        cw = geom.image_to_world(np.asarray([(c.x, c.y) for c in cs], float))
        derr = np.linalg.norm(cw - gw, axis=1)
        i_ball = int(np.argmin(derr))
        if float(derr[i_ball]) > radius_m:
            ranks[band].append(None)
            continue
        order = np.argsort([-c.score for c in cs], kind="stable")
        ranks[band].append(int(np.where(order == i_ball)[0][0]) + 1)
    return ranks


def _print_rank_table(ranks):
    print(
        "\nRANK diagnostic (GT ball's score-rank among its frame's candidates; "
        "fractions of ALL GT in band):"
    )
    buckets = (("r1", 1, 1), ("r2-3", 2, 3), ("r4-5", 4, 5), ("r6-10", 6, 10))
    for band in ("near", "far"):
        rs = ranks[band]
        n = len(rs)
        if not n:
            print(f"  {band:<5} n=0")
            continue
        present = [r for r in rs if r is not None]
        parts = [
            f"{k} {sum(1 for r in present if lo <= r <= hi) / n:.2f}"
            for k, lo, hi in buckets
        ]
        parts.append(f"r11+ {sum(1 for r in present if r >= 11) / n:.2f}")
        parts.append(f"absent {(n - len(present)) / n:.2f}")
        cum = " ".join(
            f"{sum(1 for r in present if r <= k) / n:.2f}" for k in (1, 3, 5, 10)
        )
        med = int(np.median(present)) if present else None
        print(
            f"  {band:<5} n={n:<5} "
            + " ".join(parts)
            + f" | P(rank<=1/3/5/10) {cum} | med-rank {med}"
        )


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
    ap.add_argument(
        "--rank-only",
        action="store_true",
        help="print ceiling + argmax + the RANK diagnostic, skip the config sweep",
    )
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
            f"  {name:<26} ALL R15 {str(a['R15']):<6} NEAR {str(nr['R15']):<6} "
            f"FAR {str(fr['R15']):<6} | ALL med {a['med']}"
        )

    print(
        f"\n=== dump: {sum(len(f) for f in frames)} cands / {len(ef)} frames, "
        f"{len(balls)} GT (far<{far_px}px) ==="
    )
    cd, cn, cf = _ceiling(frames, ef, balls, geom, far_px, stride)
    print("CEILING (config-independent bar):")
    line("candidate ceiling", cd, cn, cf)

    _print_rank_table(rank_table(frames, ef, balls, geom, far_px, stride))

    base = RerankConfig()
    print("\nselected R15m by variant:")
    argmax = {
        i: (max(fr, key=lambda c: c.score).x, max(fr, key=lambda c: c.score).y)
        for i, fr in enumerate(frames)
        if fr
    }
    line(
        "score-argmax (no track)",
        *_score(argmax, frames, ef, balls, geom, far_px, stride),
    )

    # Depth-calibrated confidence (EXP-DIST-22: the raw sigmoid is saturated and carries
    # ~no cross-frame ranking signal): re-score every candidate as its score PERCENTILE
    # within the game's depth band, so a far ball competes against far peers instead of
    # being buried under confident near distractors.
    from training.world_model.selector_features import FEATURE_NAMES, build_features

    i_pd = FEATURE_NAMES.index("pct_depth")
    feats = build_features(frames, geom)
    dc_frames = [
        [
            Candidate(x=c.x, y=c.y, score=float(fx[i, i_pd]), size_px=c.size_px)
            for i, c in enumerate(cs)
        ]
        for cs, fx in zip(frames, feats, strict=True)
    ]
    dc_argmax = {
        i: (max(fr, key=lambda c: c.score).x, max(fr, key=lambda c: c.score).y)
        for i, fr in enumerate(dc_frames)
        if fr
    }
    line(
        "DEPTH-CAL argmax",
        *_score(dc_argmax, frames, ef, balls, geom, far_px, stride),
    )
    dc_cfg = replace(RerankConfig(), alpha=1.0, phys_sigma_px=5.0, ball_vmax_mpf=2.5)
    dc_track = kalman_smooth(
        rerank(dc_frames, geom, frame_gaps=gaps, config=dc_cfg), geom
    )
    line(
        "DEPTH-CAL tracker a1 sig5 vmax2.5",
        *_score(dc_track, frames, ef, balls, geom, far_px, stride),
    )

    if args.rank_only:
        return

    def run(name, cfg, *, prior=None, use_kalman=True):
        pr = None
        if prior == "size":
            pr = _prior_size(frames, geom, 2.0)
        elif prior == "support":
            pr = _prior_support(frames, geom, 2.0)
        elif prior == "support_dome":
            # soft off-field cost with the airborne dome carved out: penalise the
            # far-margin/edge statics WITHOUT punishing a ball flying above the far line
            pr = _prior_support(frames, geom, 2.0, dome_px=400.0)
        sel = rerank(frames, geom, frame_gaps=gaps, priors=pr, config=cfg)
        track = kalman_smooth(sel, geom) if use_kalman else sel
        line(name, *_score(track, frames, ef, balls, geom, far_px, stride))

    run("baseline (defaults)", base)
    # physics x alpha grid (static_w kept at 2.0 — reducing it hurt): sweep the
    # measurement-noise jitter (phys_sigma_px) x real ball-speed ceiling (ball_vmax_mpf)
    for a in (0.3, 1.0, 3.0):
        for sig, vmax in ((3.0, 2.5), (5.0, 2.5), (8.0, 3.5)):
            run(
                f"a{a} sig{sig} vmax{vmax}",
                replace(base, alpha=a, phys_sigma_px=sig, ball_vmax_mpf=vmax),
            )
    # size-continuity (EXP-DIST-47 Phase 4): candidates/2 dumps carry per-candidate
    # blob sizes, making the tracker's size_cont_w term live — sweep it on the
    # strongest physics family (a small ball must not hand off to a 200px person).
    for szc in (2.0, 4.0, 8.0):
        for a in (0.3, 1.0):
            run(
                f"a{a} sig8.0 vmax3.5 szc{szc}",
                replace(
                    base,
                    alpha=a,
                    phys_sigma_px=8.0,
                    ball_vmax_mpf=3.5,
                    size_cont_w=szc,
                ),
            )
    # Kalman ablation on a strong config — does the CV smoother drag NEAR picks off the ball?
    strong = replace(base, alpha=1.0, phys_sigma_px=5.0, ball_vmax_mpf=2.5)
    run("strong +kalman", strong, use_kalman=True)
    run("strong  NO-kalman", strong, use_kalman=False)
    # soft in-field prior (EXP-DIST-17 found a HARD gate useless — distractors were
    # in-field — but current-model statics leak in the far-margin/edge zones)
    run("strong +support", strong, prior="support")
    run("strong +support+dome", strong, prior="support_dome")
    # very loose (near-ungated) + trust detector — approaches argmax while keeping far coasting
    run(
        "a3 vmax30 +kal",
        replace(base, alpha=3.0, phys_sigma_px=5.0, ball_vmax_mpf=30.0),
    )
    run(
        "a3 vmax30 NO-kal",
        replace(base, alpha=3.0, phys_sigma_px=5.0, ball_vmax_mpf=30.0),
        use_kalman=False,
    )

    # Confidence-hybrid: where the detector's top RAW score is high (typically the bright near ball —
    # argmax nails near, the global-smooth tracker drags it off), trust argmax; else the tracker (weak
    # far balls need continuity). Tests the near fix without a rerank change.
    print("\nconfidence-hybrid (argmax where max-score>=T, else tracker):")
    strong = replace(base, alpha=1.0, phys_sigma_px=5.0, ball_vmax_mpf=2.5)
    strong_track = kalman_smooth(
        rerank(frames, geom, frame_gaps=gaps, config=strong), geom
    )
    for T in (0.2, 0.3, 0.5, 0.7):
        hyb = {}
        for i, fr in enumerate(frames):
            if not fr:
                continue
            top = max(fr, key=lambda c: c.score)
            if top.score >= T:
                hyb[i] = (top.x, top.y)
            elif i in strong_track:
                hyb[i] = strong_track[i]
        line(f"hybrid conf>={T}", *_score(hyb, frames, ef, balls, geom, far_px, stride))


if __name__ == "__main__":
    main()
