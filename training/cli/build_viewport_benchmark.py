"""Whole-game viewport BENCHMARK: best-of AutoCam + human corrections (Mark 2026-07-07).

Fuses two reference tiers into one per-frame ball-position benchmark covering the
ENTIRE video, so our homegrown viewport can be measured game-wide instead of only on
labeled clips:

- **tier A (human):** every consolidated human ball label — authoritative, mostly the
  corrected far/hard frames AutoCam gets wrong.
- **tier B (autocam):** AutoCam's TOP high-confidence detection on frames inside
  sustained SELF-CONSISTENT stretches — where its viewport verifiably follows its own
  detections. Calibrated 2026-07-06 (EXP-DIST-33): such stretches score 0.94-1.00
  ball-in-viewport against human GT, so they can carry the benchmark unreviewed.
  The reference point is the DETECTION (AutoCam's ball estimate), not the smoothed
  viewport center (which lags by design).

Frames in neither tier (AutoCam loss windows without human labels) are honestly
UNKNOWN — excluded, and countable as the remaining labeling frontier. Human
``not_visible`` frames are emitted as ``none`` rows (nobody should be scored there).

Output: ``viewport_benchmark.jsonl`` rows ``{g, x, y, tier}`` (or ``{g, tier:
"none"}``) next to the video + a provenance README. Idempotent (--force to rebuild).

    python -m training.cli.build_viewport_benchmark \
      --game-dir "F:/Heat_2012s/2026.05.31 - vs Spencerport gold 2 (away)"
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

SELF_CONSISTENT_ELLIPSE = (1200.0, 500.0)  # nominal rendered-viewport half extents


def autocam_self_consistency(
    vp: dict[int, tuple[float, float]],
    dets: dict[int, list],
    *,
    conf_floor: float = 0.3,
    window: int = 160,
    min_frac: float = 0.8,
) -> dict[int, tuple[float, float]]:
    """Tier-B frames: ``{g: top-det xy}`` where the viewport tracks its own
    detections >= ``min_frac`` of the surrounding ±``window`` source frames."""
    hw, hh = SELF_CONSISTENT_ELLIPSE
    agree: dict[int, bool] = {}
    top: dict[int, tuple[float, float]] = {}
    for g, rows in dets.items():
        if g not in vp:
            continue
        good = [c for c in rows if float(c[2]) >= conf_floor]
        if not good:
            continue
        t = max(good, key=lambda c: float(c[2]))
        top[g] = (float(t[0]), float(t[1]))
        vx, vy = vp[g]
        agree[g] = ((top[g][0] - vx) / hw) ** 2 + ((top[g][1] - vy) / hh) ** 2 <= 1.0
    ks = np.asarray(sorted(agree), int)
    if not len(ks):
        return {}
    vals = np.asarray([agree[int(g)] for g in ks], float)
    csum = np.concatenate([[0.0], np.cumsum(vals)])
    out: dict[int, tuple[float, float]] = {}
    for idx, g in enumerate(ks):
        lo = int(np.searchsorted(ks, g - window))
        hi = int(np.searchsorted(ks, g + window, side="right"))
        n = hi - lo
        if n >= 20 and (csum[hi] - csum[lo]) / n >= min_frac and agree[int(g)]:
            out[int(g)] = top[int(g)]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--game-dir", required=True)
    ap.add_argument("--conf-floor", type=float, default=0.3)
    ap.add_argument("--window", type=int, default=160)
    ap.add_argument("--min-frac", type=float, default=0.8)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    from training.data_prep import distill_dataset as dd

    gd = Path(args.game_dir)
    out = gd / "viewport_benchmark.jsonl"
    if out.exists() and not args.force:
        raise SystemExit(f"{out} exists (idempotent) — use --force to rebuild")
    gj = json.loads((gd / "game.json").read_text(encoding="utf-8", errors="ignore"))
    offs = dd.seg_offsets(gj["segments"])
    hb, hn = (
        dd.load_human_labels(gd / "ball_labels.jsonl", offs)
        if (gd / "ball_labels.jsonl").exists()
        else ({}, set())
    )
    vp: dict[int, tuple[float, float]] = {}
    vpp = gd / "autocam_viewport.jsonl"
    if vpp.exists():
        for ln in vpp.read_text(encoding="utf-8", errors="ignore").splitlines():
            if ln.strip():
                r = json.loads(ln)
                g = offs.get(r.get("seg"))
                if g is not None:
                    vp[g + int(r["f"])] = (float(r["x"]), float(r["y"]))
    dets = (
        dd.load_detections(gd / "autocam_detections.jsonl", offs)
        if (gd / "autocam_detections.jsonl").exists()
        else {}
    )
    tier_b = autocam_self_consistency(
        vp,
        dets,
        conf_floor=args.conf_floor,
        window=args.window,
        min_frac=args.min_frac,
    )

    rows: dict[int, dict] = {}
    for g, xy in tier_b.items():
        rows[g] = {
            "g": g,
            "x": round(xy[0], 1),
            "y": round(xy[1], 1),
            "tier": "autocam",
        }
    for g in hn:  # human not_visible: nobody scores here
        rows[g] = {"g": int(g), "tier": "none"}
    for g, xy in hb.items():  # human labels override everything
        rows[g] = {
            "g": int(g),
            "x": round(float(xy[0]), 1),
            "y": round(float(xy[1]), 1),
            "tier": "human",
        }
    ordered = [rows[g] for g in sorted(rows)]
    with open(out, "w", encoding="utf-8") as fh:
        for r in ordered:
            fh.write(json.dumps(r) + "\n")
    n_h = sum(1 for r in ordered if r["tier"] == "human")
    n_a = sum(1 for r in ordered if r["tier"] == "autocam")
    n_n = sum(1 for r in ordered if r["tier"] == "none")
    total = int(gj.get("total_frames") or (max(rows) if rows else 0))
    (gd / "README.benchmark.md").write_text(
        f"# viewport_benchmark.jsonl — {gd.name}\n\n"
        "Per-frame game-ball reference: BEST-OF AutoCam + human corrections "
        "(design: Mark 2026-07-07).\n\n"
        f"- tier `human`: {n_h} frames — consolidated ball_labels.jsonl, authoritative.\n"
        f"- tier `autocam`: {n_a} frames — AutoCam's top detection (conf >= "
        f"{args.conf_floor}) inside self-consistent viewport stretches "
        f"(>= {args.min_frac:.0%} of ±{args.window} frames; calibrated 0.94-1.00 "
        "vs human GT, EXP-DIST-33).\n"
        f"- tier `none`: {n_n} frames — human 'not visible'.\n"
        f"- remaining ~{max(total - len(ordered), 0)} frames: UNKNOWN "
        "(AutoCam loss windows without human labels) — the labeling frontier.\n\n"
        "Built by `training/cli/build_viewport_benchmark.py` (idempotent; --force to "
        "rebuild). Consumed by `training/cli/replay_fullgame.py --benchmark`.\n"
    )
    print(
        f"{gd.name}: benchmark {len(ordered)} rows "
        f"(human {n_h}, autocam {n_a}, none {n_n}) of ~{total} frames "
        f"-> coverage {(n_h + n_a) / max(total, 1):.1%}"
    )


if __name__ == "__main__":
    main()
