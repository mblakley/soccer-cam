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
    args = ap.parse_args()

    import av
    import cv2

    from training.data_prep.heatmap_dataset import (
        _dewarp_mask_gray,
        _far_margin_polygon,
        _native_iso_warp,
    )
    from training.data_prep.warped_dataset import (
        apply_display_rotation,
        resolve_video_rotation,
    )

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

        # Isolate each set: a decode failure on one clip (e.g. NVDEC choking on a raw camera
        # segment) must not lose every other set's crops. Skip the bad set and keep going.
        container = None
        try:
            vrot = resolve_video_rotation(clip, m.get("video_rotation"))
            _hw = None
            if not args.no_hwaccel:
                try:
                    _hw = av.codec.hwaccel.HWAccel(
                        device_type="cuda", allow_software_fallback=True
                    )
                except Exception:  # noqa: BLE001
                    _hw = None
            container = av.open(clip, hwaccel=_hw) if _hw else av.open(clip)
            stream = container.streams.video[0]
            if _hw is None:
                stream.thread_type = "AUTO"
            sw, sh = stream.codec_context.width, stream.codec_context.height
            far_poly = _far_margin_polygon(poly, args.far_margin)
            warp = _native_iso_warp(far_poly, sw, sh, None)
            bh, bw = warp.shape
            mpoly = warp.points(far_poly).astype(np.int32)
            mask = np.zeros((bh, bw), np.uint8)
            cv2.fillPoly(mask, [mpoly], 255)

            hi = max(want)
            # Warping every full-match frame (cv2.remap on 7680x2160) is the real cost and dwarfs
            # NVDEC decode. Only warp frames inside a label's 3-frame band; decode stays sequential
            # (cheap) but we skip the remap for the ~99% of frames with no nearby label. The crop at
            # each labeled frame is byte-identical to the naive version — this only drops dead work.
            warp_frames: set[int] = set()
            for f in want:
                warp_frames.update((f - 2, f - 1, f))
            band: dict[int, np.ndarray] = {}
            idx = -1
            n = 0
            for fr in container.decode(stream):
                idx += 1
                if idx > hi:
                    break
                if idx not in warp_frames:
                    continue
                img = apply_display_rotation(fr.to_ndarray(format="bgr24"), vrot)
                band[idx] = _dewarp_mask_gray(img, warp, mask)
                if idx in want:
                    sx, sy, is_pos = want[idx]
                    bx, by = warp.points([(sx, sy)])[0]
                    x0 = int(np.clip(round(bx) - half, 0, max(0, bw - args.crop)))
                    y0 = int(np.clip(round(by) - half, 0, max(0, bh - args.crop)))
                    g0 = band[idx]
                    g1 = band.get(idx - 1, g0)
                    g2 = band.get(idx - 2, g1)
                    stack = np.zeros((3, args.crop, args.crop), np.uint8)
                    for i, gr in enumerate((g2, g1, g0)):
                        patch = gr[y0 : y0 + args.crop, x0 : x0 + args.crop]
                        stack[i, : patch.shape[0], : patch.shape[1]] = patch
                    lx, ly = bx - x0, by - y0
                    if is_pos and not (0 <= lx < args.crop and 0 <= ly < args.crop):
                        continue
                    tag = "hpos" if is_pos else "hneg"
                    fn = f"human_{sd.name}_f{idx:06d}_{tag}.npy"
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
                    for k in [j for j in band if j < idx - 2]:
                        del band[k]
            print(f"  {sd.name}: +{n} crops", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"  SKIP {sd.name}: decode/emit error: {e!r}", flush=True)
        finally:
            if container is not None:
                container.close()

    idx_obj["items"] = items
    idx_obj.setdefault("summary", {})
    idx_obj["summary"]["samples"] = len(items)
    idx_obj["summary"]["human_pos"] = totals["pos"]
    idx_obj["summary"]["human_neg"] = totals["neg"]
    (out / "index.json").write_text(json.dumps(idx_obj))
    print(
        f"\nHUMAN CROPS appended: +{totals['pos']} positives, +{totals['neg']} negatives "
        f"(store now {len(items)} crops) -> {out}",
        flush=True,
    )


if __name__ == "__main__":
    main()
