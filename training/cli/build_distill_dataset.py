"""Build the distillation crop store for ``train_v4_heatmap``.

Per game: the teacher track (existing tracker over AutoCam detections + human GT override, snapped to
the real detection) → dewarped 3-frame band crops + Gaussian **ball** targets, via the reused
``build_distill_games`` → ``build_heatmap_crops`` path. Hold-out (eval) games are excluded from the
store entirely; ``--val`` games go to the val split for checkpoint selection. Run on the box (F:
detections/videos are local)::

    python -m training.cli.build_distill_dataset --roots F:/Flash_2013s F:/Heat_2012s \
        --holdout heat__2026.05.31_vs_Spencerport_gold_2_away \
        --val flash__2026.05.09_vs_Cleveland_Force_SC_White_home \
        --out G:/ballresearch/distill/crops_v2 --max-games 12
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from training.data_prep import distill_dataset as dd


def game_config(vdir: Path) -> dict | None:
    """Build a ``build_distill_games`` config from a game's F: video dir, or None if unusable."""
    try:
        gj = json.loads(
            (vdir / "game.json").read_text(encoding="utf-8", errors="ignore")
        )
    except Exception:  # noqa: BLE001
        return None
    if not gj.get("field_polygon") or not (vdir / "autocam_detections.jsonl").exists():
        return None
    video = gj.get("combined_video")
    if not video or not Path(video).exists():
        video = None
        for cand in ("combined.mp4", "combined_rotated.mp4"):
            if (vdir / cand).exists():
                video = str(vdir / cand)
                break
        if not video:
            raws = sorted(vdir.glob("*-raw.mp4"))
            video = str(raws[0]) if raws else None
    if not video:
        return None
    gc = {
        "game_id": gj["game_id"],
        "video": video,
        "segments": gj["segments"],
        "polygon": gj["field_polygon"],
        "detections": str(vdir / "autocam_detections.jsonl"),
        "camera": gj.get("camera", "?"),
        "team": gj.get("team", "?"),
        "video_rotation": gj.get("video_rotation", 0),
        "game_state": gj.get("game_state"),
    }
    if (vdir / "ball_labels.jsonl").exists():
        gc["human_labels"] = str(vdir / "ball_labels.jsonl")
    return gc


def find_configs(roots: list[str]) -> list[dict]:
    by_id: dict[str, dict] = {}
    for r in roots:
        for gj in Path(r).glob("**/game.json"):
            c = game_config(gj.parent)
            if c and c["game_id"] not in by_id:
                by_id[c["game_id"]] = c
    return list(by_id.values())


def balance(cfgs: list[dict], n: int) -> list[dict]:
    """Interleave by camera so a capped set stays mixed Dahua/Reolink, GT-labeled games first."""
    cfgs = sorted(cfgs, key=lambda c: (("human_labels" not in c), c["game_id"]))
    cams: dict[str, list[dict]] = {}
    for c in cfgs:
        cams.setdefault(c["camera"], []).append(c)
    out, order = [], list(cams)
    while len(out) < n and any(cams[k] for k in order):
        for k in order:
            if cams[k]:
                out.append(cams[k].pop(0))
                if len(out) >= n:
                    break
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--roots", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--holdout",
        nargs="*",
        default=[],
        help="game_ids excluded from the store (eval)",
    )
    ap.add_argument("--val", nargs="*", default=[], help="game_ids for the val split")
    ap.add_argument("--max-games", type=int, default=None)
    ap.add_argument(
        "--camera",
        default=None,
        help="restrict to one camera (e.g. reolink) — Reolink is the clean primary",
    )
    ap.add_argument(
        "--games",
        nargs="+",
        default=None,
        help="restrict to exactly these game_ids (twin builds: replicate a "
        "reference store's game set even after the registry has grown)",
    )
    ap.add_argument(
        "--base-stride",
        type=int,
        default=4,
        help="thin the (already stride-4) teacher frames by this index step (16 ~ 1.5 Hz)",
    )
    ap.add_argument(
        "--max-per-game",
        type=int,
        default=None,
        help="hard cap on auto labels/game (uniform); bounds crop count for a fast first build",
    )
    ap.add_argument("--crop", type=int, default=256)
    ap.add_argument("--sigma", type=float, default=4.0)
    ap.add_argument("--target-width", type=int, default=None)
    ap.add_argument(
        "--normalize",
        action="store_true",
        help="per-camera target_width for a consistent ~8px ball across cameras "
        "(measured: reolink ~5120, dahua ~3900) — use for the mixed all-games build",
    )
    ap.add_argument(
        "--no-hwaccel",
        action="store_true",
        help="disable NVDEC hardware decode (default: on; ~3.3x faster on this box)",
    )
    ap.add_argument(
        "--stabilize",
        action="store_true",
        help="wind-align every band to the game's first frame before cropping "
        "(BandStabilizer; labels corrected by the per-frame shift — EXP-DIST-57)",
    )
    args = ap.parse_args()

    holdout, val = set(args.holdout), set(args.val)
    cfgs = [c for c in find_configs(args.roots) if c["game_id"] not in holdout]
    if args.camera:
        cfgs = [c for c in cfgs if c.get("camera") == args.camera]
    if args.games:
        keep = set(args.games)
        cfgs = [c for c in cfgs if c["game_id"] in keep]
        missing = keep - {c["game_id"] for c in cfgs}
        if missing:
            raise SystemExit(f"--games not found in registry: {sorted(missing)}")
    if args.normalize:
        by_cam = {"reolink": 5120, "dahua": 3900}
        for c in cfgs:
            c["target_width"] = by_cam.get(c.get("camera"))
    if args.max_games:
        cfgs = balance(cfgs, args.max_games)
    for c in cfgs:
        c["split"] = "val" if c["game_id"] in val else "train"
    print(
        f"{len(cfgs)} games (holdout {len(holdout)}, val {len(val & {c['game_id'] for c in cfgs})}):",
        flush=True,
    )

    games = dd.build_distill_games(
        cfgs,
        base_stride=args.base_stride,
        max_per_game=args.max_per_game,
        report=True,
    )
    if not games:
        raise SystemExit("no games produced labels")
    val_ids = {g["game_id"] for g in games if g.get("split") == "val"}

    from training.data_prep.heatmap_dataset import build_heatmap_crops

    summary = build_heatmap_crops(
        games,
        args.out,
        crop=args.crop,
        sigma=args.sigma,
        val_game_ids=val_ids,
        target_width=args.target_width,
        hwaccel=not args.no_hwaccel,
        stabilize=args.stabilize,
    )
    print("\nDATASET:", summary, flush=True)


if __name__ == "__main__":
    main()
