"""Held-out eval: OUR detector -> peaks -> the EXISTING tracker -> vs human GT in meters.

The real bar (Mark): beat AutoCam on far balls. On the frames AutoCam loses, its own viewport sits
~0.15 R15m from the true ball while the tracker over *good* detections hits 0.77 — so the question is
only whether our distilled detector produces detections that good. This runs our ``HeatmapNet`` over
the held-out game's field band (tiled to fit the 1060), extracts peaks, maps them back to source px,
tracks them with ``world_model.reranker.track_ball``, and scores the track against the human GT in
meters (R5/10/15), split far vs near by apparent ball size, head-to-head with the AutoCam viewport.

    python -m training.cli.eval_detector --ckpt G:/ballresearch/distill/runs/hm_reolink/best.pt \
        --game-dir "F:/Heat_2013s/2026.05.31 - vs Spencerport gold 2 (away)" --base 24
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from training.data_prep import distill_dataset as dd


def _pad8(a: np.ndarray) -> tuple[np.ndarray, int, int]:
    """Pad an ``(C, H, W)`` stack so H, W are multiples of 8 (the net's 3 downsamples)."""
    _, h, w = a.shape
    ph, pw = (-h) % 8, (-w) % 8
    if ph or pw:
        a = np.pad(a, ((0, 0), (0, ph), (0, pw)), mode="constant")
    return a, h, w


def infer_band(model, dev, stack: np.ndarray, tile_w: int, overlap: int) -> np.ndarray:
    """Run the fully-conv detector over a wide field band in horizontal tiles; stitch the sigmoid
    heatmaps by max in the overlaps. ``stack`` is ``(3, bh, bw)`` float32 in [0, 1]. Returns ``(bh, bw)``.
    """
    import torch

    _, bh, bw = stack.shape
    hm = np.zeros((bh, bw), np.float32)
    x0 = 0
    while x0 < bw:
        x1 = min(x0 + tile_w, bw)
        tile = stack[:, :, x0:x1]
        padded, th, tw = _pad8(tile)
        with torch.no_grad():
            t = torch.from_numpy(padded[None]).to(dev)
            out = torch.sigmoid(model(t))[0, 0, :th, :tw].cpu().numpy()
        hm[:, x0:x1] = np.maximum(hm[:, x0:x1], out)
        if x1 >= bw:
            break
        x0 = x1 - overlap
    return hm


def _hits(errs, radii):
    if not errs:
        return {f"R{r}m": None for r in radii} | {"n": 0, "median_m": None}
    return {
        f"R{r}m": round(float(np.mean([e <= r for e in errs])), 3) for r in radii
    } | {"n": len(errs), "median_m": round(float(np.median(errs)), 1)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", required=True)
    ap.add_argument("--game-dir", required=True)
    ap.add_argument("--base", type=int, default=24)
    ap.add_argument(
        "--stride",
        type=int,
        default=4,
        help="eval every Nth source frame (match dumps)",
    )
    ap.add_argument("--radii", type=float, nargs="+", default=[5, 10, 15])
    ap.add_argument("--top-k", type=int, default=24)
    ap.add_argument("--thr", type=float, default=0.1)
    ap.add_argument(
        "--far-size-px", type=float, default=8.0, help="apparent ball < this = far"
    )
    ap.add_argument("--tile-w", type=int, default=2560)
    ap.add_argument("--overlap", type=int, default=256)
    ap.add_argument("--no-hwaccel", action="store_true")
    ap.add_argument(
        "--max-frames", type=int, default=6000, help="cap the eval span (frames)"
    )
    args = ap.parse_args()

    import av
    import cv2
    import torch

    from training.data_prep.heatmap_dataset import (
        _dewarp_mask_gray,
        _far_margin_polygon,
        _native_iso_warp,
    )
    from training.data_prep.warped_dataset import (
        apply_display_rotation,
        resolve_video_rotation,
    )
    from training.models.heatmap_net import HeatmapNet
    from training.world_model.eval import extract_peaks
    from training.world_model.geometry import build_field_geometry
    from training.world_model.reranker import track_ball
    from training.world_model.tbd import Candidate

    vdir = Path(args.game_dir)
    gj = json.loads((vdir / "game.json").read_text(encoding="utf-8", errors="ignore"))
    poly = gj["field_polygon"]
    geom = build_field_geometry(np.asarray(poly, float))
    if not geom.valid:
        raise SystemExit(
            "held-out game geometry is neutral — need a valid homography for meters"
        )
    offs = dd.seg_offsets(gj["segments"])
    balls, _ = dd.load_human_labels(vdir / "ball_labels.jsonl", offs)
    vps = (
        dd.load_viewport(vdir / "autocam_viewport.jsonl", offs)
        if (vdir / "autocam_viewport.jsonl").exists()
        else {}
    )
    if not balls:
        raise SystemExit("no human GT (ball_labels.jsonl) in the held-out game")
    video = gj.get("combined_video")
    if not video or not Path(video).exists():
        cands = list(vdir.glob("combined*.mp4")) or list(vdir.glob("*-raw.mp4"))
        if not cands:
            raise SystemExit(f"no video found in {vdir}")
        video = str(cands[0])

    # eval span = around the GT (contiguous, capped) so the tracker has continuity
    lo, hi = min(balls), max(balls)
    if hi - lo > args.max_frames:
        hi = lo + args.max_frames
        balls = {f: xy for f, xy in balls.items() if lo <= f <= hi}
    eval_frames = list(range(lo - 2 if lo >= 2 else 0, hi + 1, args.stride))
    print(
        f"{gj['game_id']}: {len(balls)} GT balls in span {lo}..{hi}, "
        f"{len(eval_frames)} eval frames (stride {args.stride})",
        flush=True,
    )

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model = HeatmapNet(in_frames=3, in_ch_per_frame=1, base=args.base).to(dev)
    ck = torch.load(args.ckpt, map_location=dev)
    model.load_state_dict(ck["model"] if "model" in ck else ck)
    model.eval()

    vrot = resolve_video_rotation(video, gj.get("video_rotation"))
    _hw = None
    if not args.no_hwaccel:
        try:
            _hw = av.codec.hwaccel.HWAccel(
                device_type="cuda", allow_software_fallback=True
            )
        except Exception:  # noqa: BLE001
            _hw = None
    container = av.open(video, hwaccel=_hw) if _hw else av.open(video)
    stream = container.streams.video[0]
    if _hw is None:
        stream.thread_type = "AUTO"
    sw, sh = stream.codec_context.width, stream.codec_context.height
    far_poly = _far_margin_polygon(poly, 400.0)
    warp = _native_iso_warp(far_poly, sw, sh, None)
    bh, bw = warp.shape
    mpoly = warp.points(far_poly).astype(np.int32)
    mask = np.zeros((bh, bw), np.uint8)
    cv2.fillPoly(mask, [mpoly], 255)

    want = set(eval_frames)
    frames_cands: dict[int, list] = {}
    buf: list = []
    idx = -1
    lo_dec = min(want)
    for fr in container.decode(stream):
        idx += 1
        if idx < lo_dec:
            continue
        img = apply_display_rotation(fr.to_ndarray(format="bgr24"), vrot)
        buf.append(_dewarp_mask_gray(img, warp, mask))
        if len(buf) > 3:
            buf.pop(0)
        if idx in want:
            grays = buf if len(buf) == 3 else [buf[0]] * (3 - len(buf)) + buf
            stack = np.stack(grays, 0).astype(np.float32) / 255.0
            hm = infer_band(model, dev, stack, args.tile_w, args.overlap)
            frames_cands[idx] = [
                Candidate(x=hx / warp.scale, y=hy / warp.scale + warp.y_top, score=sc)
                for (hx, hy, sc) in extract_peaks(
                    hm, top_k=args.top_k, threshold=args.thr
                )
            ]
        if idx >= max(want):
            break
    container.close()
    print(f"ran detector on {len(frames_cands)} frames", flush=True)

    # existing tracker over OUR detector's candidates
    ef = sorted(frames_cands)
    gaps = [
        (ef[i + 1] - ef[i]) if i + 1 < len(ef) else args.stride for i in range(len(ef))
    ]
    track = track_ball([frames_cands[f] for f in ef], geom, frame_gaps=gaps)
    fidx = {f: i for i, f in enumerate(ef)}

    det_err, vp_err, ceil_err, sizes = [], [], [], []
    for g, gt in balls.items():
        near = min(ef, key=lambda f: abs(f - g)) if ef else None
        if near is None or abs(near - g) > args.stride or fidx[near] not in track:
            continue
        gw = geom.image_to_world(np.asarray([gt], float))[0]
        tw = geom.image_to_world(np.asarray([track[fidx[near]]], float))[0]
        det_err.append((float(np.linalg.norm(tw - gw)), g))
        sizes.append(float(geom.expected_ball_diameter_px(np.asarray(gt))[0]))
        if g in vps:
            vw = geom.image_to_world(np.asarray([vps[g]], float))[0]
            vp_err.append(float(np.linalg.norm(vw - gw)))
        cands = frames_cands.get(near, [])
        if cands:
            ceil_err.append(
                min(
                    float(
                        np.linalg.norm(
                            geom.image_to_world(np.asarray([(c.x, c.y)], float))[0] - gw
                        )
                    )
                    for c in cands
                )
            )

    de = [e for e, _ in det_err]
    sz = np.asarray(sizes)
    far = [e for (e, _), s in zip(det_err, sz, strict=False) if s < args.far_size_px]
    nearb = [e for (e, _), s in zip(det_err, sz, strict=False) if s >= args.far_size_px]
    print(
        f"\n=== HELD-OUT EVAL (n={len(de)} GT balls, far<{args.far_size_px}px apparent) ==="
    )
    print(f"  OUR detector -> tracker : {_hits(de, args.radii)}")
    print(f"    far  : {_hits(far, args.radii)}")
    print(f"    near : {_hits(nearb, args.radii)}")
    if ceil_err:
        print(f"  candidate ceiling       : {_hits(ceil_err, args.radii)}")
    if vp_err:
        print(f"  AutoCam viewport (bar)  : {_hits(vp_err, args.radii)}")


if __name__ == "__main__":
    main()
