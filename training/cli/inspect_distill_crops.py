"""Vision-gate inspector for the AutoCam-distillation dataset.

Renders overlay PNGs for a few sampled label frames per game so a human (or vision model) can
verify, on real frames, that:

* the field band is masked correctly and rendered **right-side-up** (the flipped early-Dahua games
  with ``video_rotation = -180`` must come out upright),
* the **far/near split** lands where expected (far = top of the band, toward the far touchline),
* the ball sits **under the Gaussian target blob** (label↔frame alignment),
* viewport-gated selection picked the ball and not an off-field false positive.

It reuses the *exact* ``build_heatmap_crops`` decode path (``_far_margin_polygon`` → ``_native_iso_warp``
→ ``apply_display_rotation`` → ``_dewarp_mask_gray`` → ``warp.points``) so what you see is what trains.
This is a read-only dataset-inspection tool — it renders nothing into the training store.

Usage::

    python -m training.cli.inspect_distill_crops "F:/.../game dir" [more dirs ...] \
        --out G:/ballresearch/distill/vis_gate --n 6
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np


def load_game_config(video_dir: Path) -> dict:
    """Build a ``build_distill_games`` config from a game's F: video dir (reads ``game.json``)."""
    gj = json.loads((video_dir / "game.json").read_text())
    # combined_video is sometimes blank in game.json; the marathon's source is the dir's combined.mp4
    # (frame_base=0 => decode index == global frame). Fall back to it, then to a *-raw.mp4.
    video = gj.get("combined_video")
    if not video:
        for cand in ("combined.mp4", "combined_rotated.mp4"):
            if (video_dir / cand).exists():
                video = str(video_dir / cand)
                break
        if not video:
            raws = sorted(video_dir.glob("*-raw.mp4"))
            video = str(raws[0]) if raws else str(video_dir / "video.mp4")
    gc = {
        "game_id": gj["game_id"],
        "video": video,
        "segments": gj["segments"],
        "polygon": gj.get("field_polygon"),
        "detections": str(video_dir / "autocam_detections.jsonl"),
        "viewport": str(video_dir / "autocam_viewport.jsonl"),
        "camera": gj.get("camera", "?"),
        "team": gj.get("team", "?"),
        "video_rotation": gj.get("video_rotation", 0),
    }
    hl = video_dir / "ball_labels.jsonl"
    if hl.exists():
        gc["human_labels"] = str(hl)
    return gc


def _pick_samples(
    kept: dict, far_frames: set, human: dict, n: int
) -> list[tuple[int, str]]:
    """Choose up to ``n`` sample frames as ``(frame, tag)`` spread within a bounded window.

    Window is anchored at the earliest human label (if any) else the earliest kept near-ball, so the
    sequential decode stays bounded. Mixes near-auto, far-auto (dropped — shown for the split check),
    and human labels.
    """
    anchor = (
        min(human)
        if human
        else (min(kept) if kept else (min(far_frames) if far_frames else 0))
    )
    win = (anchor, anchor + 9000)

    def inwin(fs):
        return sorted(f for f in fs if win[0] <= f <= win[1])

    near = inwin(kept)
    far = inwin(far_frames)
    hum = inwin(human)
    picks: list[tuple[int, str]] = []
    # interleave: prefer 1 human, then spread near/far
    for src, tag in ((hum, "human"), (near, "near_auto"), (far, "far_auto_dropped")):
        if not src:
            continue
        take = max(1, n // 3)
        step = max(1, len(src) // take)
        for f in src[::step][:take]:
            picks.append((f, tag))
    # dedup by frame, keep order, cap at n
    seen = set()
    out = []
    for f, tag in picks:
        if f in seen:
            continue
        seen.add(f)
        out.append((f, tag))
    return out[:n]


def _render_panels(
    band_gray, dbx, dby, depth, far, tag, gid, frame, sigma, crop=256, far_curve=None
):
    import cv2

    from training.data_prep.heatmap_dataset import gaussian_heatmap

    bh, bw = band_gray.shape
    band_bgr = cv2.cvtColor(band_gray, cv2.COLOR_GRAY2BGR)

    # Panel A: full masked band downscaled to ~1600 wide, marker at the label.
    scale = 1600.0 / bw
    a = cv2.resize(band_bgr, (1600, max(1, int(round(bh * scale)))))
    # far/near boundary curve (the curve-following far split) — should hug the far touchline
    if far_curve is not None and len(far_curve):
        pts = (np.asarray(far_curve, np.float64) * scale).round().astype(np.int32)
        cv2.polylines(a, [pts], False, (255, 200, 0), 2)
        cv2.putText(
            a,
            "far<->near boundary",
            (pts[0][0] + 4, pts[0][1] - 6),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (255, 200, 0),
            1,
        )
    ax, ay = int(round(dbx * scale)), int(round(dby * scale))
    cv2.circle(a, (ax, ay), 16, (0, 0, 255), 2)
    cv2.drawMarker(a, (ax, ay), (0, 255, 255), cv2.MARKER_CROSS, 28, 2)
    txt = f"{gid}  f={frame}  {tag}  depth={depth:.0%}  far={'Y' if far else 'N'}"
    cv2.putText(a, txt, (10, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

    # Panel B: 256 crop around the label + Gaussian target (sigma) blended as a red heat overlay.
    half = crop // 2
    x0 = int(np.clip(round(dbx) - half, 0, max(0, bw - crop)))
    y0 = int(np.clip(round(dby) - half, 0, max(0, bh - crop)))
    patch = np.zeros((crop, crop, 3), np.uint8)
    sub = band_bgr[y0 : y0 + crop, x0 : x0 + crop]
    patch[: sub.shape[0], : sub.shape[1]] = sub
    lx, ly = dbx - x0, dby - y0
    tgt = gaussian_heatmap(crop, crop, lx, ly, sigma)
    heat = np.zeros((crop, crop, 3), np.uint8)
    heat[..., 2] = (tgt * 255).astype(np.uint8)
    patch = cv2.addWeighted(patch, 1.0, heat, 0.5, 0)
    cv2.drawMarker(
        patch, (int(round(lx)), int(round(ly))), (0, 255, 255), cv2.MARKER_CROSS, 18, 1
    )
    patch = cv2.resize(patch, (512, 512), interpolation=cv2.INTER_NEAREST)
    cv2.putText(
        patch,
        "256 crop + target",
        (10, 24),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.6,
        (0, 255, 0),
        2,
    )

    # stack A over B (pad B to A's width)
    aw = a.shape[1]
    bpad = np.zeros((patch.shape[0], aw, 3), np.uint8)
    bpad[:, : patch.shape[1]] = patch
    return np.vstack([a, bpad])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("video_dirs", nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--sigma", type=float, default=4.0)
    ap.add_argument("--far-frac", type=float, default=0.22)
    ap.add_argument("--target-width", type=int, default=None)
    args = ap.parse_args()

    import av
    import cv2

    from training.data_prep import distill_dataset as dd
    from training.data_prep.heatmap_dataset import (
        _dewarp_mask_gray,
        _far_margin_polygon,
        _native_iso_warp,
    )
    from training.data_prep.warped_dataset import (
        apply_display_rotation,
        resolve_video_rotation,
    )
    from training.world_model.geometry import build_field_geometry

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    for vd in args.video_dirs:
        vdir = Path(vd)
        gc = load_game_config(vdir)
        gid = gc["game_id"]
        poly = gc["polygon"]
        if not poly:
            print(f"{gid}: NO field_polygon — skipped")
            continue

        # full build (prints the filter report line)
        dd.build_distill_games([gc], far_frac=args.far_frac, report=True)

        # reconstruct intermediates for sampling / far visualisation
        offsets = dd.seg_offsets(gc["segments"])
        geom = build_field_geometry(np.asarray(poly, float))
        far_edge, near_edge = dd.field_edges(poly)
        dets = dd.load_detections(gc["detections"], offsets)
        vps = dd.load_viewport(gc["viewport"], offsets)
        human, novis = (
            dd.load_human_labels(gc["human_labels"], offsets)
            if gc.get("human_labels")
            else ({}, set())
        )
        teacher = dd.select_teacher(dets, vps, geom)
        non_far, far_frames = dd.split_far(
            teacher, far_edge, near_edge, far_frac=args.far_frac
        )
        non_far, _ = dd.drop_frozen_runs(non_far)
        kept = dd.subsample(non_far)
        label_xy = dict(kept)
        label_xy.update(human)
        for g in novis:
            label_xy.pop(g, None)

        samples = _pick_samples(kept, far_frames, human, args.n)
        if not samples:
            print(f"{gid}: no samples in window — skipped")
            continue
        print(f"{gid}: geom.valid={geom.valid} samples={samples}")

        # source-px label per sample: human/near from label_xy; far_auto from teacher (dropped)
        def lbl(f):
            return label_xy.get(f) or teacher.get(f)

        # build the SAME warp+mask as the trainer
        container = av.open(gc["video"])
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        sw, sh = stream.codec_context.width, stream.codec_context.height
        vrot = resolve_video_rotation(gc["video"], gc.get("video_rotation"))
        far_poly = _far_margin_polygon(poly, 400.0)
        warp = _native_iso_warp(far_poly, sw, sh, args.target_width)
        bh, bw = warp.shape
        mpoly = warp.points(far_poly).astype(np.int32)
        mask = np.zeros((bh, bw), np.uint8)
        cv2.fillPoly(mask, [mpoly], 255)

        # the curve-following far/near boundary, in band coords, for the overlay
        bx_lo = max(far_edge[:, 0].min(), near_edge[:, 0].min())
        bx_hi = min(far_edge[:, 0].max(), near_edge[:, 0].max())
        cxs = np.linspace(bx_lo, bx_hi, 60)
        yfs = np.interp(cxs, far_edge[:, 0], far_edge[:, 1])
        yns = np.interp(cxs, near_edge[:, 0], near_edge[:, 1])
        ybnd = yfs + args.far_frac * (yns - yfs)
        far_curve = warp.points(np.column_stack([cxs, ybnd]))

        want = {f for f, _ in samples}
        lo, hi = min(want) - 2, max(want)
        buf: list = []
        idx = -1
        rendered = 0
        for fr in container.decode(stream):
            idx += 1
            if idx < lo:
                continue
            img = apply_display_rotation(fr.to_ndarray(format="bgr24"), vrot)
            buf.append(_dewarp_mask_gray(img, warp, mask))
            if len(buf) > 3:
                buf.pop(0)
            if idx in want:
                bx, by = lbl(idx)
                dxy = warp.points([(bx, by)])[0]
                dbx, dby = float(dxy[0]), float(dxy[1])
                depth = dd.curve_depth(bx, by, far_edge, near_edge)
                tag = next(t for f, t in samples if f == idx)
                far = idx in far_frames
                png = _render_panels(
                    buf[-1],
                    dbx,
                    dby,
                    depth,
                    far,
                    tag,
                    gid,
                    idx,
                    args.sigma,
                    far_curve=far_curve,
                )
                fn = out / f"{gid}_f{idx:06d}_{tag}.png"
                cv2.imwrite(str(fn), png)
                rendered += 1
                print(
                    f"  wrote {fn.name}  band_xy=({dbx:.0f},{dby:.0f}) src=({bx:.0f},{by:.0f})"
                )
            if idx >= hi:
                break
        container.close()
        print(f"{gid}: rendered {rendered} PNGs")


if __name__ == "__main__":
    main()
