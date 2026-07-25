"""Overlay the CURRENT detector's top candidates onto far-label sets.

Why: the annotator can't mark "not the game ball" against distractors they can't see —
and their single game-ball click (or not_visible) already implies every OTHER detector
candidate is a negative (training consumes frames listwise). So instead of guessing at
decoys, show the model's actual confusions: run the detector over each set's frames
(same 3-frame raw-segment decode as the real pipeline) and write the top-K candidates
into each manifest frame's ``context`` — the deployed far-label UI already renders that
layer as dots (blue = top-5, orange = rest). The full ``candidates`` [[x, y, score]...]
are stored alongside for training-time use.

    python -m training.cli.inject_set_candidates \
      --ckpt G:/ballresearch/distill/runs/hm_reolink_hn2/best.pt \
      --sets heat__2026.05.31_vs_Spencerport_gold_2_away__hard spc_hard_review
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import numpy as np

FAR_LABEL_DIR = Path("D:/training_data/far_label")


def context_entries(
    peaks: list[tuple[float, float, float]], warp, top_blue: int = 5
) -> tuple[list[dict], list[list[float]]]:
    """Map band-space peaks to (UI ``context`` dots, source-px ``candidates``).

    ``df < 0`` renders blue in the deployed UI (used for the top-``top_blue`` ranks),
    ``df > 0`` orange. Peaks arrive score-sorted from ``extract_peaks``.
    """
    ctx: list[dict] = []
    cands: list[list[float]] = []
    for rank, (hx, hy, sc) in enumerate(peaks):
        sx = float(hx) / warp.scale
        sy = float(hy) / warp.scale + warp.y_top
        ctx.append(
            {"x": round(sx, 1), "y": round(sy, 1), "df": -1 if rank < top_blue else 1}
        )
        cands.append([round(sx, 1), round(sy, 1), round(float(sc), 4)])
    return ctx, cands


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", required=True)
    ap.add_argument(
        "--sets", nargs="+", required=True, help="set names under the far-label dir"
    )
    ap.add_argument("--far-label-dir", default=str(FAR_LABEL_DIR))
    ap.add_argument("--base", type=int, default=24)
    ap.add_argument("--top-k", type=int, default=12)
    ap.add_argument("--thr", type=float, default=0.1)
    ap.add_argument("--min-distance", type=int, default=3)
    ap.add_argument("--tile-w", type=int, default=2560)
    ap.add_argument("--overlap", type=int, default=256)
    ap.add_argument("--no-hwaccel", action="store_true")
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
    from training.models.heatmap_net import HeatmapNet
    from training.world_model.eval import extract_peaks

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model = HeatmapNet(in_frames=3, in_ch_per_frame=1, base=args.base).to(dev)
    ck = torch.load(args.ckpt, map_location=dev)
    model.load_state_dict(ck["model"] if "model" in ck else ck)
    model.eval()

    for set_name in args.sets:
        sd = Path(args.far_label_dir) / set_name
        mp = sd / "manifest.json"
        if not mp.exists():
            print(f"SKIP {set_name}: no manifest", flush=True)
            continue
        m = json.loads(mp.read_text(encoding="utf-8"))
        clip = Path(m["clip"])
        game_dir = clip.parent
        gjp = game_dir / "game.json"
        if clip.name == "combined.mp4" and gjp.exists():
            segments = json.loads(gjp.read_text(encoding="utf-8", errors="ignore"))[
                "segments"
            ]
        else:
            segments = [{"seg": clip.stem, "global_offset": 0, "frames": 10**9}]
        vrot = resolve_video_rotation(str(clip), None)
        with av.open(str(clip)) as probe:
            vs = probe.streams.video[0]
            sw, sh = vs.codec_context.width, vs.codec_context.height

        far_poly = _far_margin_polygon(m["polygon"], 400.0)
        warp = _native_iso_warp(far_poly, sw, sh, None)
        bh, bw = warp.shape
        mpoly = warp.points(far_poly).astype(np.int32)
        mask = np.zeros((bh, bw), np.uint8)
        cv2.fillPoly(mask, [mpoly], 255)

        by_idx = {int(f["frame_idx"]): f for f in m["frames"]}
        need: set[int] = set()
        for g in by_idx:
            need.update(k for k in (g, g - 1, g - 2) if k >= 0)
        warped: dict[int, np.ndarray] = {}
        done = 0
        for idx, img in iter_frames_from_segments(
            game_dir, segments, sorted(need), vrot, hwaccel=not args.no_hwaccel
        ):
            warped[idx] = _dewarp_mask_gray(img, warp, mask)
            if idx in by_idx:
                seq = [warped.get(idx - 2), warped.get(idx - 1), warped.get(idx)]
                seq = [s for s in seq if s is not None]
                grays = seq if len(seq) == 3 else [seq[0]] * (3 - len(seq)) + seq
                stack = np.stack(grays, 0).astype(np.float32) / 255.0
                hm = infer_band(model, dev, stack, args.tile_w, args.overlap)
                peaks = extract_peaks(
                    hm,
                    top_k=args.top_k,
                    threshold=args.thr,
                    min_distance=args.min_distance,
                )
                ctx, cands = context_entries(peaks, warp)
                by_idx[idx]["context"] = ctx
                by_idx[idx]["candidates"] = cands
                done += 1
            for _k in [k for k in warped if k < idx - 2]:
                del warped[_k]

        if not (sd / "manifest.orig.json").exists():
            shutil.copy2(mp, sd / "manifest.orig.json")
        m["candidates_ckpt"] = str(args.ckpt)
        mp.write_text(json.dumps(m, indent=2))
        print(
            f"{set_name}: candidates injected on {done}/{len(by_idx)} frames",
            flush=True,
        )


if __name__ == "__main__":
    main()
