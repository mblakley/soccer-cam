"""Full-game candidate dumps: the marathon artifact behind selection-level work.

Runs the (frozen) detector over a game's ACTIVE-PLAY span at ``--stride`` and writes
per-frame candidates in resumable CHUNKS. One artifact, three consumers: corpus-level
static-distractor mining, selection-level supervision at the volume the learned
selector requires (EXP-DIST-24 re-open conditions), and targeted hard negatives for
the next full retrain.

The sampled grid is aligned to ``global % stride == 0`` so it intersects the AutoCam
marathon's 0-mod-4 detection grid exactly (teacher matching needs no interpolation
when ``stride`` is a multiple of 4).

Output layout (``--out`` directory):
  meta.json                     game, ckpt, params, ranges, chunk list
  part_<start>_<end>.pkl        {global: [(x, y, score), ...]} for that span

Chunks that already exist are SKIPPED — kill/resume safe at chunk granularity.

    python -m training.cli.dump_game_candidates \
      --ckpt G:/ballresearch/distill/runs/hm_reolink_hn2/best.pt \
      --game-dir "F:/Heat_2012s/2026.05.27 - vs Chili Vortex (away)" \
      --out G:/ballresearch/selector/fullgame/chili
"""

from __future__ import annotations

import argparse
import json
import pickle
from pathlib import Path

import numpy as np


def sample_grid(
    ranges: list[tuple[int, int]], stride: int, total_frames: int
) -> list[int]:
    """Stride-aligned (``g % stride == 0``) frame grid over active-play ranges
    (whole game when no ranges are known)."""
    if not ranges:
        ranges = [(0, total_frames)]
    out: list[int] = []
    for lo, hi in ranges:
        start = ((max(lo, 0) + stride - 1) // stride) * stride
        out.extend(range(start, min(hi, total_frames), stride))
    return sorted(set(out))


def chunk_spans(grid: list[int], chunk: int) -> list[tuple[int, int, list[int]]]:
    """Split the grid into ``chunk``-sized pieces -> (start, end, frames) tuples."""
    return [
        (part[0], part[-1], part)
        for part in (grid[i : i + chunk] for i in range(0, len(grid), chunk))
        if part
    ]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", required=True)
    ap.add_argument("--game-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--base",
        type=int,
        default=None,
        help="expected base width; inferred from the checkpoint, mismatch = error",
    )
    ap.add_argument("--stride", type=int, default=4)
    ap.add_argument("--chunk", type=int, default=1500)
    ap.add_argument("--top-k", type=int, default=24)
    ap.add_argument("--thr", type=float, default=0.1)
    ap.add_argument("--min-distance", type=int, default=3)
    ap.add_argument("--tile-w", type=int, default=2560)
    ap.add_argument(
        "--angular-norm-width",
        type=int,
        default=None,
        help="normalize the warped band to this width (constant px-per-degree "
        "across cameras: a Dahua 4096 px pano at 7680 shows the detector "
        "training-scale balls; coordinates map back through warp.scale)",
    )
    ap.add_argument("--overlap", type=int, default=256)
    ap.add_argument(
        "--boundary-margin",
        type=float,
        default=0.0,
        help="uniform px tolerance around ALL field boundaries (end lines behind "
        "goals + dome above the far line) so out-of-play exits stay detectable "
        "and the OOB/aerial physics can engage. 0 = legacy far-touchline margin only",
    )
    ap.add_argument(
        "--start-g",
        type=int,
        default=None,
        help="limit the dump to global frames >= this",
    )
    ap.add_argument(
        "--end-g", type=int, default=None, help="limit the dump to global frames < this"
    )
    ap.add_argument("--no-hwaccel", action="store_true")
    args = ap.parse_args()

    import av
    import cv2
    import torch

    from training.cli.eval_detector import infer_band
    from training.data_prep import distill_dataset as dd
    from training.data_prep.heatmap_dataset import (
        _dewarp_mask_gray,
        _far_margin_polygon,
        _native_iso_warp,
    )
    from training.data_prep.segment_decode import iter_frames_from_segments
    from training.data_prep.warped_dataset import resolve_video_rotation
    from training.models.heatmap_net import load_detector_checkpoint
    from training.world_model.eval import extract_peaks
    from video_grouper.inference.ball_detector import blob_diameter
    from video_grouper.inference.iso_warp import expand_polygon

    gd = Path(args.game_dir)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    gj = json.loads((gd / "game.json").read_text(encoding="utf-8", errors="ignore"))
    if not gj.get("field_polygon"):
        raise SystemExit(f"{gd.name}: no field polygon — cannot dewarp")
    ranges = dd.active_play_ranges(gj["segments"], gj.get("game_state"))
    total = int(gj.get("total_frames") or 0)
    grid = sample_grid(ranges, args.stride, total)
    if args.start_g is not None or args.end_g is not None:
        lo = args.start_g if args.start_g is not None else -(10**12)
        hi = args.end_g if args.end_g is not None else 10**12
        grid = [g for g in grid if lo <= g < hi]  # window for fast per-clip iteration
    spans = chunk_spans(grid, args.chunk)
    print(
        f"{gd.name}: {len(grid)} frames (stride {args.stride}, "
        f"{'active-play' if ranges else 'FULL GAME (no phases)'}), "
        f"{len(spans)} chunks",
        flush=True,
    )

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    # prelude + net (raw frames in, logits out); geometry inferred from the ckpt,
    # encoding from its metadata — an explicit --base mismatch is a hard error.
    model, _meta = load_detector_checkpoint(args.ckpt, base=args.base, device=dev)

    clip = gj.get("combined_video") or "combined.mp4"
    vrot = resolve_video_rotation(str(gd / clip), gj.get("video_rotation"))
    probe_path = gd / clip
    if not probe_path.exists():
        probe_path = gd / f"{gj['segments'][0]['seg']}.mp4"
    with av.open(str(probe_path)) as probe:
        vs = probe.streams.video[0]
        sw, sh = vs.codec_context.width, vs.codec_context.height
    far_poly = expand_polygon(
        _far_margin_polygon(gj["field_polygon"], 400.0), args.boundary_margin
    )
    warp = _native_iso_warp(far_poly, sw, sh, args.angular_norm_width)
    bh, bw = warp.shape
    mpoly = warp.points(far_poly).astype(np.int32)
    mask = np.zeros((bh, bw), np.uint8)
    cv2.fillPoly(mask, [mpoly], 255)

    for start, end, frames_want in spans:
        part = out / f"part_{start:07d}_{end:07d}.pkl"
        if part.exists():
            print(f"  chunk {start}-{end}: exists, skipping", flush=True)
            continue
        need: set[int] = set()
        for g in frames_want:
            need.update(k for k in (g, g - 1, g - 2) if k >= 0)
        warped: dict[int, np.ndarray] = {}
        cands: dict[int, list[tuple[float, float, float]]] = {}
        wanted = set(frames_want)
        for idx, img in iter_frames_from_segments(
            gd, gj["segments"], sorted(need), vrot, hwaccel=not args.no_hwaccel
        ):
            warped[idx] = _dewarp_mask_gray(img, warp, mask)
            if idx in wanted:
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
                # 4th element = observed blob diameter, SOURCE px (the
                # eval_detector convention) — fullgame dumps previously carried
                # no sizes, keeping size_cont_w dormant (EXP-DIST-32/47).
                cands[idx] = [
                    (
                        round(float(hx) / warp.scale, 1),
                        round(float(hy) / warp.scale + warp.y_top, 1),
                        round(float(sc), 4),
                        round(
                            blob_diameter(grays[-1], int(hx), int(hy))
                            / max(warp.scale, 1e-6),
                            1,
                        ),
                    )
                    for (hx, hy, sc) in peaks
                ]
            for _k in [k for k in warped if k < idx - 2]:
                del warped[_k]
        tmp = part.with_suffix(".tmp")
        with open(tmp, "wb") as fh:
            pickle.dump(cands, fh)
        tmp.rename(part)
        print(f"  chunk {start}-{end}: {len(cands)} frames dumped", flush=True)

    meta = {
        "schema": "fullgame_candidates/1",
        "game_dir": str(gd),
        "ckpt": str(args.ckpt),
        "params": {
            "stride": args.stride,
            "top_k": args.top_k,
            "thr": args.thr,
            "min_distance": args.min_distance,
        },
        "ranges": ranges,
        "n_frames": len(grid),
        "parts": sorted(p.name for p in out.glob("part_*.pkl")),
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    print(f"{gd.name}: DONE ({len(meta['parts'])} parts)", flush=True)


if __name__ == "__main__":
    main()
