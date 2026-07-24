"""Static-persistence candidate filter — model-free, CPU, dump-level.

Furniture (static ball-sized distractors) sits at the same source position in
essentially every frame; the ball does not. For each candidate, measure the
fraction of eval frames that have ANY candidate within ``--radius`` px of its
position; down-weight candidates whose persistence exceeds ``--thresh``.

Born 2026-07-24 from the FAIR-6k autopsy (DECISIONS (l)): one static object at
(1637, 470) was rank-0 in every Phase 2 arm's top-24 on 400/400 frames and
single-handedly produced the geo/norm argmax collapse. Third instance of the
size-is-not-a-discriminator lesson (ball/head, ball/furniture) — temporal
context is the discriminator each time. This filter is the temporal half of the
#19 pair (geometry-derived size prior + static-persistence filter).

Validation CLI — rescores cached eval dumps and prints per-arm top-1 accuracy
before/after plus pairwise event flips after:

    python -m training.experiments.static_persistence \
        --dump-dir G:/ballresearch/geodet --inst fair --arms mg_ctrl mg_geo mg_norm
"""

from __future__ import annotations

import argparse
import bisect
import math
import pickle
from collections import defaultdict
from pathlib import Path

GAP = 64  # event clustering, matches compose_verdict


def persistence_map(
    cands_by_frame: dict[int, list], radius: float
) -> dict[tuple[int, int], float]:
    """cell -> fraction of frames with any candidate in that radius-sized cell."""
    occ: dict[tuple[int, int], set[int]] = defaultdict(set)
    for f, cs in cands_by_frame.items():
        for c in cs:
            occ[(int(c[0] // radius), int(c[1] // radius))].add(f)
    n = max(1, len(cands_by_frame))
    return {cell: len(fs) / n for cell, fs in occ.items()}


def candidate_persistence(
    pm: dict[tuple[int, int], float], radius: float, x: float, y: float
) -> float:
    """max persistence over the candidate's cell and its 8 neighbors."""
    cx, cy = int(x // radius), int(y // radius)
    return max(
        pm.get((cx + dx, cy + dy), 0.0) for dx in (-1, 0, 1) for dy in (-1, 0, 1)
    )


def filter_cands(
    cands_by_frame: dict[int, list],
    radius: float = 12.0,
    thresh: float = 0.6,
    penalty: float = 0.2,
) -> dict[int, list]:
    """Down-weight (never delete) persistent-static candidates; re-rank."""
    pm = persistence_map(cands_by_frame, radius)
    out: dict[int, list] = {}
    for f, cs in cands_by_frame.items():
        rescored = [
            (
                c[0],
                c[1],
                c[2]
                * (
                    penalty
                    if candidate_persistence(pm, radius, c[0], c[1]) > thresh
                    else 1.0
                ),
            )
            + tuple(c[3:])
            for c in cs
        ]
        rescored.sort(key=lambda t: -t[2])
        out[f] = rescored
    return out


def _events(frames: list[int]) -> list[list[int]]:
    frames = sorted(frames)
    if not frames:
        return []
    ev = [[frames[0]]]
    for f in frames[1:]:
        (ev[-1].append(f) if f - ev[-1][-1] <= GAP else ev.append([f]))
    return ev


def _top1_hits(
    cands: dict[int, list], balls: dict[int, tuple], hit_px: float
) -> dict[int, bool]:
    """GT frame -> top-1 within hit_px (GT mapped to nearest eval frame, +/-2)."""
    ks = sorted(cands)
    out: dict[int, bool] = {}
    for f, g in balls.items():
        i = bisect.bisect_left(ks, f)
        near = next(
            (
                ks[j]
                for j in (i - 1, i, i + 1)
                if 0 <= j < len(ks) and abs(ks[j] - f) <= 2
            ),
            None,
        )
        if near is None or not cands[near]:
            continue
        t = cands[near][0]
        out[f] = math.hypot(t[0] - g[0], t[1] - g[1]) <= hit_px
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump-dir", required=True)
    ap.add_argument("--inst", default="fair")
    ap.add_argument("--arms", nargs="+", required=True)
    ap.add_argument("--radius", type=float, default=12.0)
    ap.add_argument("--thresh", type=float, default=0.6)
    ap.add_argument("--penalty", type=float, default=0.2)
    ap.add_argument("--hit-px", type=float, default=40.0)
    args = ap.parse_args()

    dd_ = Path(args.dump_dir)
    hits_pre: dict[str, dict[int, bool]] = {}
    hits_post: dict[str, dict[int, bool]] = {}
    for arm in args.arms:
        d = pickle.loads((dd_ / f"cands_{args.inst}_{arm}.pkl").read_bytes())
        balls = d["balls"]
        hits_pre[arm] = _top1_hits(d["cands"], balls, args.hit_px)
        filt = filter_cands(d["cands"], args.radius, args.thresh, args.penalty)
        hits_post[arm] = _top1_hits(filt, balls, args.hit_px)

    print(
        f"=== static-persistence filter on {args.inst} "
        f"(radius {args.radius}, thresh {args.thresh}, penalty {args.penalty}, "
        f"hit {args.hit_px}px) ==="
    )
    for arm in args.arms:
        pre, post = hits_pre[arm], hits_post[arm]
        n = len(pre)
        print(
            f"  {arm:<10} top-1: {sum(pre.values())}/{n} = {sum(pre.values()) / n:.3f}"
            f"  ->  {sum(post.values())}/{len(post)} = {sum(post.values()) / max(len(post), 1):.3f}"
        )

    base = args.arms[0]
    for arm in args.arms[1:]:
        common = sorted(set(hits_post[base]) & set(hits_post[arm]))
        a_only = [f for f in common if hits_post[arm][f] and not hits_post[base][f]]
        b_only = [f for f in common if hits_post[base][f] and not hits_post[arm][f]]
        print(
            f"  POST-filter flips {arm} vs {base}: "
            f"ev{len(_events(a_only))}v{len(_events(b_only))} "
            f"(frames {len(a_only)}v{len(b_only)})"
        )


if __name__ == "__main__":
    main()
