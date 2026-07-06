r"""Build a far-label review SET for a game — active-learning selection of the *hard* frames.

Consolidates the ad-hoc ``G:\ballresearch\build_*farlabel*`` / ``build_0615_*`` one-off scripts into
ONE committed, parameterized CLI. Selects the frames where AutoCam loses or doubts the ball — which
(per Mark) is usually **occlusion** (ball behind a player), plus far-field and distractor ambiguity —
extracts full-frame strips, and writes the ``manifest.json`` the annotation server + ``far-label.html``
already consume (``D:/training_data/far_label/<set>/{manifest.json, strips/f######.jpg}``). It NEVER
writes ``labels.json`` (server-owned).

Selection criteria (``--criteria``):
  * ``lost``       — no on-field AutoCam detection that frame (ball lost = occlusion / gone-far).
  * ``lowconf``    — top on-field detection confidence in ``[lc_lo, lc_hi)`` (detector unsure).
  * ``distractor`` — 2+ on-field candidates of similar confidence (which one is the game ball?).
  * ``hard``       — the union (default): the frames AutoCam struggles with, the high-value tail.
  * ``near``       — NEAR-band selector supervision (EXP-DIST-29: all existing gold is far-mined,
    and the learned selector demotes near balls). Mines a marathon fullgame dump + its teacher-snap
    labels (``--fullgame-dir`` + ``--sel-labels``) for frames where the teacher's near ball is NOT
    the top-scored candidate (``near_misrank``) or has no teacher coverage (``near_unknown``);
    candidate overlays come straight from the dump (no re-inference).

Frames are restricted to active play (``game_state``), temporally spread into ``--max-frames`` bins
(most-ambiguous per bin, so no clustering), and any frames already in ``--exclude-sets`` are skipped.

    python -m training.cli.build_far_label_queue \
        --game-dir "F:/Heat_2012s/2026.06.07 - vs Lakefront SC (home)" \
        --criteria hard --max-frames 160
    # add --analyze to print the selection stats + yields WITHOUT decoding/writing.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from training.data_prep import distill_dataset as dd


def _in_field(poly_np: np.ndarray, x: float, y: float, margin: float) -> bool:
    import cv2

    return cv2.pointPolygonTest(poly_np, (float(x), float(y)), True) >= margin


def _spread_bins(pool: list[list], target: int) -> list[list]:
    """Temporally spread ``pool`` rows ``[frame, ..., value]`` into ``target`` bins,
    keeping the highest-value row per bin (no clustering)."""
    if not pool:
        return []
    pool.sort(key=lambda r: r[0])
    fmin, fmax = pool[0][0], pool[-1][0]
    span = max(1, fmax - fmin)
    bins: dict[int, list] = {}
    for c in pool:
        b = int((c[0] - fmin) / span * (target - 1))
        if b not in bins or c[-1] > bins[b][-1]:
            bins[b] = c
    return sorted(bins.values(), key=lambda r: r[0])


def select_near_frames(
    ef: list[int],
    cands: dict[int, list],
    labels: dict[int, tuple[int, float]],
    geom,
    *,
    near_px: float,
    target: int,
    exclude: set[int],
) -> list[dict]:
    """NEAR-band miner over a fullgame dump + teacher-snap ``labels`` ({ef_index:
    (cand_idx, weight)}, -1 = none). ``near_misrank`` (teacher's near ball outscored by
    another candidate — the demotions the learned selector copies, value = score rank)
    beats ``near_unknown`` (near candidates but no teacher coverage, value 0.5) in a bin.
    """
    pool: list[list] = []  # [frame, reason, (hx, hy), conf, value]
    for i, g in enumerate(ef):
        if g in exclude:
            continue
        rows = cands.get(g) or []
        if not rows:
            continue
        xy = np.asarray([(r[0], r[1]) for r in rows], float)
        exp = geom.expected_ball_diameter_px(xy)
        lab = labels.get(i)
        if lab is not None:
            j = int(lab[0])
            if j < 0 or exp[j] <= near_px:  # none-frame or teacher ball not near
                continue
            scores = [float(r[2]) for r in rows]
            rank = sum(1 for s in scores if s > scores[j])
            if rank == 0:  # score already ranks it #1 — low labeling value
                continue
            pool.append(
                [
                    g,
                    "near_misrank",
                    (float(rows[j][0]), float(rows[j][1])),
                    scores[j],
                    float(rank),
                ]
            )
        else:
            nj = [k for k in range(len(rows)) if exp[k] > near_px]
            if not nj:
                continue
            top = max(nj, key=lambda k: float(rows[k][2]))
            pool.append(
                [
                    g,
                    "near_unknown",
                    (float(rows[top][0]), float(rows[top][1])),
                    float(rows[top][2]),
                    0.5,
                ]
            )
    chosen = _spread_bins(pool, target)
    return [
        {
            "frame_idx": int(f),
            "file": f"f{int(f):06d}.jpg",
            "hint_x": round(hx, 1),
            "hint_y": round(hy, 1),
            "autocam": reason == "near_misrank",  # teacher-backed hint
            "hint_conf": round(conf, 3),
            "reason": reason,
        }
        for (f, reason, (hx, hy), conf, _v) in chosen
    ]


def select_frames(
    dets: dict[int, list],
    poly_np: np.ndarray,
    in_active,
    *,
    criteria: str,
    lc_lo: float,
    lc_hi: float,
    dist_ratio: float,
    target: int,
    exclude: set[int],
    margin: float,
) -> list[dict]:
    """Return the chosen frames as manifest ``frames[]`` dicts, temporally spread.

    ``dets``: ``{global_frame: [(x, y, conf), ...]}`` (any order). ``in_active(f)`` -> bool.
    """
    pool: list[list] = []  # [frame, reason, (hx, hy), conf, score, autocam]
    for f in sorted(dets):
        if f in exclude or not in_active(f):
            continue
        onfield = [c for c in dets[f] if _in_field(poly_np, c[0], c[1], margin)]
        if not onfield:
            if criteria in ("lost", "hard"):
                # ball lost this frame — the human finds it (or marks occluded/out); center hint.
                pool.append([f, "lost", (3840.0, 1080.0), 0.0, 1.0, False])
            continue
        onfield.sort(key=lambda c: -float(c[2]))
        top = onfield[0]
        conf = float(top[2])
        reason, score = None, 0.0
        if criteria in ("lowconf", "hard") and lc_lo <= conf < lc_hi:
            reason, score = "lowconf_visible", (lc_hi - conf)
        elif (
            criteria in ("distractor", "hard")
            and len(onfield) >= 2
            and float(onfield[1][2]) >= dist_ratio * conf
            and conf >= lc_hi
        ):
            reason, score = "distractor", float(onfield[1][2]) / max(conf, 1e-6)
        if reason:
            pool.append([f, reason, (float(top[0]), float(top[1])), conf, score, True])
    # value is the LAST column for _spread_bins
    chosen = _spread_bins(
        [[f, r, xy, c, ac, s] for (f, r, xy, c, s, ac) in pool], target
    )
    return [
        {
            "frame_idx": int(f),
            "file": f"f{int(f):06d}.jpg",
            "hint_x": round(hx, 1),
            "hint_y": round(hy, 1),
            "autocam": bool(ac),
            "hint_conf": round(conf, 3),
            "reason": reason,
        }
        for (f, reason, (hx, hy), conf, ac, _score) in chosen
    ]


def _load_exclusions(out_dir: Path, sets: list[str]) -> set[int]:
    excl: set[int] = set()
    for s in sets:
        m = out_dir / s / "manifest.json"
        if m.exists():
            try:
                excl |= {
                    int(e["frame_idx"])
                    for e in json.loads(m.read_text()).get("frames", [])
                }
            except Exception:  # noqa: BLE001
                pass
    return excl


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--game-dir", required=True)
    ap.add_argument("--out", default="D:/training_data/far_label")
    ap.add_argument("--set-name", default=None, help="default: <game_id>__<criteria>")
    ap.add_argument(
        "--criteria",
        choices=["hard", "lowconf", "lost", "distractor", "near"],
        default="hard",
    )
    ap.add_argument("--lc-lo", type=float, default=0.05)
    ap.add_argument("--lc-hi", type=float, default=0.25)
    ap.add_argument("--dist-ratio", type=float, default=0.7)
    ap.add_argument("--max-frames", type=int, default=160)
    ap.add_argument(
        "--margin", type=float, default=-40.0, help="in-field test margin (px)"
    )
    ap.add_argument("--exclude-sets", nargs="*", default=[])
    ap.add_argument("--no-hwaccel", action="store_true")
    ap.add_argument(
        "--analyze", action="store_true", help="print stats, write nothing, no decode"
    )
    # near-mode inputs (marathon artifact + its teacher-snap supervision)
    ap.add_argument("--fullgame-dir", default=None)
    ap.add_argument("--sel-labels", default=None)
    ap.add_argument(
        "--near-px",
        type=float,
        default=8.0,
        help="near band = expected diameter > this (eval dumps' far_size_px)",
    )
    ap.add_argument(
        "--priority", type=int, default=None, help="landing-page sort order (1 = top)"
    )
    args = ap.parse_args()

    vdir = Path(args.game_dir)
    gj = json.loads((vdir / "game.json").read_text(encoding="utf-8", errors="ignore"))
    gid = gj["game_id"]
    poly = gj["field_polygon"]
    poly_np = np.array(poly, np.float32)
    offs = dd.seg_offsets(gj["segments"])
    dets = dd.load_detections(vdir / "autocam_detections.jsonl", offs)

    ranges = dd.active_play_ranges(gj["segments"], gj.get("game_state"))
    if ranges:

        def in_active(f: int) -> bool:
            return any(lo <= f < hi for lo, hi in ranges)
    else:
        print("[warn] no game_state active-play ranges — using ALL frames", flush=True)

        def in_active(f: int) -> bool:  # noqa: ARG001
            return True

    excl = _load_exclusions(Path(args.out), args.exclude_sets)
    near_cands: dict[int, list] = {}
    if args.criteria == "near":
        if not (args.fullgame_dir and args.sel_labels):
            raise SystemExit("--criteria near needs --fullgame-dir and --sel-labels")
        from training.cli.build_selector_labels import load_fullgame_candidates
        from training.world_model.geometry import build_field_geometry

        ef, near_cands, fg_meta = load_fullgame_candidates(Path(args.fullgame_dir))
        geom = build_field_geometry(np.asarray(poly, float))
        if not geom.valid:
            raise SystemExit("field polygon does not fit a valid homography")
        sel = json.loads(Path(args.sel_labels).read_text(encoding="utf-8"))["labels"]
        labels = {int(k): (int(v[0]), float(v[1])) for k, v in sel.items()}
        # never re-ask for a frame a human already labeled (any set grid, ±4)
        if (vdir / "ball_labels.jsonl").exists():
            hb, hn = dd.load_human_labels(vdir / "ball_labels.jsonl", offs)
            for g in list(hb) + list(hn):
                excl.update(range(g - 4, g + 5))
        frames = select_near_frames(
            ef,
            near_cands,
            labels,
            geom,
            near_px=args.near_px,
            target=args.max_frames,
            exclude=excl,
        )
    else:
        frames = select_frames(
            dets,
            poly_np,
            in_active,
            criteria=args.criteria,
            lc_lo=args.lc_lo,
            lc_hi=args.lc_hi,
            dist_ratio=args.dist_ratio,
            target=args.max_frames,
            exclude=excl,
            margin=args.margin,
        )
    reasons: dict[str, int] = {}
    for e in frames:
        reasons[e["reason"]] = reasons.get(e["reason"], 0) + 1
    print(
        f"[select] {gid}: {len(dets)} det-frames, exclude {len(excl)} -> "
        f"{len(frames)} chosen (criteria={args.criteria}); reasons={reasons}",
        flush=True,
    )
    if not frames:
        raise SystemExit("no frames selected")
    if args.analyze:
        return

    # --- resolve the combined video (same axis as the detections/global frames) ---
    video = gj.get("combined_video")
    if not video or not Path(video).exists():
        cands = list(vdir.glob("combined*.mp4")) or list(vdir.glob("*-raw.mp4"))
        if not cands:
            raise SystemExit(f"no video in {vdir}")
        video = str(cands[0])

    # Skip stub folders: a real game's combined video is multiple GB, but each game day tends to
    # have a tiny "house / camera-test" recording in a sibling folder (tens of MB) that otherwise
    # becomes a garbage all-gap label set. Don't build a set from non-game footage.
    MIN_GAME_BYTES = 500 * 1024 * 1024
    vbytes = Path(video).stat().st_size
    if vbytes < MIN_GAME_BYTES:
        print(
            f"SKIP {vdir.name}: video {vbytes // (1024 * 1024)}MB < 500MB — "
            "stub/non-game footage, not building a label set",
            flush=True,
        )
        return

    set_name = args.set_name or f"{gid}__{args.criteria}"
    out = Path(args.out) / set_name
    strips = out / "strips"
    strips.mkdir(parents=True, exist_ok=True)
    for old in [*strips.glob("*.jpg"), *strips.glob("*.png")]:
        old.unlink()

    import cv2

    from training.data_prep.segment_decode import iter_frames_from_segments
    from training.data_prep.warped_dataset import resolve_video_rotation

    seg0 = gj["segments"][0]
    sw, sh = int(seg0["w"]), int(seg0["h"])
    vrot = resolve_video_rotation(video, gj.get("video_rotation"))

    # STREAM the wanted global frames from the RAW per-segment clips, NOT the re-encoded/VFR/
    # corruption-prone combined video. The combined is a stream-copy concat, so raw-segment frame f
    # is bit-identical to combined global (offset+f); decoding raw is frame-exact, corruption-isolated
    # (a bad segment loses only its own frames), and fast (a keyframe seek + short decode per label).
    # Each JPEG is written as its frame arrives — never hold the full-res frame set in memory.
    want = {int(e["frame_idx"]) for e in frames}
    written = 0
    for f, img in iter_frames_from_segments(
        vdir, gj["segments"], want, vrot, hwaccel=not args.no_hwaccel
    ):
        # lossless PNG on disk; manifest keeps .jpg names — the deployed server serves
        # the .png when present (same layout the PNG regen left on every existing set)
        cv2.imwrite(str(strips / f"f{f:06d}.png"), img)
        written += 1
    print(
        f"  wrote {written}/{len(want)} strips via raw-segment decode "
        f"({len(want) - written} unavailable — corrupt/missing segments)",
        flush=True,
    )
    if written == 0:
        raise SystemExit(
            f"no decodable strips for {gid} (video corrupt from the start)"
        )

    kept = [e for e in frames if (strips / e["file"]).with_suffix(".png").exists()]
    for e in kept:
        e.update(
            {
                "crop_x0": 0,
                "crop_y0": 0,
                "crop_w": int(sw),
                "crop_h": int(sh),
                "band": "normal",
            }
        )
    if args.criteria == "near":
        # candidate overlays straight from the dump (score-sorted; top-5 render blue)
        for e in kept:
            rows = (near_cands.get(int(e["frame_idx"])) or [])[:12]
            e["context"] = [
                {
                    "x": round(float(r[0]), 1),
                    "y": round(float(r[1]), 1),
                    "df": -1 if k < 5 else 1,
                }
                for k, r in enumerate(rows)
            ]
            e["candidates"] = [
                [round(float(r[0]), 1), round(float(r[1]), 1), round(float(r[2]), 4)]
                for r in rows
            ]
    manifest = {
        "set": set_name,
        "clip": video,
        "fps": float(gj.get("fps", 20.0)),
        "src_w": int(sw),
        "src_h": int(sh),
        "strip_y0": 0,
        "strip_y1": int(sh),
        "crop_w": int(sw),
        "full_frame": True,
        "criteria": args.criteria,
        "polygon": poly,
        "n_frames": len(kept),
        "n_autocam": sum(1 for e in kept if e["autocam"]),
        "frames": kept,
    }
    if args.priority is not None:
        manifest["priority"] = int(args.priority)
    if args.criteria == "near":
        manifest["candidates_ckpt"] = fg_meta.get("ckpt", "")
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(
        f"WROTE {out}/manifest.json: {len(kept)} frames, {manifest['n_autocam']} AutoCam-seeded "
        f"(reasons={reasons})",
        flush=True,
    )


if __name__ == "__main__":
    main()
