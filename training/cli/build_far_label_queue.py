r"""Build a far-label review SET for a game — active-learning selection of the *hard* frames.

Consolidates the ad-hoc ``build_*farlabel*`` / ``build_0615_*`` one-off scripts into ONE committed,
parameterized CLI. Selects the frames where AutoCam loses or doubts the ball — which is usually
**occlusion** (the ball behind a player), plus far-field and distractor ambiguity — extracts
full-frame strips, and writes the ``manifest.json`` the annotation server + ``far-label.html`` consume
(``D:/training_data/far_label/<set>/{manifest.json, strips/f######.png}``, lossless). It NEVER writes
``labels.json`` (server-owned). Self-contained: no dependency outside ``av`` / ``cv2`` / ``numpy``.

Selection criteria (``--criteria``):
  * ``lost``       — no on-field AutoCam detection that frame (ball lost = occlusion / gone far).
  * ``lowconf``    — top on-field detection confidence in ``[lc_lo, lc_hi)`` (detector unsure).
  * ``distractor`` — 2+ on-field candidates of similar confidence (which one is the game ball?).
  * ``hard``       — the union (default): the frames AutoCam struggles with, the high-value tail.

Frames are restricted to active play (``game_state``), temporally spread into ``--max-frames`` bins
(most-ambiguous per bin, so no clustering), and any frames already in ``--exclude-sets`` are skipped.

    python -m training.cli.build_far_label_queue \
        --game-dir "F:/Heat_2012s/2026.06.07 - vs Lakefront SC (home)" \
        --criteria hard --max-frames 160
    # --analyze prints the selection stats/yields WITHOUT decoding or writing.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

import numpy as np


# --------------------------------------------------------------------------- #
# Canonical per-game sidecar helpers (inlined so this tool is self-contained). #
# --------------------------------------------------------------------------- #
def seg_offsets(segments: list[dict]) -> dict[str, int]:
    """``{segment_name: global_offset}`` from ``game.json`` ``segments`` (global = offset + f)."""
    return {s["seg"]: int(s["global_offset"]) for s in segments}


def active_play_ranges(segments, game_state) -> list[tuple[int, int]]:
    """Global-frame ``[(lo, hi), ...]`` for the halves (``first_half``/``second_half``) from
    ``game.json`` ``game_state`` — drops warm-up / halftime / pre- & post-game. ``[]`` if unknown."""
    offs = seg_offsets(segments)
    out: list[tuple[int, int]] = []
    for ph in game_state or []:
        if ph.get("phase") in ("first_half", "second_half"):
            s, e = ph.get("start"), ph.get("end")
            if s and e and s[0] in offs and e[0] in offs:
                out.append((offs[s[0]] + int(s[1]), offs[e[0]] + int(e[1])))
    return out


def load_detections(jsonl_path, offsets: dict[str, int]) -> dict[int, list[tuple]]:
    """Parse ``autocam_detections.jsonl`` -> ``{global_frame: [(x, y, conf), ...]}`` (conf-desc)."""
    out: dict[int, list[tuple]] = defaultdict(list)
    with open(jsonl_path) as fh:
        for line in fh:
            line = line.strip()
            if not line or '"_meta"' in line[:12]:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "x" not in o or "y" not in o or o.get("seg") not in offsets:
                continue
            g = offsets[o["seg"]] + int(o["f"])
            out[g].append((float(o["x"]), float(o["y"]), float(o.get("conf", 0.0))))
    for g in out:
        out[g].sort(key=lambda c: c[2], reverse=True)
    return dict(out)


def resolve_video_rotation(video_path, explicit=None) -> int:
    """Display-rotation for a video: ``explicit`` if set, else ``game.json`` ``video_rotation``, else 0."""
    if explicit:
        return int(explicit)
    try:
        gj = Path(video_path).parent / "game.json"
        if gj.exists():
            return int(json.loads(gj.read_text()).get("video_rotation", 0) or 0)
    except Exception:  # noqa: BLE001
        pass
    return 0


def apply_display_rotation(img, video_rotation):
    """PyAV ignores the container display-matrix; flip 180 for the upside-down-mounted games."""
    if video_rotation in (180, -180):
        import cv2

        return cv2.flip(img, -1)
    return img


# --------------------------------------------------------------------------- #
# Frame selection                                                             #
# --------------------------------------------------------------------------- #
def _in_field(poly_np: np.ndarray, x: float, y: float, margin: float) -> bool:
    import cv2

    return cv2.pointPolygonTest(poly_np, (float(x), float(y)), True) >= margin


# frame offsets whose AutoCam ball position is shown to the human as ghost context — so an occluded
# ball can be placed by interpolating the path just before/after it (the tracker's job, done by eye).
_CTX_OFFSETS = (-24, -16, -8, -4, 4, 8, 16, 24)


def _context(dets, poly_np, margin, f: int, stride: int) -> list[dict]:
    ctx = []
    for d in _CTX_OFFSETS:
        g = f + d * stride
        for c in dets.get(g, ()):
            if _in_field(poly_np, c[0], c[1], margin):
                ctx.append(
                    {
                        "df": d,
                        "x": round(float(c[0]), 1),
                        "y": round(float(c[1]), 1),
                        "conf": round(float(c[2]), 3),
                    }
                )
                break
    return ctx


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
    stride: int,
) -> list[dict]:
    """Return chosen frames as manifest ``frames[]`` dicts, temporally spread over ``target`` bins."""
    pool: list[list] = []  # [frame, reason, (hx, hy), conf, score, autocam]
    for f in sorted(dets):
        if f in exclude or not in_active(f):
            continue
        onfield = [c for c in dets[f] if _in_field(poly_np, c[0], c[1], margin)]
        if not onfield:
            if criteria in ("lost", "hard"):
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
    if not pool:
        return []
    fmin, fmax = pool[0][0], pool[-1][0]
    span = max(1, fmax - fmin)
    bins: dict[int, list] = {}
    for c in pool:
        b = int((c[0] - fmin) / span * (target - 1))
        if b not in bins or c[4] > bins[b][4]:
            bins[b] = c
    chosen = sorted(bins.values(), key=lambda r: r[0])
    return [
        {
            "frame_idx": int(f),
            "file": f"f{int(f):06d}.png",
            "hint_x": round(hx, 1),
            "hint_y": round(hy, 1),
            "autocam": bool(ac),
            "hint_conf": round(conf, 3),
            "reason": reason,
            "context": _context(dets, poly_np, margin, f, stride),
        }
        for (f, reason, (hx, hy), conf, _score, ac) in chosen
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
        "--criteria", choices=["hard", "lowconf", "lost", "distractor"], default="hard"
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
    args = ap.parse_args()

    vdir = Path(args.game_dir)
    gj = json.loads((vdir / "game.json").read_text(encoding="utf-8", errors="ignore"))
    gid = gj["game_id"]
    poly = gj["field_polygon"]
    poly_np = np.array(poly, np.float32)
    offs = seg_offsets(gj["segments"])
    dets = load_detections(vdir / "autocam_detections.jsonl", offs)
    _df = sorted(dets)
    _gaps = [_df[i] - _df[i - 1] for i in range(1, len(_df))]
    stride = (
        int(np.median(_gaps)) if _gaps else 1
    )  # detection cadence (for context offsets)

    ranges = active_play_ranges(gj["segments"], gj.get("game_state"))
    if ranges:

        def in_active(f: int) -> bool:
            return any(lo <= f < hi for lo, hi in ranges)
    else:
        print("[warn] no game_state active-play ranges — using ALL frames", flush=True)

        def in_active(f: int) -> bool:  # noqa: ARG001
            return True

    excl = _load_exclusions(Path(args.out), args.exclude_sets)
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
        stride=stride,
    )
    reasons: dict[str, int] = {}
    for e in frames:
        reasons[e["reason"]] = reasons.get(e["reason"], 0) + 1
    print(
        f"[select] {gid}: {len(dets)} det-frames, exclude {len(excl)} -> {len(frames)} chosen "
        f"(criteria={args.criteria}); reasons={reasons}",
        flush=True,
    )
    if not frames:
        raise SystemExit("no frames selected")
    if args.analyze:
        return

    video = gj.get("combined_video")
    if not video or not Path(video).exists():
        cands = list(vdir.glob("combined*.mp4")) or list(vdir.glob("*-raw.mp4"))
        if not cands:
            raise SystemExit(f"no video in {vdir}")
        video = str(cands[0])

    set_name = args.set_name or f"{gid}__{args.criteria}"
    out = Path(args.out) / set_name
    strips = out / "strips"
    strips.mkdir(parents=True, exist_ok=True)
    for old in list(strips.glob("*.jpg")) + list(strips.glob("*.png")):
        old.unlink()

    import av
    import cv2

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

    # SEQUENTIAL decode (combined video is re-encoded — seeking silently misaligns).
    want = {e["frame_idx"] for e in frames}
    hi = max(want)
    written = 0
    idx = -1
    for fr in container.decode(stream):
        idx += 1
        if idx in want:
            img = apply_display_rotation(fr.to_ndarray(format="bgr24"), vrot)
            # PNG = LOSSLESS: JPEG 4:2:0 chroma subsampling visibly smears a 3-8 px far ball
            cv2.imwrite(
                str(strips / f"f{idx:06d}.png"), img, [cv2.IMWRITE_PNG_COMPRESSION, 3]
            )
            written += 1
            if written % 20 == 0:
                print(f"  wrote {written}/{len(want)} strips (frame {idx})", flush=True)
        if idx >= hi:
            break
    container.close()

    kept = [e for e in frames if (strips / e["file"]).exists()]
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
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(
        f"WROTE {out}/manifest.json: {len(kept)} frames, {manifest['n_autocam']} AutoCam-seeded "
        f"(reasons={reasons})",
        flush=True,
    )


if __name__ == "__main__":
    main()
