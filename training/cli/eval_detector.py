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


def _blob_diam(gray: np.ndarray, hx: int, hy: int, win: int = 61) -> float:
    """Observed apparent blob diameter — delegates to the PRODUCT implementation
    (``video_grouper.inference.ball_detector.blob_diameter``) so the runtime,
    eval CLIs, and dumps all measure size identically. Kept under the old name
    for the existing call sites."""
    from video_grouper.inference.ball_detector import blob_diameter

    return blob_diameter(gray, hx, hy, win)


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
    ap.add_argument(
        "--base",
        type=int,
        default=None,
        help="expected base width; inferred from the checkpoint, mismatch = error",
    )
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
    ap.add_argument(
        "--min-distance",
        type=int,
        default=3,
        help="peak NMS radius in band px (bigger = merge nearby peaks)",
    )
    ap.add_argument(
        "--no-infield-gate",
        action="store_true",
        help="disable in-field candidate gating (default: gate — the teacher was in-field "
        "filtered, so ungated eval peaks flood the tracker with off-field distractors)",
    )
    ap.add_argument(
        "--infield-margin",
        type=float,
        default=120.0,
        help="in-field gate margin in source px (covers lines/throw-ins/airborne)",
    )
    ap.add_argument(
        "--no-size-gate",
        action="store_true",
        help="disable size-consistency gate (default: gate — reject candidates whose observed blob is "
        "geometrically too big for the perspective-expected ball size at that field location)",
    )
    ap.add_argument(
        "--size-max-ratio",
        type=float,
        default=3.0,
        help="reject a candidate if observed diameter > this * expected ball diameter (a real ball is "
        "never this oversized; a player at a far-field spot is ~5x)",
    )
    ap.add_argument(
        "--size-win", type=int, default=61, help="blob-measure window (band px)"
    )
    ap.add_argument("--tile-w", type=int, default=2560)
    ap.add_argument("--overlap", type=int, default=256)
    ap.add_argument("--no-hwaccel", action="store_true")
    ap.add_argument(
        "--max-frames", type=int, default=6000, help="cap the eval span (frames)"
    )
    ap.add_argument(
        "--span-lo",
        type=int,
        default=None,
        help="only use GT at global frames >= this (target a specific GT cluster)",
    )
    ap.add_argument(
        "--span-hi",
        type=int,
        default=None,
        help="only use GT at global frames <= this",
    )
    ap.add_argument(
        "--dump-cands",
        default=None,
        help="also pickle raw candidates (+observed size) + GT here for cli/sweep_tracker",
    )
    ap.add_argument(
        "--stabilize",
        action="store_true",
        help="wind-align bands to the first decoded frame before inference; OUR "
        "metrics + the dump score against shift-corrected GT (AutoCam reference "
        "rows keep raw GT — they ran on raw frames). EXP-DIST-57.",
    )
    args = ap.parse_args()

    import cv2
    import torch

    from training.data_prep.heatmap_dataset import (
        _dewarp_mask_gray,
        _far_margin_polygon,
        _native_iso_warp,
    )
    from training.data_prep.warped_dataset import (
        resolve_video_rotation,
    )
    from training.models.heatmap_net import load_detector_checkpoint
    from training.world_model.eval import extract_peaks
    from training.world_model.geometry import build_field_geometry
    from training.world_model.reranker import track_ball
    from training.world_model.tbd import Candidate
    from video_grouper.inference.iso_warp import BandStabilizer

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
    if args.span_lo is not None or args.span_hi is not None:
        slo = args.span_lo if args.span_lo is not None else -(10**12)
        shi = args.span_hi if args.span_hi is not None else 10**12
        balls = {f: xy for f, xy in balls.items() if slo <= f <= shi}
        if not balls:
            raise SystemExit("no GT in the requested --span-lo/--span-hi window")
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
    # prelude + net (raw frames in, logits out); geometry inferred from the ckpt,
    # encoding from its metadata — an explicit --base mismatch is a hard error.
    model, _meta = load_detector_checkpoint(args.ckpt, base=args.base, device=dev)

    vrot = resolve_video_rotation(video, gj.get("video_rotation"))
    from training.data_prep.segment_decode import iter_frames_from_segments

    game_dir = Path(video).parent
    segments = gj["segments"]
    want = set(eval_frames)
    # Stream the 3-frame band ({f-2,f-1,f}) for each eval frame from the RAW per-segment clips instead
    # of decoding the whole combined video (which scored ~1500 stride-frames by grinding all ~13k
    # frames). Frame-exact, corruption-isolated, memory-light (one frame at a time).
    band_globals: set[int] = set()
    for f in want:
        band_globals.update((max(0, f - 2), max(0, f - 1), f))

    frames_cands: dict[int, list] = {}
    band_gray: dict[int, np.ndarray] = {}
    warp = None
    mask = None
    geo_plane = None  # gray3geo: per-game geometry plane (band px expected size)
    stab = BandStabilizer() if args.stabilize else None
    shifts: dict[int, tuple[float, float]] = {}
    for f, img in iter_frames_from_segments(
        game_dir, segments, band_globals, vrot, hwaccel=not args.no_hwaccel
    ):
        if warp is None:
            sh, sw = img.shape[:2]
            far_poly = _far_margin_polygon(poly, 400.0)
            warp = _native_iso_warp(far_poly, sw, sh, None)
            mpoly = warp.points(far_poly).astype(np.int32)
            mask = np.zeros(warp.shape, np.uint8)
            cv2.fillPoly(mask, [mpoly], 255)
            if _meta["encoding"] == "gray3geo":
                from video_grouper.inference.ball_detector import band_geo_plane

                geo_plane = band_geo_plane(np.asarray(poly, float), warp)
        band_gray[f] = _dewarp_mask_gray(img, warp, mask, stab)
        if stab is not None:
            shifts[f] = stab.last
        if f in want:
            g0 = band_gray[f]
            g1 = band_gray.get(f - 1, g0)
            g2 = band_gray.get(f - 2, g1)
            stack = np.stack((g2, g1, g0), 0).astype(np.float32) / 255.0
            if geo_plane is not None:
                stack = np.concatenate([stack, geo_plane[None]], axis=0)
            hm = infer_band(model, dev, stack, args.tile_w, args.overlap)
            cands = []
            for hx, hy, sc in extract_peaks(
                hm, top_k=args.top_k, threshold=args.thr, min_distance=args.min_distance
            ):
                sx = hx / warp.scale
                sy = hy / warp.scale + warp.y_top
                if not args.no_infield_gate and not bool(
                    geom.is_in_support(
                        np.asarray([(sx, sy)], float), margin_px=args.infield_margin
                    )[0]
                ):
                    continue
                observed = (
                    _blob_diam(g0, int(hx), int(hy), args.size_win)
                    / max(warp.scale, 1e-6)
                )  # observed apparent diameter, source px (for the size gate + the dump)
                if not args.no_size_gate:
                    expected = float(
                        geom.expected_ball_diameter_px(np.asarray([(sx, sy)], float))[0]
                    )
                    if expected > 0 and observed > args.size_max_ratio * expected:
                        continue  # too big for its field location — a player/structure, not the ball
                cands.append(Candidate(x=sx, y=sy, score=sc, size_px=observed))
            frames_cands[f] = cands
        for k in [k for k in band_gray if k < f - 2]:
            del band_gray[k]
    print(f"ran detector on {len(frames_cands)} frames", flush=True)

    # existing tracker over OUR detector's candidates. frame_gaps[t] = gap INTO t (backward diff).
    ef = sorted(frames_cands)
    gaps = [args.stride] + [ef[i] - ef[i - 1] for i in range(1, len(ef))]
    track = track_ball([frames_cands[f] for f in ef], geom, frame_gaps=gaps)
    fidx = {f: i for i, f in enumerate(ef)}

    def _reg_gt(g, gt):
        """GT (raw-frame source px) -> anchor-registered coords via the nearest
        decoded eval frame's wind shift (wind moves slowly vs the stride)."""
        if stab is None or warp is None or not ef:
            return (float(gt[0]), float(gt[1]))
        nf = min(ef, key=lambda f: abs(f - g))
        sdx, sdy = shifts.get(nf, (0.0, 0.0))
        return (float(gt[0]) - sdx / warp.scale, float(gt[1]) - sdy / warp.scale)

    if args.dump_cands:
        # Cache raw candidates (+ observed size) + GT so cli/sweep_tracker can replay track_ball
        # under many configs in seconds — decode is the 40-min cost; tracking is milliseconds.
        import pickle

        dump = {
            "polygon": poly,
            "ef": ef,
            "gaps": gaps,
            "cands": {
                f: [(c.x, c.y, c.score, c.size_px) for c in frames_cands[f]] for f in ef
            },
            # stabilized runs: candidates are anchor-registered, so ship GT in the
            # same space — sweep_tracker replays unchanged either way
            "balls": {g: _reg_gt(g, gt) for g, gt in balls.items()}
            if stab is not None
            else balls,
            "far_size_px": args.far_size_px,
            "stride": args.stride,
        }
        if stab is not None:
            dump["stabilize"] = True
        with open(args.dump_cands, "wb") as fh:
            pickle.dump(dump, fh)
        print(
            f"dumped {sum(len(v) for v in dump['cands'].values())} candidates / "
            f"{len(ef)} frames -> {args.dump_cands}",
            flush=True,
        )

    # Reference: AutoCam's OWN detections through OUR tracker (same frames/gaps). This isolates
    # detection from selection: if it beats OUR-detector->tracker on far, our detector is the gap;
    # if it also loses on far, selection (the tracker) is the gap, not the detector.
    ac_track = {}
    ac_path = vdir / "autocam_detections.jsonl"
    if ac_path.exists():
        ac_dets = dd.load_detections(ac_path, offs)
        ac_cands = [
            [Candidate(x=x, y=y, score=cf) for (x, y, cf) in ac_dets.get(f, [])]
            for f in ef
        ]
        ac_track = track_ball(ac_cands, geom, frame_gaps=gaps)

    # Per-ball rows: (apparent_size_px, our_err, autocam_viewport_err|None, our_candidate_ceiling|None).
    # The goal is a head-to-head: match AutoCam on near+medium, beat it on far. That only reads cleanly
    # if BOTH our track AND AutoCam's viewport are split by the SAME size gate — so score them together
    # per ball and split afterward, rather than reporting AutoCam as a single blended number.
    rows = []
    for g, gt in balls.items():
        near = min(ef, key=lambda f: abs(f - g)) if ef else None
        if near is None or abs(near - g) > args.stride or fidx[near] not in track:
            continue
        # OUR metrics score in the (possibly) anchor-registered space the detector
        # ran in; the AutoCam references ran on raw frames and keep raw GT.
        gw = geom.image_to_world(np.asarray([_reg_gt(g, gt)], float))[0]
        gw_raw = geom.image_to_world(np.asarray([gt], float))[0]
        tw = geom.image_to_world(np.asarray([track[fidx[near]]], float))[0]
        our = float(np.linalg.norm(tw - gw))
        size = float(geom.expected_ball_diameter_px(np.asarray(gt))[0])
        vpe = None
        if g in vps:
            vw = geom.image_to_world(np.asarray([vps[g]], float))[0]
            vpe = float(np.linalg.norm(vw - gw_raw))
        cands = frames_cands.get(near, [])
        ce = (
            min(
                float(
                    np.linalg.norm(
                        geom.image_to_world(np.asarray([(c.x, c.y)], float))[0] - gw
                    )
                )
                for c in cands
            )
            if cands
            else None
        )
        ace = None
        if fidx[near] in ac_track:
            aw = geom.image_to_world(np.asarray([ac_track[fidx[near]]], float))[0]
            ace = float(np.linalg.norm(aw - gw_raw))
        rows.append((size, our, vpe, ce, ace))

    def _col(rws, i):
        return [r[i] for r in rws if r[i] is not None]

    far_rows = [r for r in rows if r[0] < args.far_size_px]
    near_rows = [r for r in rows if r[0] >= args.far_size_px]
    print(
        f"\n=== HELD-OUT EVAL: {gj['game_id']} "
        f"(n={len(rows)} GT balls; far = apparent <{args.far_size_px}px) ==="
    )
    print("  goal: OUR ~= AutoCam on NEAR+MED, and OUR beats AutoCam on FAR\n")

    def _band(title, rws):
        if not rws:
            return
        print(f"  [{title}]  n={len(rws)}")
        print(f"    OUR detector -> tracker : {_hits(_col(rws, 1), args.radii)}")
        if any(r[2] is not None for r in rws):
            print(f"    AutoCam viewport        : {_hits(_col(rws, 2), args.radii)}")
        if any(r[4] is not None for r in rws):
            print(f"    AutoCam dets -> OUR trk : {_hits(_col(rws, 4), args.radii)}")
        if any(r[3] is not None for r in rws):
            print(f"    OUR candidate ceiling   : {_hits(_col(rws, 3), args.radii)}")

    _band("ALL", rows)
    _band("NEAR+MED", near_rows)
    _band("FAR", far_rows)


if __name__ == "__main__":
    main()
