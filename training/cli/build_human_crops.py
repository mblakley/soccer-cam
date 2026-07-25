r"""Emit detector-training crops from HUMAN far-label labels and append them to a crop store.

The far-label tool (``cli/build_far_label_queue`` + ``far-label.html``) produces human GT on exactly
the frames AutoCam struggles with. This turns those labels into training crops so a fine-tune injects
the human signal on the hard cases:

  * ``ball``        -> POSITIVE crop (Gaussian at the human-placed ball).
  * ``obscured``    -> NEGATIVE crop at the human best-guess position (the ball is behind a player —
                       the detector must NOT fire there; teaches "that player is not the ball").
  * ``not_visible`` / ``out_of_play`` -> NEGATIVE crop at AutoCam's hint (its false-fire that frame).

Held-out games MUST be excluded (``--exclude`` regex) so the eval isn't leaked. Decodes each set's
clip from its manifest (self-contained coord system) and writes crops in the ``HeatmapCropDataset``
format ({file, x, y|null, split}) so the existing trainer consumes them unchanged.

    python -m training.cli.build_human_crops --out G:/ballresearch/distill/crops_reolink
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import numpy as np


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--far-label-dir", default="D:/training_data/far_label")
    ap.add_argument(
        "--out", required=True, help="crop store to augment (crops/ + index.json)"
    )
    ap.add_argument(
        "--exclude",
        default=r"spc|0615|irondequoit|Spencerport|2026\.05\.31|2026\.06\.15",
        help="regex of far-label set names to SKIP (held-out games — avoid eval leak)",
    )
    ap.add_argument("--crop", type=int, default=256)
    ap.add_argument("--far-margin", type=float, default=400.0)
    ap.add_argument("--no-hwaccel", action="store_true")
    ap.add_argument(
        "--stabilize",
        action="store_true",
        help="wind-align bands to each set's first decoded frame; label coords "
        "corrected by the per-frame shift (EXP-DIST-57)",
    )
    args = ap.parse_args()

    import cv2

    from training.data_prep.heatmap_dataset import (
        _dewarp_mask_gray,
        _far_margin_polygon,
        _native_iso_warp,
    )
    from training.data_prep.segment_decode import iter_frames_from_segments
    from training.data_prep.warped_dataset import resolve_video_rotation
    from video_grouper.inference.iso_warp import BandStabilizer

    out = Path(args.out)
    crops = out / "crops"
    crops.mkdir(parents=True, exist_ok=True)
    # index.json is {"summary": {...}, "items": [...]}; keep the summary, append to items.
    idx_obj = json.loads((out / "index.json").read_text())
    items = idx_obj["items"]
    if not (out / "index.orig_prehuman.json").exists():
        (out / "index.orig_prehuman.json").write_text(json.dumps(idx_obj))

    excl = re.compile(args.exclude)
    half = args.crop // 2
    fld = Path(args.far_label_dir)
    totals = {"pos": 0, "neg": 0}
    for sd in sorted(fld.iterdir()):
        if not sd.is_dir() or excl.search(sd.name):
            continue
        man, lab = sd / "manifest.json", sd / "labels.json"
        if not (man.exists() and lab.exists()):
            continue
        m = json.loads(man.read_text())
        labels = [r for r in json.loads(lab.read_text()) if r.get("source") == "human"]
        if not labels:
            continue
        clip = m["clip"]
        clip_p = Path(clip)
        if not clip_p.exists():
            print(f"  SKIP {sd.name}: clip missing", flush=True)
            continue
        # Never train on stubs: house/camera-test recordings are tiny (~50MB) vs real game
        # clips (0.7GB+ per segment). Directory clips are raw .dav recordings, not decodable here.
        if clip_p.is_dir():
            print(
                f"  SKIP {sd.name}: clip is a directory (raw recording): {clip}",
                flush=True,
            )
            continue
        MIN_CLIP_BYTES = 200 * 1024 * 1024
        if clip_p.stat().st_size < MIN_CLIP_BYTES:
            mb = clip_p.stat().st_size // (1024 * 1024)
            print(
                f"  SKIP {sd.name}: clip {mb}MB < 200MB — stub/non-game footage: {clip}",
                flush=True,
            )
            continue
        poly = m["polygon"]
        hint = {
            int(f["frame_idx"]): (f.get("hint_x"), f.get("hint_y")) for f in m["frames"]
        }
        # what to emit per labeled frame: (source_x, source_y, is_positive)
        want: dict[int, tuple] = {}
        for r in labels:
            fi = int(r["frame_idx"])
            a = r.get("action")
            if a == "ball" and r.get("x") is not None:
                want[fi] = (float(r["x"]), float(r["y"]), True)
            else:  # obscured / not_visible / out_of_play -> negative
                if r.get("x") is not None:
                    cx, cy = float(r["x"]), float(r["y"])
                elif hint.get(fi) and hint[fi][0] is not None:
                    cx, cy = float(hint[fi][0]), float(hint[fi][1])
                else:
                    continue
                want[fi] = (cx, cy, False)
        if not want:
            continue

        # Pull the 3-frame band ({f-2, f-1, f}) for each label from the RAW per-segment clips instead
        # of the re-encoded/VFR/corruption-prone combined video. A "combined.mp4" set uses global
        # frame indices (mapped via game.json segments); a single-clip set's frame_idx is local to
        # that one clip. Corruption-isolated per segment; a missing frame just drops that one label.
        clip_p2 = Path(clip)
        game_dir = clip_p2.parent
        gjp = game_dir / "game.json"
        if clip_p2.name == "combined.mp4" and gjp.exists():
            segments = json.loads(gjp.read_text())["segments"]
        else:
            segments = [{"seg": clip_p2.stem, "global_offset": 0, "frames": 10**9}]
        vrot = resolve_video_rotation(clip, m.get("video_rotation"))
        band_globals: set[int] = set()
        for f in want:
            band_globals.update((max(0, f - 2), max(0, f - 1), f))
        # STREAM the band frames (ascending): warp each as it arrives, emit a label's
        # crop once its window [f-2, f] is in hand, evict older band frames. Never
        # holds the full-res frame set (a dense far-label set OOMs the 16 GB box).
        far_poly = _far_margin_polygon(poly, args.far_margin)
        warp = mask = None
        bh = bw = 0
        band_gray: dict[int, np.ndarray] = {}
        stab = BandStabilizer() if args.stabilize else None
        n = 0
        try:
            for f, img in iter_frames_from_segments(
                game_dir, segments, band_globals, vrot, hwaccel=not args.no_hwaccel
            ):
                if warp is None:
                    sh_, sw_ = img.shape[:2]
                    warp = _native_iso_warp(far_poly, sw_, sh_, None)
                    bh, bw = warp.shape
                    mpoly = warp.points(far_poly).astype(np.int32)
                    mask = np.zeros((bh, bw), np.uint8)
                    cv2.fillPoly(mask, [mpoly], 255)
                band_gray[f] = _dewarp_mask_gray(img, warp, mask, stab)
                if f in want:
                    sx, sy, is_pos = want[f]
                    bx, by = warp.points([(sx, sy)])[0]
                    if stab is not None:
                        bx, by = bx - stab.last[0], by - stab.last[1]
                    x0 = int(np.clip(round(bx) - half, 0, max(0, bw - args.crop)))
                    y0 = int(np.clip(round(by) - half, 0, max(0, bh - args.crop)))
                    lx, ly = bx - x0, by - y0
                    if is_pos and not (0 <= lx < args.crop and 0 <= ly < args.crop):
                        continue
                    g0 = band_gray[f]
                    g1 = band_gray.get(f - 1, g0)
                    g2 = band_gray.get(f - 2, g1)
                    stack = np.zeros((3, args.crop, args.crop), np.uint8)
                    for i, gr in enumerate((g2, g1, g0)):
                        patch = gr[y0 : y0 + args.crop, x0 : x0 + args.crop]
                        stack[i, : patch.shape[0], : patch.shape[1]] = patch
                    tag = "hpos" if is_pos else "hneg"
                    fn = f"human_{sd.name}_f{f:06d}_{tag}.npy"
                    np.save(crops / fn, stack)
                    items.append(
                        {
                            "file": fn,
                            "x": round(float(lx), 1) if is_pos else None,
                            "y": round(float(ly), 1) if is_pos else None,
                            "split": "train",
                        }
                    )
                    n += 1
                    totals["pos" if is_pos else "neg"] += 1
                for _k in [k for k in band_gray if k < f - 2]:
                    del band_gray[_k]
        except Exception as e:  # noqa: BLE001
            print(f"  SKIP {sd.name}: extract error: {e!r}", flush=True)
            continue
        if warp is None:
            print(f"  SKIP {sd.name}: no frames extracted", flush=True)
            continue
        print(f"  {sd.name}: +{n} crops (raw-segment)", flush=True)

    from training.data_prep.store_versions import freeze_index

    v0, s0 = freeze_index(out)
    idx_obj["items"] = items
    idx_obj.setdefault("summary", {})
    idx_obj["summary"]["samples"] = len(items)
    idx_obj["summary"]["human_pos"] = totals["pos"]
    idx_obj["summary"]["human_neg"] = totals["neg"]
    (out / "index.json").write_text(json.dumps(idx_obj))
    v1, s1 = freeze_index(out)
    print(f"STORE VERSIONED: pre=v{v0}({s0}) -> post=v{v1}({s1})", flush=True)
    print(
        f"\nHUMAN CROPS appended: +{totals['pos']} positives, +{totals['neg']} negatives "
        f"(store now {len(items)} crops) -> {out}",
        flush=True,
    )


if __name__ == "__main__":
    main()
