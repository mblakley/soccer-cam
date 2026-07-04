"""Corpus-level static-distractor mining: cross-frame analysis Mark asked for.

A detection cell that is occupied across a large fraction of the game and NEVER
coincides with a human game-ball label (or the teacher track) is a static distractor
OBJECT — a bench ball, tent, line intersection, corner flag. One pass over the game's
detection stream turns a handful of human clicks into thousands of implied per-frame
negatives, and reduces manual decoy-marking to only MOVING decoys.

Guard rails:
- Restart dwells are naturally excluded: a goal-kick/kickoff ball sits for ~30 s
  (~600 frames of a ~50k-frame half) — far below ``--min-occ``.
- Output clusters are TIERED, not blindly used: ``label_confirmed`` (a human ball label
  exists elsewhere in the game, never in this cell) vs ``teacher_only``. And they are
  NOT automatically detector negatives — a static REAL ball must stay detectable (only
  the selector rejects it); each cluster gets a crop JPEG so a human can class it
  ball / not-ball in one glance per cluster (gallery review), not per frame.

    python -m training.cli.mine_static_distractors \
      --game-dir "F:/Heat_2012s/2026.05.27 - vs Chili Vortex (away)" --crops
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np


def find_static_clusters(
    det_world_by_frame: dict[int, np.ndarray],
    *,
    cell_m: float = 2.0,
    min_occ: float = 0.15,
) -> list[dict]:
    """Cluster world-space detections into ~``cell_m`` cells; return cells occupied in
    >= ``min_occ`` of the frames, sorted by occupancy. Each: {wx, wy, occupancy, n_dets,
    frames} (wx/wy = mean detection position in the cell, meters)."""
    n_frames = len(det_world_by_frame)
    if n_frames == 0:
        return []
    occ_frames: dict[tuple[int, int], set[int]] = {}
    sums: dict[tuple[int, int], np.ndarray] = {}
    counts: dict[tuple[int, int], int] = {}
    for g, w in det_world_by_frame.items():
        cells = set()
        for p in w:
            c = (round(float(p[0]) / cell_m), round(float(p[1]) / cell_m))
            cells.add(c)
            sums[c] = sums.get(c, np.zeros(2)) + p
            counts[c] = counts.get(c, 0) + 1
        for c in cells:
            occ_frames.setdefault(c, set()).add(g)
    out = []
    for c, frames in occ_frames.items():
        occ = len(frames) / n_frames
        if occ >= min_occ:
            mean = sums[c] / counts[c]
            out.append(
                {
                    "wx": round(float(mean[0]), 2),
                    "wy": round(float(mean[1]), 2),
                    "occupancy": round(occ, 3),
                    "n_dets": counts[c],
                    "n_frames": len(frames),
                }
            )
    out.sort(key=lambda r: -r["occupancy"])
    return out


def confirm_clusters(
    clusters: list[dict],
    ball_world: np.ndarray,
    teacher_world: np.ndarray,
    *,
    clear_m: float = 3.0,
) -> list[dict]:
    """Tier clusters by what proves the game ball is never there: human labels
    (``label_confirmed``) plus/or the teacher track (``teacher_only``). A cluster the
    ball DOES visit is dropped (it's a lane of play, not a static object)."""
    out = []
    for cl in clusters:
        p = np.array([cl["wx"], cl["wy"]])
        near_ball = (
            bool((np.linalg.norm(ball_world - p, axis=1) <= clear_m).any())
            if len(ball_world)
            else False
        )
        near_teacher = (
            bool((np.linalg.norm(teacher_world - p, axis=1) <= clear_m).any())
            if len(teacher_world)
            else False
        )
        if near_ball or near_teacher:
            continue  # the game ball passes through here — not a static distractor
        tier = "label_confirmed" if len(ball_world) else "teacher_only"
        out.append({**cl, "confirmed_by": tier})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--game-dir", required=True)
    ap.add_argument(
        "--out", default=None, help="default: <game-dir>/static_distractors.json"
    )
    ap.add_argument("--cell-m", type=float, default=2.0)
    ap.add_argument("--min-occ", type=float, default=0.15)
    ap.add_argument("--clear-m", type=float, default=3.0)
    ap.add_argument("--conf-floor", type=float, default=0.06)
    ap.add_argument(
        "--crops", action="store_true", help="save one review JPEG per cluster"
    )
    ap.add_argument("--no-hwaccel", action="store_true")
    args = ap.parse_args()

    from training.data_prep import distill_dataset as dd
    from training.world_model.geometry import build_field_geometry

    gd = Path(args.game_dir)
    gj = json.loads((gd / "game.json").read_text(encoding="utf-8", errors="ignore"))
    offs = dd.seg_offsets(gj["segments"])
    play = dd.active_play_ranges(gj["segments"], gj.get("game_state"))
    geom = build_field_geometry(
        np.asarray(gj["field_polygon"], float) if gj.get("field_polygon") else None
    )
    if not geom.valid:
        raise SystemExit("no valid field polygon/homography for this game")

    detections = dd.load_detections(gd / "autocam_detections.jsonl", offs)
    in_play = (
        (lambda g: any(lo <= g < hi for lo, hi in play)) if play else (lambda g: True)
    )
    det_world: dict[int, np.ndarray] = {}
    for g, ds in detections.items():
        if not in_play(g):
            continue
        pts = np.asarray(
            [(x, y) for (x, y, c) in ds if c >= args.conf_floor], float
        ).reshape(-1, 2)
        if len(pts):
            det_world[g] = geom.image_to_world(pts)

    hb, _hn = (
        dd.load_human_labels(gd / "ball_labels.jsonl", offs)
        if (gd / "ball_labels.jsonl").exists()
        else ({}, set())
    )
    ball_world = (
        geom.image_to_world(np.asarray(list(hb.values()), float))
        if hb
        else np.zeros((0, 2))
    )
    teacher = dd.teacher_track(
        detections, gj.get("field_polygon"), geom=geom, human_balls=hb
    )
    teacher_world = (
        geom.image_to_world(np.asarray(list(teacher.values()), float))
        if teacher
        else np.zeros((0, 2))
    )

    clusters = find_static_clusters(det_world, cell_m=args.cell_m, min_occ=args.min_occ)
    confirmed = confirm_clusters(
        clusters, ball_world, teacher_world, clear_m=args.clear_m
    )
    for cl in confirmed:
        img = geom.world_to_image(np.asarray([[cl["wx"], cl["wy"]]], float))[0]
        cl["x"], cl["y"] = round(float(img[0]), 1), round(float(img[1]), 1)

    out_path = Path(args.out) if args.out else gd / "static_distractors.json"
    payload = {
        "schema": "static_distractors/1",
        "game_dir": str(gd),
        "params": {
            "cell_m": args.cell_m,
            "min_occ": args.min_occ,
            "clear_m": args.clear_m,
            "conf_floor": args.conf_floor,
        },
        "n_play_frames": len(det_world),
        "n_ball_labels": len(hb),
        "n_teacher_frames": len(teacher),
        "clusters": confirmed,
    }
    out_path.write_text(json.dumps(payload, indent=2))
    print(
        f"{gd.name}: {len(confirmed)} static distractor clusters "
        f"(of {len(clusters)} static cells; {len(hb)} ball labels, "
        f"{len(teacher)} teacher frames) -> {out_path}",
        flush=True,
    )

    if args.crops and confirmed:
        import cv2

        from training.data_prep.segment_decode import iter_frames_from_segments
        from training.data_prep.warped_dataset import resolve_video_rotation

        crops_dir = out_path.parent / "static_distractor_crops"
        crops_dir.mkdir(exist_ok=True)
        mid = sorted(det_world)[len(det_world) // 2]
        vrot = resolve_video_rotation(
            str(gd / (gj.get("combined_video") or "combined.mp4")),
            gj.get("video_rotation"),
        )
        for g, img in iter_frames_from_segments(
            gd, gj["segments"], {mid}, vrot, hwaccel=not args.no_hwaccel
        ):
            for i, cl in enumerate(confirmed):
                x0 = int(np.clip(cl["x"] - 128, 0, img.shape[1] - 256))
                y0 = int(np.clip(cl["y"] - 128, 0, img.shape[0] - 256))
                crop = img[y0 : y0 + 256, x0 : x0 + 256].copy()
                cv2.drawMarker(
                    crop,
                    (int(cl["x"] - x0), int(cl["y"] - y0)),
                    (0, 0, 255),
                    cv2.MARKER_CROSS,
                    40,
                    2,
                )
                cv2.imwrite(
                    str(crops_dir / f"cluster_{i:02d}_occ{cl['occupancy']:.2f}.jpg"),
                    crop,
                )
        print(f"  review crops -> {crops_dir}", flush=True)


if __name__ == "__main__":
    main()
