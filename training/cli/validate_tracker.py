"""Run the EXISTING tracker (``world_model.reranker.track_ball``) over per-frame detection candidates
and (a) validate it follows the ball to the viewport bar in METERS vs human GT, and (b) emit the
GT-anchored teacher track.

Production path is ``detector -> this tracker -> viewport``. We don't reinvent the tracker; we feed it
AutoCam's detections (which recover the ball ~0.80 even where AutoCam's own viewport loses it) to
check the existing tracker clears the ~10–15 m follow bar GIVEN good detections — before training our
detector to match them. ``--gt-override`` anchors the track to the human GT at verified frames (the
teacher-generation mode); validation runs WITHOUT it (overriding then scoring the same GT is circular).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from training.data_prep import distill_dataset as dd


def labeled_dirs(roots: list[str]) -> list[Path]:
    out, seen = [], set()
    for r in roots:
        for gj in Path(r).glob("**/game.json"):
            d = gj.parent
            if d in seen:
                continue
            if (d / "ball_labels.jsonl").exists() and (
                d / "autocam_detections.jsonl"
            ).exists():
                seen.add(d)
                out.append(d)
    return out


def build_frames(dets, gframes, conf_floor, gt=None, novis=None):
    from training.world_model.tbd import Candidate

    gt, novis = gt or {}, novis or set()
    frames = []
    for g in gframes:
        if g in gt:
            frames.append([Candidate(x=gt[g][0], y=gt[g][1], score=1.0)])
        elif g in novis:
            frames.append([])
        else:
            frames.append(
                [
                    Candidate(x=x, y=y, score=max(c, 1e-3))
                    for (x, y, c) in dets.get(g, [])
                    if c >= conf_floor
                ]
            )
    return frames


def _hits(world_err: list[float], radii) -> dict:
    return {
        f"R{r}m": round(float(np.mean([e <= r for e in world_err])), 3) for r in radii
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--roots", nargs="+", required=True)
    ap.add_argument("--conf-floor", type=float, default=0.06)
    ap.add_argument(
        "--gt-override", action="store_true", help="anchor track to GT (teacher mode)"
    )
    ap.add_argument("--radii", type=float, nargs="+", default=[5, 10, 15])
    args = ap.parse_args()

    from training.world_model.geometry import build_field_geometry
    from training.world_model.reranker import track_ball

    all_track_err, all_vp_err, all_ceil_err, sizes = [], [], [], []
    vp_vecs = []
    vsvp_games = []  # per-game dense tracker-vs-viewport match (normal-play proxy GT)
    for d in labeled_dirs(args.roots):
        gj = json.loads((d / "game.json").read_text(encoding="utf-8", errors="ignore"))
        poly = gj.get("field_polygon")
        if not poly:
            print(f"  skip {d.name}: no polygon")
            continue
        geom = build_field_geometry(np.asarray(poly, float))
        if not geom.valid:
            print(f"  skip {d.name}: geom invalid (need meters)")
            continue
        offs = dd.seg_offsets(gj["segments"])
        dets = dd.load_detections(d / "autocam_detections.jsonl", offs)
        vps = (
            dd.load_viewport(d / "autocam_viewport.jsonl", offs)
            if (d / "autocam_viewport.jsonl").exists()
            else {}
        )
        balls, novis = dd.load_human_labels(d / "ball_labels.jsonl", offs)
        if not dets or not balls:
            continue
        gframes = sorted(dets)
        gaps = [
            (gframes[i + 1] - gframes[i]) if i + 1 < len(gframes) else 4
            for i in range(len(gframes))
        ]
        frames = build_frames(
            dets,
            gframes,
            args.conf_floor,
            gt=balls if args.gt_override else None,
            novis=novis if args.gt_override else None,
        )
        track = track_ball(frames, geom, frame_gaps=gaps)

        # dense per-game match vs the AutoCam viewport (proxy GT for normal play, every frame)
        if vps:
            vverr = [
                float(
                    np.linalg.norm(
                        geom.image_to_world(np.asarray([track[i]], float))[0]
                        - geom.image_to_world(np.asarray([vps[g]], float))[0]
                    )
                )
                for i, g in enumerate(gframes)
                if i in track and g in vps
            ]
            if vverr:
                vsvp_games.append(
                    (
                        d.name,
                        len(vverr),
                        _hits(vverr, args.radii),
                        float(np.median(vverr)),
                    )
                )

        def nearest_idx(g):
            # nearest strided detection frame to a GT frame (stride ~4)
            i = min(range(len(gframes)), key=lambda j: abs(gframes[j] - g))
            return i if abs(gframes[i] - g) <= 3 else None

        for g, gt in balls.items():
            i = nearest_idx(g)
            if i is None or i not in track:
                continue
            gw = geom.image_to_world(np.asarray([gt], float))[0]
            tw = geom.image_to_world(np.asarray([track[i]], float))[0]
            all_track_err.append(float(np.linalg.norm(tw - gw)))
            sizes.append(float(geom.expected_ball_diameter_px(np.asarray(gt))[0]))
            if g in vps:
                vw = geom.image_to_world(np.asarray([vps[g]], float))[0]
                all_vp_err.append(float(np.linalg.norm(vw - gw)))
                vp_vecs.append((vps[g][0] - gt[0], vps[g][1] - gt[1]))
            cands = dets.get(gframes[i], [])
            if cands:
                cw = min(
                    np.linalg.norm(
                        geom.image_to_world(np.asarray([(c[0], c[1])], float))[0] - gw
                    )
                    for c in cands
                )
                all_ceil_err.append(float(cw))
        print(f"  {d.name}: {len(balls)} GT balls")

    if not all_track_err:
        raise SystemExit("no data")
    print(f"\n=== {len(all_track_err)} GT balls  (gt_override={args.gt_override}) ===")
    print(
        f"  EXISTING TRACKER on AutoCam dets : {_hits(all_track_err, args.radii)}  median={np.median(all_track_err):.1f}m"
    )
    if all_ceil_err:
        print(
            f"  candidate ceiling (best det)     : {_hits(all_ceil_err, args.radii)}  median={np.median(all_ceil_err):.1f}m"
        )
    if all_vp_err:
        print(
            f"  AutoCam viewport (floor)         : {_hits(all_vp_err, args.radii)}  median={np.median(all_vp_err):.1f}m"
        )
        mv = np.mean(vp_vecs, axis=0)
        sv = np.std(vp_vecs, axis=0)
        print(
            f"  viewport-GT vector mean={mv.round(0)} std={sv.round(0)}  "
            f"(large mean vs std => coord offset; ~0 mean + large std => AutoCam looked elsewhere)"
        )
    if vsvp_games:
        print(
            "\n  dense tracker-vs-AutoCam-viewport match per game (normal-play proxy GT):"
        )
        for name, n, hits, med in vsvp_games:
            print(f"    {name[:46]:<46} n={n:>6}  {hits}  median={med:.1f}m")

    # hit-rate by apparent size for the tracker
    sz = np.array(sizes)
    er = np.array(all_track_err)
    print("\n  tracker hit-rate by apparent ball size:")
    for lo, hi in [(0, 6), (6, 8), (8, 12), (12, 100)]:
        m = (sz >= lo) & (sz < hi)
        if m.sum():
            print(
                f"    {lo:>2}-{hi:<3}px n={int(m.sum()):>5}  {_hits(er[m].tolist(), args.radii)}"
            )


if __name__ == "__main__":
    main()
