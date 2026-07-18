"""Mine hard-negative crops = the current detector's confident IN-FIELD false fires.

The held-out eval showed the ball IS in our detector's peaks (candidate ceiling ~0.95) but the tracker
selects a distractor (selected ~0.24): the detector fires as confidently on players / line
intersections / the centre circle as on the ball, so the Viterbi tracker coasts onto a smooth
distractor trajectory. Random background negatives (mostly grass) don't teach it to suppress those
specific ball-like distractors.

This miner runs the detector over sampled teacher-label frames at band scale and, for every
high-confidence peak that is **in-field** but **far from the teacher ball**, writes a 256-crop centred
on it as an extra NEGATIVE into an existing crop store (append to ``index.json``). A fine-tune on the
augmented store teaches the detector to score those distractors low → clean candidates → the tracker
we know works (0.85 on AutoCam's clean detections). Off-field peaks are left to ``eval_detector``'s
in-field gate, so we keep only the in-field residual the gate can't remove.

    python -m training.cli.mine_hard_negatives --roots F:/Flash_2013s F:/Heat_2012s F:/Guest \
        --camera reolink --holdout heat__2026.05.31_vs_Spencerport_gold_2_away \
        --val flash__2026.05.09_vs_Cleveland_Force_SC_White_home \
        --ckpt G:/ballresearch/distill/runs/hm_reolink/best.pt \
        --out G:/ballresearch/distill/crops_reolink
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import numpy as np

from training.cli.build_distill_dataset import find_configs
from training.data_prep import distill_dataset as dd


def load_index(out: Path) -> tuple[list, dict | None]:
    """Load a crop store's index. Two historical forms exist: a bare item LIST and the
    ``{"summary": ..., "items": [...]}`` DICT that ``build_heatmap_crops`` writes. The
    hn1/hn2 mining rounds CRASHED on the dict form (``index.append`` on a dict) and
    silently added zero crops — support both, return ``(items, wrapper_or_None)``."""
    raw = json.loads((out / "index.json").read_text())
    if isinstance(raw, dict):
        return raw["items"], raw
    return raw, None


def save_index(out: Path, items: list, wrapper: dict | None) -> None:
    """Write the index back in the SAME form it was read (updating summary counts).

    EXP-DIST-55: the store is pinned BEFORE and AFTER the write, so both the
    pre-mining and post-mining states are immutable, recoverable versions —
    in-place mutation cost a full experiment batch its baseline."""
    from training.data_prep.store_versions import freeze_index

    v0, s0 = freeze_index(out)
    if wrapper is not None:
        wrapper["items"] = items
        summary = wrapper.get("summary")
        if isinstance(summary, dict):
            summary["samples"] = len(items)
        (out / "index.json").write_text(json.dumps(wrapper))
    else:
        (out / "index.json").write_text(json.dumps(items))
    v1, s1 = freeze_index(out)
    print(f"STORE VERSIONED: pre=v{v0}({s0}) -> post=v{v1}({s1})", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--roots", nargs="+", required=True)
    ap.add_argument("--ckpt", required=True)
    ap.add_argument(
        "--out",
        required=True,
        help="existing crop store to augment (crops/ + index.json)",
    )
    ap.add_argument("--camera", default="reolink")
    ap.add_argument("--holdout", nargs="*", default=[])
    ap.add_argument("--val", nargs="*", default=[])
    ap.add_argument(
        "--base",
        type=int,
        default=None,
        help="expected base width; inferred from the checkpoint, mismatch = error",
    )
    ap.add_argument("--base-stride", type=int, default=1)
    ap.add_argument(
        "--sample-stride",
        type=int,
        default=8,
        help="mine every Nth teacher-label frame (distractors recur; no need for all frames)",
    )
    ap.add_argument(
        "--max-frames", type=int, default=6000, help="cap decode span per game"
    )
    ap.add_argument("--crop", type=int, default=256)
    ap.add_argument(
        "--score-thr", type=float, default=0.3, help="only mine confident false peaks"
    )
    ap.add_argument(
        "--min-ball-dist-px",
        type=float,
        default=48.0,
        help="band px; keep only peaks farther than this from the ball (never negate the ball)",
    )
    ap.add_argument("--max-per-frame", type=int, default=3)
    ap.add_argument("--top-k", type=int, default=24)
    ap.add_argument("--tile-w", type=int, default=2560)
    ap.add_argument("--overlap", type=int, default=256)
    ap.add_argument("--infield-margin", type=float, default=120.0)
    ap.add_argument("--no-hwaccel", action="store_true")
    ap.add_argument(
        "--use-gt",
        action="store_true",
        help="GUARD: mine only on HUMAN-GT frames, using the GT ball position as the "
        "exclusion centre — never crops the real ball even when AutoCam was wrong "
        "(the teacher guard mines the real ball on AutoCam-error frames). Reads each "
        "game's ball_labels.jsonl.",
    )
    ap.add_argument(
        "--corroboration-dir",
        default=None,
        help="GUARD (scalable): mine on CORROBORATION frames where AutoCam and our v7 "
        "selector independently agree on the ball (2 sources ~= GT, no human label). "
        "Reads <dir>/<game_id>.json {global_frame: [x,y]} from build_corrob_labels.py. "
        "Overrides --use-gt/teacher labels for that game.",
    )
    ap.add_argument(
        "--stabilize",
        action="store_true",
        help="wind-align bands to each game's first decoded frame; label/exclusion "
        "coords corrected by the per-frame shift (EXP-DIST-57). Use iff the store "
        "being mined into was built with --stabilize.",
    )
    args = ap.parse_args()

    import av
    import cv2
    import torch

    from training.cli.eval_detector import infer_band
    from training.data_prep.heatmap_dataset import (
        _dewarp_mask_gray,
        _far_margin_polygon,
        _native_iso_warp,
    )
    from training.data_prep.segment_decode import iter_frames_from_segments
    from training.data_prep.warped_dataset import resolve_video_rotation
    from training.models.heatmap_net import load_detector_checkpoint
    from training.world_model.eval import extract_peaks
    from training.world_model.geometry import build_field_geometry
    from video_grouper.inference.iso_warp import BandStabilizer

    out = Path(args.out)
    crops = out / "crops"
    index, wrapper = load_index(out)
    if not (
        out / "index.orig.json"
    ).exists():  # one-time backup so mining is revertible
        shutil.copy2(out / "index.json", out / "index.orig.json")

    holdout, val = set(args.holdout), set(args.val)
    cfgs = [c for c in find_configs(args.roots) if c["game_id"] not in holdout]
    if args.camera:
        cfgs = [c for c in cfgs if c.get("camera") == args.camera]
    for c in cfgs:
        c["split"] = "val" if c["game_id"] in val else "train"
    games = dd.build_distill_games(cfgs, base_stride=args.base_stride, report=False)

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    # prelude + net (raw frames in, logits out); geometry inferred from the ckpt,
    # encoding from its metadata — an explicit --base mismatch is a hard error.
    model, _meta = load_detector_checkpoint(args.ckpt, base=args.base, device=dev)

    half = args.crop // 2
    total = 0
    for g in games:
        gid = g["game_id"]
        if g.get("split") == "val":
            continue
        labels = {int(k): v for k, v in g["labels"].items()}
        if args.use_gt:
            # GT GUARD: replace the teacher labels with HUMAN ball GT so the exclusion
            # centre is the true ball, not AutoCam's (possibly wrong) pick. Same global
            # frame space (global_offset + f) as the teacher labels.
            gdir = Path(g["video"]).parent
            blp, gjp2 = gdir / "ball_labels.jsonl", gdir / "game.json"
            if not (blp.exists() and gjp2.exists()):
                continue
            offs = dd.seg_offsets(
                json.loads(gjp2.read_text(encoding="utf-8", errors="ignore"))[
                    "segments"
                ]
            )
            hb, _ = dd.load_human_labels(blp, offs)
            labels = {int(k): (float(v[0]), float(v[1])) for k, v in hb.items()}
        if args.corroboration_dir:
            cf = Path(args.corroboration_dir) / f"{gid}.json"
            if not cf.exists():
                continue
            labels = {
                int(k): (float(v[0]), float(v[1]))
                for k, v in json.loads(cf.read_text(encoding="utf-8")).items()
            }
        if not labels:
            continue
        geom = build_field_geometry(np.asarray(g["polygon"], float))
        if not geom.valid:
            continue
        # Rotation is applied inside iter_frames_from_segments; probe only for dims.
        vrot = resolve_video_rotation(str(g["video"]), g.get("video_rotation"))
        with av.open(str(g["video"])) as _probe:
            _vs = _probe.streams.video[0]
            sw, sh = _vs.codec_context.width, _vs.codec_context.height
        far_poly = _far_margin_polygon(g["polygon"], 400.0)
        warp = _native_iso_warp(far_poly, sw, sh, g.get("target_width"))
        bh, bw = warp.shape
        mpoly = warp.points(far_poly).astype(np.int32)
        mask = np.zeros((bh, bw), np.uint8)
        cv2.fillPoly(mask, [mpoly], 255)

        want = sorted(labels)
        lo, hi = want[0], min(want[-1], want[0] + args.max_frames)
        infer_set = {
            f
            for i, f in enumerate(want)
            if lo <= f <= hi and i % args.sample_stride == 0
        }
        # Raw-segment streaming decode (EXP-DIST-21): only each sampled frame's
        # 3-frame window is decoded (~one GOP per cluster), corruption-isolated.
        # Non-combined bases (corrected/trimmed single clip) = one synthetic segment.
        video_p = Path(str(g["video"]))
        gjp = video_p.parent / "game.json"
        if video_p.name == "combined.mp4" and gjp.exists():
            segments = json.loads(gjp.read_text(encoding="utf-8", errors="ignore"))[
                "segments"
            ]
        else:
            segments = [{"seg": video_p.stem, "global_offset": 0, "frames": 10**9}]
        need: set[int] = set()
        for f in infer_set:
            need.update(k for k in (f, f - 1, f - 2) if k >= 0)
        warped: dict[int, np.ndarray] = {}
        stab = BandStabilizer() if args.stabilize else None
        nadd = 0
        for idx, img in iter_frames_from_segments(
            video_p.parent, segments, sorted(need), vrot, hwaccel=not args.no_hwaccel
        ):
            warped[idx] = _dewarp_mask_gray(img, warp, mask, stab)
            if idx in infer_set:
                seq = [warped.get(idx - 2), warped.get(idx - 1), warped.get(idx)]
                seq = [s for s in seq if s is not None]
                grays = seq if len(seq) == 3 else [seq[0]] * (3 - len(seq)) + seq
                for _k in [k for k in warped if k < idx - 2]:
                    del warped[_k]
                stack = np.stack(grays, 0).astype(np.float32) / 255.0
                hm = infer_band(model, dev, stack, args.tile_w, args.overlap)
                bx, by = warp.points([labels[idx]])[0]
                if stab is not None:
                    # label lives on the RAW frame -> aligned-band coords; the
                    # peaks are already aligned coords (inference ran on the
                    # aligned band), as is the polygon-registered geometry.
                    bx, by = bx - stab.last[0], by - stab.last[1]
                kept = 0
                for hx, hy, _sc in extract_peaks(
                    hm, top_k=args.top_k, threshold=args.score_thr, min_distance=6
                ):
                    if kept >= args.max_per_frame:
                        break
                    if (hx - bx) ** 2 + (hy - by) ** 2 <= args.min_ball_dist_px**2:
                        continue  # this peak IS (near) the ball — never negate it
                    sx, sy = hx / warp.scale, hy / warp.scale + warp.y_top
                    if not bool(
                        geom.is_in_support(
                            np.asarray([(sx, sy)], float), margin_px=args.infield_margin
                        )[0]
                    ):
                        continue  # off-field distractor — the eval gate already drops these
                    x0 = int(np.clip(round(hx) - half, 0, max(0, bw - args.crop)))
                    y0 = int(np.clip(round(hy) - half, 0, max(0, bh - args.crop)))
                    cstack = np.zeros((3, args.crop, args.crop), np.uint8)
                    for i, gr in enumerate(grays):
                        patch = gr[y0 : y0 + args.crop, x0 : x0 + args.crop]
                        cstack[i, : patch.shape[0], : patch.shape[1]] = patch
                    fname = f"{gid}_f{idx:06d}_hardmine{kept}.npy"
                    np.save(crops / fname, cstack)
                    index.append(
                        {
                            "file": fname,
                            "x": None,  # NEGATIVE: no ball target (the trainer marker)
                            "y": None,
                            "split": "train",
                            "mine_x": float(
                                sx
                            ),  # audit only: source-px distractor centre
                            "mine_y": float(sy),
                        }
                    )
                    kept += 1
                    nadd += 1
        total += nadd
        print(f"{gid}: +{nadd} hard-neg crops", flush=True)

    save_index(out, index, wrapper)
    print(f"\nMINED: +{total} hard-negative crops appended to {out}", flush=True)


if __name__ == "__main__":
    main()
