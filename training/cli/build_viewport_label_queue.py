"""Build a VIEWPORT labeling set — label the expected rendered output (Mark, 2026-07-19).

Instead of labeling balls, the annotator sees the currently planned viewport, AutoCam's
aim, and the GT ball (when known) drawn on a full-frame strip, and places the focal
point the broadcast SHOULD be centered on (sizing the view around it). Sets are queued
from the segments that look worst to a viewer:

  * round-trip WHIPS: fast, large pan excursions of the planned path that reverse
    (net displacement << path length — the camera darted somewhere and came back);
  * sustained DIVERGENCE from AutoCam's aim (both can't be right);

ranked worst-first. Frames inside each segment are sampled on a stride grid.

    python -m training.cli.build_viewport_label_queue \
        --game-dir "F:/Heat_2012s/2026.06.06 - vs Fairport (away)" \
        --cams G:/ballresearch/selector/cams_fair.pkl --set-name fair_viewport_worst
"""

from __future__ import annotations

import argparse
import json
import pickle
from pathlib import Path

import numpy as np


def find_segments(
    cx: np.ndarray,
    vps: dict[int, tuple[float, float]],
    *,
    speed_thr: float,
    min_amp: float,
    div_thr: float,
    div_min_frames: int,
) -> list[tuple[int, int, float, str]]:
    """(lo, hi, badness, kind) segments: round-trip whips + sustained AC divergence."""
    n = len(cx)
    v = np.abs(np.diff(cx, prepend=cx[0]))
    fast = v > speed_thr
    out: list[tuple[int, int, float, str]] = []
    i = 0
    while i < n:
        if not fast[i]:
            i += 1
            continue
        j = i
        while j < n - 1 and (fast[j + 1] or any(fast[j + 1 : min(n, j + 11)])):
            j += 1
            if j - i > 2000:
                break
        seg = cx[max(0, i - 10) : min(n, j + 11)]
        amp = float(seg.max() - seg.min())
        net = float(abs(seg[-1] - seg[0]))
        path = float(np.abs(np.diff(seg)).sum())
        if amp >= min_amp and net / max(path, 1.0) < 0.5:
            out.append((max(0, i - 20), min(n - 1, j + 20), amp * 2.0, "whip"))
        i = j + 1
    run: list[int] | None = None
    for g in range(n):
        ac = vps.get(g)
        far = ac is not None and abs(cx[g] - float(ac[0])) > div_thr
        if far:
            run = [g, g] if run is None else [run[0], g]
        else:
            if run and run[1] - run[0] >= div_min_frames:
                d = float(
                    np.mean(
                        [
                            abs(cx[k] - vps[k][0])
                            for k in range(run[0], run[1] + 1)
                            if k in vps
                        ]
                    )
                )
                out.append((run[0], run[1], d, "diverge"))
            run = None
    if run and run[1] - run[0] >= div_min_frames:
        d = float(
            np.mean(
                [abs(cx[k] - vps[k][0]) for k in range(run[0], run[1] + 1) if k in vps]
            )
        )
        out.append((run[0], run[1], d, "diverge"))
    return sorted(out, key=lambda s: -s[2])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--game-dir", required=True)
    ap.add_argument(
        "--cams",
        required=True,
        help="pkl with {'cams': [(cx,cy,hfov), ...]} per source frame",
    )
    ap.add_argument("--set-name", required=True)
    ap.add_argument("--out", default="D:/training_data/viewport_label")
    ap.add_argument("--max-frames", type=int, default=800)
    ap.add_argument(
        "--stride", type=int, default=4, help="frame grid (scrub granularity)"
    )
    ap.add_argument(
        "--pad-s",
        type=float,
        default=3.0,
        help="context seconds included around each mismatch segment",
    )
    ap.add_argument(
        "--full-game",
        action="store_true",
        help="stride grid over the WHOLE video instead of mismatch segments "
        "(segment kinds still annotated where they apply)",
    )
    ap.add_argument(
        "--scale", type=float, default=0.22, help="strip downscale vs source"
    )
    ap.add_argument(
        "--quality", type=int, default=72, help="strip JPEG quality (scrub speed)"
    )
    ap.add_argument("--speed-thr", type=float, default=40.0)
    ap.add_argument("--min-amp", type=float, default=800.0)
    ap.add_argument("--div-thr", type=float, default=1500.0)
    ap.add_argument("--div-min-s", type=float, default=2.0)
    ap.add_argument("--priority", type=int, default=1)
    ap.add_argument(
        "--no-active-play-filter",
        action="store_true",
        help="keep warm-up / halftime / pre- & post-game frames (default: drop "
        "them — those are NOT broadcast content and the planner wanders wildly "
        "with no ball, so they dominate the swing/divergence segments)",
    )
    ap.add_argument("--no-hwaccel", action="store_true")
    args = ap.parse_args()

    import av
    import cv2

    from training.data_prep import distill_dataset as dd
    from training.data_prep.segment_decode import iter_frames_from_segments
    from training.data_prep.warped_dataset import resolve_video_rotation

    gd = Path(args.game_dir)
    gj = json.loads((gd / "game.json").read_text(encoding="utf-8", errors="ignore"))
    offs = dd.seg_offsets(gj["segments"])
    balls, _ = (
        dd.load_human_labels(gd / "ball_labels.jsonl", offs)
        if (gd / "ball_labels.jsonl").exists()
        else ({}, set())
    )
    vps = (
        dd.load_viewport(gd / "autocam_viewport.jsonl", offs)
        if (gd / "autocam_viewport.jsonl").exists()
        else {}
    )
    cams = pickle.loads(Path(args.cams).read_bytes())["cams"]
    cx = np.array([c[0] for c in cams])
    fps = 20.0

    segs = find_segments(
        cx,
        vps,
        speed_thr=args.speed_thr,
        min_amp=args.min_amp,
        div_thr=args.div_thr,
        div_min_frames=int(args.div_min_s * fps),
    )
    n_cams = len(cx)
    pad = int(args.pad_s * fps)
    if args.full_game:
        frames = list(range(0, n_cams, args.stride))[: args.max_frames]
    else:
        if not segs:
            raise SystemExit("no whip/divergence segments found — nothing to label")
        # contiguous stride grid over each PADDED segment (worst first) so the
        # annotator can scrub through the failure with surrounding context
        frames = []
        for lo, hi, _bad, _kind in segs:
            a = max(0, lo - pad)
            b = min(n_cams - 1, hi + pad)
            frames.extend(range(a - a % args.stride, b + 1, args.stride))
            if len(set(frames)) >= args.max_frames:
                break
        frames = sorted(set(frames))[: args.max_frames]

    # Ship non-active-play frames (Mark 2026-07-21): warm-up/HT/pre-&post-game
    # are not broadcast content — the planner wanders with no ball there, so
    # those frames flood the swing/divergence set. Drop any frame outside the
    # first_half/second_half ranges from game_state. If game_state has no phases
    # (or mislabels them, e.g. first_half from frame 0), this is a no-op and the
    # game needs a phase-boundary fix — see the game-phase audit backlog.
    if not args.no_active_play_filter:
        ranges = dd.active_play_ranges(gj["segments"], gj.get("game_state"))
        if ranges:
            before = len(frames)
            frames = [g for g in frames if any(lo <= g <= hi for lo, hi in ranges)]
            print(
                f"active-play filter: kept {len(frames)}/{before} frames "
                f"(dropped {before - len(frames)} warm-up/HT/pre-post)",
                flush=True,
            )
        else:
            print(
                "active-play filter: game_state has no first/second-half phases "
                "— keeping all frames (this game needs a phase-boundary fix)",
                flush=True,
            )
    if not frames:
        raise SystemExit("no frames left after the active-play filter")
    kind_by_frame: dict[int, str] = {}
    for lo, hi, _bad, kind in segs:
        for g in range(max(0, lo - pad), min(n_cams - 1, hi + pad) + 1):
            kind_by_frame.setdefault(g, kind if lo <= g <= hi else f"near-{kind}")

    out_dir = Path(args.out) / args.set_name
    (out_dir / "strips").mkdir(parents=True, exist_ok=True)

    with av.open(str(gd / "combined.mp4")) as probe:
        vs = probe.streams.video[0]
        sw, sh = vs.codec_context.width, vs.codec_context.height
    vrot = resolve_video_rotation(str(gd / "combined.mp4"), gj.get("video_rotation"))

    n_written = 0
    for g, img in iter_frames_from_segments(
        gd, gj["segments"], set(frames), vrot, hwaccel=not args.no_hwaccel
    ):
        small = cv2.resize(img, (int(sw * args.scale), int(sh * args.scale)))
        cv2.imwrite(
            str(out_dir / "strips" / f"f{g:06d}.jpg"),
            small,
            [cv2.IMWRITE_JPEG_QUALITY, args.quality],
        )
        n_written += 1

    entries = []
    for g in frames:
        if not (out_dir / "strips" / f"f{g:06d}.jpg").exists():
            continue
        our = cams[g] if g < len(cams) else None
        gt = balls.get(g)
        ac = vps.get(g)
        entries.append(
            {
                "frame_idx": g,
                "kind": kind_by_frame.get(g, "?"),
                "our": [
                    round(float(our[0]), 1),
                    round(float(our[1]), 1),
                    round(float(our[2]), 2),
                ]
                if our
                else None,
                "ac": [round(float(ac[0]), 1), round(float(ac[1]), 1)] if ac else None,
                "gt": [round(float(gt[0]), 1), round(float(gt[1]), 1)] if gt else None,
            }
        )
    manifest = {
        "set": args.set_name,
        "game_dir": str(gd),
        "src_w": sw,
        "src_h": sh,
        "scale": args.scale,
        "priority": args.priority,
        "n_frames": len(entries),
        "frames": entries,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1))
    print(
        f"WROTE {out_dir}/manifest.json: {len(entries)} frames "
        f"({n_written} strips) from {len(segs)} segments "
        f"(top kinds: {[s[3] for s in segs[:6]]})",
        flush=True,
    )


if __name__ == "__main__":
    main()
