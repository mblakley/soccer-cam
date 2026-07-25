"""Score the world-model on an AutoCam-loses-ball clip vs human GT and AutoCam.

The gold-standard validation (``autocam_loses_ball.md``): for a clip Mark flagged
where AutoCam visibly loses the ball, we dump per-frame J peaks + MOG2 motion blobs
(``<set>.json``), Mark labels the true ball position via the far-label app
(``labels.json``), and we compare the world-model track to AutoCam's logged viewport
(``autocam_<set>.json``) on the viewport-area metric (recall at a radius sweep up to
R=400, the rendered-crop scale, per *viewport/area recall is the real metric*).

This generalises :mod:`iron_eval` (Irondequoit) to the far-label clip format, so the
same harness scores every clip in the registry. Inputs (all SOURCE-pixel coords):

- ``--peaks <set>.json``   ``{lo, hi, polygon, frames:{idx:{j:[[x,y,s]..], m:[[x,y,area]..]}}}``
- ``--labels labels.json`` far-label GT: ``[{frame_idx, action, x, y}]`` (action
  ``ball`` = positioned GT; ``not_visible`` = occluded, no position, excluded from recall)
- ``--autocam autocam_<set>.json`` (optional)  ``{frame: [x, y]}`` AutoCam viewport

    python -m training.world_model.clip_eval --peaks spc_clip1.json \
        --labels labels.json --autocam autocam_clip1.json

Pure numpy. No GPU — candidates are precomputed by the dump harness.
"""

from __future__ import annotations

import argparse
import json
import math

import numpy as np

from training.world_model.eval import evaluate_recall
from training.world_model.geometry import build_field_geometry
from training.world_model.measurements import suppress_static_candidates
from training.world_model.tbd import Candidate
from training.world_model.tracker import (
    MHTConfig,
    TrackerConfig,
    causal_track,
    causal_track_fused,
    multi_hypothesis_track,
)

# Appearance score given to a motion/action blob when it is offered as a
# *selectable* candidate (variant B). Low and fixed: a player-action blob is weak
# evidence of a precise ball, so the beam picks it only to fill an appearance gap
# (via continuity), never because it is "bright" (its raw value is pixel area, not
# a ball confidence — feeding that as score would swamp the [0,1] J peaks).
MOTION_FILL_SCORE = 0.2

RADII = [20, 100, 200, 400]
FAR_Y = (
    450.0  # all far-label clip GT sits well above this → the whole clip is "veryfar"
)


def load_clip(peaks_path: str):
    """Load a dump ``<set>.json`` → (lo, hi, appearance, action, polygon).

    ``appearance[i]`` / ``action[i]`` are the candidate lists for source frame
    ``lo + i`` (missing frames → empty lists), so the tracker sees a dense,
    consecutive sequence.
    """
    with open(peaks_path) as f:
        data = json.load(f)
    lo, hi = int(data["lo"]), int(data["hi"])
    fr = data["frames"]
    appearance: list[list[Candidate]] = []
    action: list[list[Candidate]] = []
    for t in range(lo, hi + 1):
        cell = fr.get(str(t), {})
        appearance.append(
            [
                Candidate(float(p[0]), float(p[1]), float(p[2]))
                for p in cell.get("j", [])
            ]
        )
        action.append(
            [
                Candidate(float(p[0]), float(p[1]), float(p[2]))
                for p in cell.get("m", [])
            ]
        )
    poly = data.get("polygon")
    return lo, hi, appearance, action, poly


def load_gt(labels_path: str) -> list[tuple[int, float, float]]:
    """Far-label ``labels.json`` → positioned GT ``[(frame, x, y)]`` (``ball`` only)."""
    with open(labels_path) as f:
        labels = json.load(f)
    gt = []
    for e in labels:
        if (
            e.get("action") == "ball"
            and e.get("x") is not None
            and e.get("y") is not None
        ):
            gt.append((int(e["frame_idx"]), float(e["x"]), float(e["y"])))
    return sorted(gt)


def load_autocam(path: str) -> dict[int, tuple[float, float]]:
    with open(path) as f:
        ac = json.load(f)
    return {int(k): (float(v[0]), float(v[1])) for k, v in ac.items()}


def _preds(result, lo: int) -> dict[int, tuple[float, float]]:
    """Track points (list-indexed) → {source_frame: (x, y)}."""
    return {lo + p.frame_idx: (p.x, p.y) for p in result.points}


def median_dist(
    preds: dict[int, tuple[float, float]], gt: list[tuple[int, float, float]]
) -> float:
    """Median distance (px) from prediction to GT over GT frames (miss → inf)."""
    ds = []
    for f, gx, gy in gt:
        p = preds.get(f)
        ds.append(math.hypot(p[0] - gx, p[1] - gy) if p else float("inf"))
    return float(np.median(ds)) if ds else float("inf")


def report(name: str, preds: dict, gt, n_cands_note: str = "") -> None:
    cells = []
    for rad in RADII:
        res = evaluate_recall(preds, gt, radius_px=float(rad), far_y_threshold=FAR_Y)
        cells.append(f"@{rad}={res.recall_all:.3f}")
    md = median_dist(preds, gt)
    mds = f"{md:.0f}" if math.isfinite(md) else "inf"
    print(f"  {name:30s} " + " ".join(cells) + f"  | med={mds}px {n_cands_note}")


def candidate_recall(cands: list[list[Candidate]], gt, lo: int) -> None:
    """Is the ball anywhere in the candidate set? (the tracker's ceiling.)"""
    gtd = {f: (x, y) for f, x, y in gt}
    for rad in (200, 400):
        hit = tot = 0
        for f, (gx, gy) in gtd.items():
            i = f - lo
            if 0 <= i < len(cands):
                tot += 1
                if any(math.hypot(c.x - gx, c.y - gy) <= rad for c in cands[i]):
                    hit += 1
        print(f"    candidate-recall @R{rad}: {hit / tot:.3f} ({hit}/{tot})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--peaks", required=True)
    ap.add_argument("--labels", required=True)
    ap.add_argument("--autocam", default="")
    ap.add_argument(
        "--polygon",
        default="",
        help="override polygon json (else use the one in --peaks)",
    )
    A = ap.parse_args()

    lo, hi, appearance, action, poly = load_clip(A.peaks)
    gt = load_gt(A.labels)
    if A.polygon:
        with open(A.polygon) as f:
            poly = json.load(f)["polygon"]
    geom = build_field_geometry(np.asarray(poly, float) if poly else None)
    ys = [y for _, _, y in gt]
    yspan = f"y {min(ys):.0f}..{max(ys):.0f}" if ys else "no GT"
    print(
        f"clip {A.peaks}: frames {lo}..{hi} ({hi - lo + 1}), {len(gt)} positioned GT ({yspan})"
    )
    print(
        f"geometry: valid_homography={geom.valid}, polygon={geom.polygon is not None}"
    )

    suppressed, static = suppress_static_candidates(appearance, motion=action)
    print(f"suppressed {len(static)} static background cells (motion-protected)\n")
    print("candidate ceilings (is the ball anywhere in the set — the tracker's max):")
    print("  appearance only (J peaks):")
    candidate_recall(suppressed, gt, lo)
    fused_cands = [
        a + [Candidate(c.x, c.y, MOTION_FILL_SCORE) for c in m]
        for a, m in zip(suppressed, action, strict=True)
    ]
    print("  fused (J peaks + motion):")
    candidate_recall(fused_cands, gt, lo)
    print(f"\nviewport area-recall (veryfar) at R = {RADII} px:")

    fw, fh = 7680.0, 2160.0
    if A.autocam:
        ac = load_autocam(A.autocam)
        report(
            "AutoCam (logged viewport)",
            {f: ac[f] for f in ac if f in {g[0] for g in gt}},
            gt,
        )
    # per-frame argmax of raw J (the wall)
    report(
        "argmax raw J",
        {
            lo + i: (max(c, key=lambda z: z.score).x, max(c, key=lambda z: z.score).y)
            for i, c in enumerate(appearance)
            if c
        },
        gt,
    )
    # causal continuity tracker (appearance only)
    report(
        "causal (J only)",
        _preds(
            causal_track(
                suppressed,
                geom,
                TrackerConfig(
                    gate0=80, max_lost=8, vel_alpha=0.5, frame_w=fw, frame_h=fh
                ),
            ),
            lo,
        ),
        gt,
    )
    # fused: causal + action-area prior (EXP-5/8 — the previous best, 0.52)
    base_cfg = {
        "gate0": 80,
        "max_lost": 8,
        "vel_alpha": 0.5,
        "action_pull": 0.6,
        "action_radius": 300,
        "frame_w": fw,
        "frame_h": fh,
    }
    report(
        "fused (causal + action)",
        _preds(
            causal_track_fused(suppressed, action, geom, TrackerConfig(**base_cfg)), lo
        ),
        gt,
    )
    # fused + static-aware selection (EXP-9 — don't acquire/follow persistent-static
    # detector FPs; the validated cross-game win).
    report(
        "fused + static-aware",
        _preds(
            causal_track_fused(
                suppressed, action, geom, TrackerConfig(static_thresh=0.3, **base_cfg)
            ),
            lo,
        ),
        gt,
    )
    # Phase-2 multi-hypothesis (beam) — documented NEGATIVE result: global-max over
    # the beam reintroduces the EXP-1 distractor-lock (the dim/intermittent ball
    # loses to a bright distractor under any appearance-summing objective).
    mcfg = MHTConfig(frame_w=fw, frame_h=fh)
    report(
        "MHT-B (fused, beam) [neg]",
        _preds(multi_hypothesis_track(fused_cands, action, geom, mcfg), lo),
        gt,
    )


if __name__ == "__main__":
    main()
