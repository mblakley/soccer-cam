"""Extend an existing viewport-label set with divergence frames from NEW camera
paths — provenance-blind (DECISIONS 07-23 (f): arm-vs-arm disagreement frames
join the union before labeling; the annotator never sees which arm diverged).

Existing labeled frames are untouched; new frames get kind="ext-div" and no
seed viewport from the diverging arms (the champion seed stays, so the
confirm-not-draw flow is unchanged).

    python -m training.cli.extend_viewport_label_queue \
        --set-dir D:/training_data/viewport_label/pittsford_dahua_gt \
        --game-dir "F:/.../06.25.2024 - vs Pittsford (home)" \
        --cams campath_pit_mg_ctrl.pkl campath_pit_mg_geo.pkl ... \
        --max-new 150
"""

from __future__ import annotations

import argparse
import json
from itertools import combinations
from pathlib import Path

import numpy as np

from training.cli.build_viewport_label_queue import load_cams


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set-dir", required=True)
    ap.add_argument("--game-dir", required=True)
    ap.add_argument("--cams", nargs="+", required=True, help="2+ campath artifacts")
    ap.add_argument("--div-thr", type=float, default=400.0)
    ap.add_argument("--div-min-frames", type=int, default=40)
    ap.add_argument("--stride", type=int, default=8)
    ap.add_argument("--max-new", type=int, default=150)
    ap.add_argument("--pad-s", type=float, default=2.0)
    ap.add_argument("--no-hwaccel", action="store_true")
    args = ap.parse_args()

    import cv2

    from training.data_prep import distill_dataset as dd
    from training.data_prep.segment_decode import iter_frames_from_segments
    from training.data_prep.warped_dataset import resolve_video_rotation

    sd = Path(args.set_dir)
    man = json.loads((sd / "manifest.json").read_text())
    have = {int(f["frame_idx"]) for f in man["frames"]}
    gd = Path(args.game_dir)
    gj = json.loads((gd / "game.json").read_text(encoding="utf-8", errors="ignore"))

    paths = []
    for p in args.cams:
        cams, g0 = load_cams(p)
        cx = np.full(len(cams), np.nan)
        cx[g0:] = [c[0] for c in cams[g0:]]
        paths.append(cx)
    n = min(len(p) for p in paths)

    # pairwise divergence runs (any pair apart > thr for >= div_min_frames)
    div = np.zeros(n, bool)
    for a, b in combinations(paths, 2):
        d = np.abs(a[:n] - b[:n])
        div |= np.nan_to_num(d, nan=0.0) > args.div_thr
    frames: list[int] = []
    run = None
    for g in range(n):
        if div[g]:
            run = [g, g] if run is None else [run[0], g]
        else:
            if run and run[1] - run[0] >= args.div_min_frames:
                pad = int(args.pad_s * 20)
                frames.extend(
                    range(max(0, run[0] - pad), min(n - 1, run[1] + pad), args.stride)
                )
            run = None
    ranges = dd.active_play_ranges(gj["segments"], gj.get("game_state"))
    if ranges:
        frames = [g for g in frames if any(lo <= g <= hi for lo, hi in ranges)]
    new = sorted(set(frames) - have)
    if len(new) > args.max_new:
        idx = np.linspace(0, len(new) - 1, args.max_new).round().astype(int)
        new = [new[i] for i in sorted(set(idx.tolist()))]
    if not new:
        print("no new divergence frames — nothing to extend")
        return

    vrot = resolve_video_rotation(str(gd / "combined.mp4"), gj.get("video_rotation"))
    sw, sh = man["src_w"], man["src_h"]
    n_written = 0
    for g, img in iter_frames_from_segments(
        gd, gj["segments"], set(new), vrot, hwaccel=not args.no_hwaccel
    ):
        small = cv2.resize(img, (int(sw * man["scale"]), int(sh * man["scale"])))
        cv2.imwrite(
            str(sd / "strips" / f"f{g:06d}.jpg"), small, [cv2.IMWRITE_JPEG_QUALITY, 72]
        )
        n_written += 1

    for g in new:
        if not (sd / "strips" / f"f{g:06d}.jpg").exists():
            continue
        man["frames"].append(
            {"frame_idx": g, "kind": "ext-div", "our": None, "ac": None, "gt": None}
        )
    man["frames"].sort(key=lambda f: f["frame_idx"])
    man["n_frames"] = len(man["frames"])
    (sd / "manifest.json").write_text(json.dumps(man, indent=1))
    print(f"EXTENDED {sd.name}: +{n_written} ext-div frames -> {man['n_frames']} total")


if __name__ == "__main__":
    main()
