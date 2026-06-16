"""Decisive pre-GPU experiment: world-model TBD over champion-J's peaks.

Loads the ``iron_peaks.json`` produced by the dump harness (per-frame top-K
champion-J heatmap peaks in SOURCE coords + GT + splits for the Irondequoit
held-out clip) and compares, on the honest center-distance metric (R=20px,
splits all/veryfar/acmissed):

- **per-frame argmax** — what champion-J's full-frame search does (the wall:
  veryfar ~0.29, false-fire ~76%);
- **world-model track-before-detect** — keeps the top-K peaks and decodes the
  single physically-consistent trajectory (this module's hypothesis);

against the **AutoCam baseline (veryfar 0.74)**. A meaningful lift of veryfar
recall from ~0.29 toward/past 0.74 proves the world-model spine before any GPU
training. No GPU needed here — peaks are precomputed.

    python -m training.world_model.iron_eval --peaks iron_peaks.json
"""

from __future__ import annotations

import argparse
import json

from training.world_model.eval import evaluate_recall
from training.world_model.geometry import FieldGeometry, build_field_geometry
from training.world_model.measurements import suppress_static_candidates
from training.world_model.tbd import Candidate, TBDConfig, run_tbd
from training.world_model.tracker import TrackerConfig, causal_track

AUTOCAM = {"all": 0.76, "veryfar": 0.74, "acmissed": 0.0}


def _load(path: str):
    with open(path) as f:
        data = json.load(f)
    lo, hi = int(data["lo"]), int(data["hi"])
    frames_raw = data["frames"]
    # frame list index i  <->  source frame (lo + i)
    frame_lists: list[list[Candidate]] = []
    for t in range(lo, hi + 1):
        peaks = frames_raw.get(str(t), [])
        frame_lists.append(
            [Candidate(float(p[0]), float(p[1]), float(p[2])) for p in peaks]
        )
    gt = [(int(f), float(v[0]), float(v[1])) for f, v in data["gt"].items()]
    acmissed = {int(f) for f, v in data["split"].items() if bool(v[1])}
    return data, lo, hi, frame_lists, gt, acmissed


def _argmax_predictions(frames_raw: dict, gt: list[tuple[int, float, float]]):
    preds = {}
    for f, _gx, _gy in gt:
        peaks = frames_raw.get(str(f), [])
        if peaks:
            preds[f] = (float(peaks[0][0]), float(peaks[0][1]))
    return preds


def _run_tbd_predictions(
    frame_lists: list[list[Candidate]], lo: int, geom: FieldGeometry, cfg: TBDConfig
):
    res = run_tbd(frame_lists, geom, cfg)
    # TBD frame_idx is the list index; map back to source frame = lo + idx.
    return {lo + p.frame_idx: (p.x, p.y) for p in res.points}


def _argmax_from_lists(frame_lists: list[list[Candidate]], lo: int):
    """Per-frame highest-score candidate (argmax) from candidate lists."""
    preds = {}
    for i, cands in enumerate(frame_lists):
        if cands:
            best = max(cands, key=lambda c: c.score)
            preds[lo + i] = (best.x, best.y)
    return preds


def _causal_predictions(
    frame_lists: list[list[Candidate]], lo: int, geom: FieldGeometry, cfg: TrackerConfig
):
    res = causal_track(frame_lists, geom, cfg)
    return {lo + p.frame_idx: (p.x, p.y) for p in res.points}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--peaks", required=True, help="iron_peaks.json from the dump harness"
    )
    ap.add_argument(
        "--polygon", default="", help="optional field polygon json for support/size"
    )
    A = ap.parse_args()

    data, lo, hi, frame_lists, gt, acmissed = _load(A.peaks)
    r = float(data.get("match_r", 20.0))
    far_y = float(data.get("veryfar_cy", 450.0))
    n_peaks = sum(len(f) for f in frame_lists)
    print(
        f"loaded {A.peaks}: frames {lo}..{hi} ({len(frame_lists)}), "
        f"{n_peaks} peaks, {len(gt)} GT, {len(acmissed)} acmissed, "
        f"R={r}, veryfar_cy={far_y}, {data.get('ms_per_frame')}ms/frame inference"
    )

    geom = build_field_geometry(None)
    if A.polygon:
        with open(A.polygon) as f:
            poly = json.load(f)["polygon"]
        import numpy as np

        geom = build_field_geometry(np.asarray(poly, dtype=float))
        print(
            f"geometry: valid_homography={geom.valid}, polygon_support={geom.polygon is not None}"
        )

    # What actually matters is "is the viewport looking at the ball's AREA", not
    # pixel-exact detection. The rendered crop is large, so report veryfar recall
    # across a radius sweep from exact (R=20, AutoCam's 0.74 reference) up to
    # viewport-scale (R=400) -- area recall is the metric the renderer cares about.
    radii = [int(r), 50, 100, 200, 400]

    def report(name: str, preds: dict) -> None:
        cells = []
        for rad in radii:
            res = evaluate_recall(
                preds,
                gt,
                radius_px=float(rad),
                far_y_threshold=far_y,
                acmissed_frames=acmissed,
            )
            cells.append(f"@{rad}={res.recall_veryfar:.3f}")
        base = evaluate_recall(
            preds,
            gt,
            radius_px=float(r),
            far_y_threshold=far_y,
            acmissed_frames=acmissed,
        )
        print(
            f"  {name:28s} veryfar "
            + " ".join(cells)
            + f"  | acmissed@{int(r)}={base.recall_acmissed:.3f} ff={base.false_fire}"
        )

    suppressed, static_cells = suppress_static_candidates(frame_lists)
    print(
        f"\nAutoCam ref: veryfar 0.74 @R={int(r)} (exact). Area recall @200/@400 is the real target. "
        f"(suppressed {len(static_cells)} static background cells)"
    )
    print(f"results (veryfar recall at radius = {radii} px):")
    report("per-frame argmax (raw J)", _argmax_predictions(data["frames"], gt))
    report("argmax (static-suppressed)", _argmax_from_lists(suppressed, lo))
    # Global-MAP TBD is the WRONG inference for an intermittent target (EXP-1) — it
    # locks on a smooth distractor/coast path; kept here to show the contrast.
    report(
        "global-MAP TBD (fails)",
        _run_tbd_predictions(
            suppressed,
            lo,
            geom,
            TBDConfig(occlusion_decay=0.7, frame_w=7680, frame_h=2160),
        ),
    )
    # The world-model causal continuity tracker (EXP-3) — the winning inference.
    report(
        "CAUSAL tracker (world-model)",
        _causal_predictions(
            suppressed,
            lo,
            geom,
            TrackerConfig(
                gate0=80.0, max_lost=8, vel_alpha=0.5, frame_w=7680, frame_h=2160
            ),
        ),
    )


if __name__ == "__main__":
    main()
