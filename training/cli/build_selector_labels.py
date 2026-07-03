"""Selection-level distillation labels: which of OUR candidates is the game ball, per frame.

The prior distillation trained the DETECTOR on teacher positions — the component that was
already good enough. This builds supervision for the SELECTOR instead: run the validated
teacher (AutoCam detections -> our ``track_ball``, human labels anchoring/overriding) over a
game, then snap each teacher position onto OUR detector's candidate dump. Output, per dump
eval-frame: "candidate j is the game ball" / "none visible" / (skip = can't supervise).

Noise handling (teacher errors cluster at its track-loss events):
- frames within ``--stability-k`` dump steps of a teacher DISCONTINUITY (coverage gap or a
  world-space jump) are dropped;
- human ``ball`` frames get ``--gold-weight``; human ``not_visible`` frames label "none";
- frames where the teacher exists but no candidate is within ``--snap-m`` are SKIPPED
  (our detector missed — that's a ceiling problem, not selection supervision);
- active-play gating via game.json ``game_state`` (warm-up/halftime teacher noise).

    python -m training.cli.build_selector_labels \
      --dump G:/ballresearch/selector/cands_cleveland_hn2.pkl \
      --game-dir "F:/Flash_2013s/2026.05.09 - vs Cleveland Force SC White (home)" \
      --out G:/ballresearch/selector/sel_labels_cleveland.json
"""

from __future__ import annotations

import argparse
import json
import pickle
from pathlib import Path

import numpy as np


def snap_teacher_to_candidates(
    ef: list[int],
    cands: dict[int, list[tuple]],
    teacher: dict[int, tuple[float, float]],
    human_balls: dict[int, tuple[float, float]],
    human_novis: set[int],
    geom,
    play_ranges: list[tuple[int, int]],
    *,
    snap_m: float = 2.0,
    stability_k: int = 3,
    jump_m_per_frame: float = 12.0,
    gap_frames: int = 12,
    gold_weight: float = 20.0,
) -> tuple[dict[int, tuple[int, float]], dict]:
    """Return ``({ef_index: (cand_index | -1 for none, weight)}, stats)``.

    Frames not in the mapping carry NO supervision (unknown). Discontinuity = teacher
    coverage gap > ``gap_frames`` source frames, or world jump > ``jump_m_per_frame`` *
    frame-gap between consecutive teacher frames; labels within ``stability_k`` dump
    steps of one are dropped (unless human-anchored — gold is exempt).
    """
    stats = {
        "ball": 0,
        "none": 0,
        "gold": 0,
        "skip_unstable": 0,
        "skip_missed": 0,
        "skip_nocover": 0,
        "skip_outofplay": 0,
    }
    in_play = (
        (lambda g: any(lo <= g < hi for lo, hi in play_ranges))
        if play_ranges
        else (lambda g: True)
    )

    # teacher discontinuities in GLOBAL frame space
    tg = sorted(set(teacher))
    tw = (
        geom.image_to_world(np.asarray([teacher[g] for g in tg], float))
        if tg
        else np.zeros((0, 2))
    )
    bad_globals: set[int] = set()
    for i in range(1, len(tg)):
        dg = tg[i] - tg[i - 1]
        jump = float(np.linalg.norm(tw[i] - tw[i - 1]))
        if dg > gap_frames or jump > jump_m_per_frame * dg:
            bad_globals.update((tg[i - 1], tg[i]))
    stride = int(np.median(np.diff(ef))) if len(ef) > 1 else 1
    pad = stability_k * stride

    out: dict[int, tuple[int, float]] = {}
    for i, g in enumerate(ef):
        if not in_play(g):
            stats["skip_outofplay"] += 1
            continue
        gold = g in human_balls
        if g in human_novis:
            out[i] = (-1, gold_weight)
            stats["none"] += 1
            continue
        if g not in teacher:
            stats["skip_nocover"] += 1
            continue
        if not gold and any(abs(g - b) <= pad for b in bad_globals):
            stats["skip_unstable"] += 1
            continue
        cs = cands.get(g, [])
        if not cs:
            stats["skip_missed"] += 1
            continue
        txy = geom.image_to_world(np.asarray([teacher[g]], float))[0]
        cw = geom.image_to_world(np.asarray([(c[0], c[1]) for c in cs], float))
        d = np.linalg.norm(cw - txy, axis=1)
        j = int(np.argmin(d))
        if float(d[j]) > snap_m:
            stats["skip_missed"] += 1
            continue
        out[i] = (j, gold_weight if gold else 1.0)
        stats["ball"] += 1
        if gold:
            stats["gold"] += 1
    return out, stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", required=True, help="eval_detector --dump-cands pickle")
    ap.add_argument("--game-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--snap-m", type=float, default=2.0)
    ap.add_argument("--stability-k", type=int, default=3)
    ap.add_argument("--jump-m-per-frame", type=float, default=12.0)
    ap.add_argument("--gap-frames", type=int, default=12)
    ap.add_argument("--gold-weight", type=float, default=20.0)
    ap.add_argument("--conf-floor", type=float, default=0.06)
    args = ap.parse_args()

    from training.data_prep import distill_dataset as dd
    from training.world_model.geometry import build_field_geometry

    gd = Path(args.game_dir)
    gj = json.loads((gd / "game.json").read_text(encoding="utf-8", errors="ignore"))
    offs = dd.seg_offsets(gj["segments"])
    play = dd.active_play_ranges(gj["segments"], gj.get("game_state"))

    with open(args.dump, "rb") as fh:
        d = pickle.load(fh)
    geom = build_field_geometry(np.asarray(d["polygon"], float))
    if not geom.valid:
        raise SystemExit("dump polygon does not fit a valid homography")

    detections = dd.load_detections(gd / "autocam_detections.jsonl", offs)
    hb, hn = (
        dd.load_human_labels(gd / "ball_labels.jsonl", offs)
        if (gd / "ball_labels.jsonl").exists()
        else ({}, set())
    )
    teacher = dd.teacher_track(
        detections,
        d["polygon"],
        geom=geom,
        human_balls=hb,
        human_novis=hn,
        conf_floor=args.conf_floor,
    )
    labels, stats = snap_teacher_to_candidates(
        d["ef"],
        d["cands"],
        teacher,
        hb,
        hn,
        geom,
        play,
        snap_m=args.snap_m,
        stability_k=args.stability_k,
        jump_m_per_frame=args.jump_m_per_frame,
        gap_frames=args.gap_frames,
        gold_weight=args.gold_weight,
    )
    payload = {
        "schema": "selector_labels/1",
        "dump": str(args.dump),
        "game_dir": str(gd),
        "params": {
            "snap_m": args.snap_m,
            "stability_k": args.stability_k,
            "jump_m_per_frame": args.jump_m_per_frame,
            "gap_frames": args.gap_frames,
            "gold_weight": args.gold_weight,
            "conf_floor": args.conf_floor,
        },
        "stats": stats,
        "labels": {str(i): [c, w] for i, (c, w) in sorted(labels.items())},
    }
    Path(args.out).write_text(json.dumps(payload))
    print(
        f"{gd.name}: teacher {len(teacher)} frames -> labels {len(labels)} "
        f"({stats['ball']} ball / {stats['none']} none / {stats['gold']} gold; "
        f"skipped unstable {stats['skip_unstable']}, detector-missed {stats['skip_missed']}, "
        f"no-teacher {stats['skip_nocover']}, out-of-play {stats['skip_outofplay']})",
        flush=True,
    )


if __name__ == "__main__":
    main()
