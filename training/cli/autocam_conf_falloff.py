"""Measure where AutoCam's ball-detection confidence falls off with field depth — across all games.

The far/non-far split (where we stop distilling AutoCam and let the human GT own the ball) should
sit where AutoCam *actually* loses confidence in the ball, not at a guessed depth fraction. AutoCam's
re-run detections carry per-candidate confidence, so we can read the falloff directly: for each frame
take AutoCam's selected ball (the in-field detection candidate nearest its viewport), record its
``(curve_depth, conf)``, aggregate over every game, and bin by depth. Where the per-bin confidence
rolls off is the far boundary — in normalized field depth, so it applies per game through each
game's own touchline curve.

Run on the GPU box (F: detections/sidecars are local)::

    python -m training.cli.autocam_conf_falloff --roots F:/Flash_2013s F:/Heat_2013s \
        --stride 8 --out G:/ballresearch/distill/conf_falloff.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from training.data_prep import distill_dataset as dd


def find_game_dirs(roots: list[str]) -> list[Path]:
    """All video dirs under ``roots`` that have ``game.json`` + the two AutoCam sidecars."""
    seen: set[Path] = set()
    out: list[Path] = []
    for r in roots:
        for gj in Path(r).glob("**/game.json"):
            d = gj.parent
            if d in seen:
                continue
            if (d / "autocam_detections.jsonl").exists() and (
                d / "autocam_viewport.jsonl"
            ).exists():
                seen.add(d)
                out.append(d)
    return out


def _in_field(poly: np.ndarray, x: float, y: float, margin: float = 50.0) -> bool:
    import cv2

    return cv2.pointPolygonTest(poly, (float(x), float(y)), True) >= -margin


def collect_pairs(
    game_dir: Path, *, stride: int, gate_px: float = 90.0
) -> tuple[str, np.ndarray] | None:
    """``(game_id, (N,2) array of [depth, conf])`` for AutoCam's selected ball, subsampled by stride."""
    gj = json.loads((game_dir / "game.json").read_text())
    poly = gj.get("field_polygon")
    if not poly:
        return None
    poly_arr = np.asarray(poly, dtype=np.float32)
    offsets = dd.seg_offsets(gj["segments"])
    far_edge, near_edge = dd.field_edges(poly)
    dets = dd.load_detections(game_dir / "autocam_detections.jsonl", offsets)
    vps = dd.load_viewport(game_dir / "autocam_viewport.jsonl", offsets)

    pairs: list[tuple[float, float]] = []
    for i, g in enumerate(sorted(vps)):
        if i % stride:
            continue
        cands = dets.get(g)
        if not cands:
            continue
        vx, vy = vps[g]
        best, best_d = None, gate_px * gate_px
        for cx, cy, conf in cands:
            d2 = (cx - vx) ** 2 + (cy - vy) ** 2
            if d2 > best_d:
                continue
            if not _in_field(poly_arr, cx, cy):
                continue
            best, best_d = (cx, cy, conf), d2
        if best is None:
            continue
        cx, cy, conf = best
        pairs.append((dd.curve_depth(cx, cy, far_edge, near_edge), conf))
    if not pairs:
        return None
    return gj["game_id"], np.asarray(pairs, dtype=np.float64)


def summarize(all_pairs: np.ndarray, n_bins: int = 20) -> list[dict]:
    """Bin by depth (0=far touchline, 1=near) and report count + conf stats per bin."""
    rows = []
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        m = (all_pairs[:, 0] >= lo) & (all_pairs[:, 0] < hi)
        confs = all_pairs[m, 1]
        rows.append(
            {
                "depth_lo": round(float(lo), 3),
                "depth_hi": round(float(hi), 3),
                "n": int(m.sum()),
                "conf_median": round(float(np.median(confs)), 4)
                if len(confs)
                else None,
                "conf_mean": round(float(np.mean(confs)), 4) if len(confs) else None,
                "conf_p25": round(float(np.percentile(confs, 25)), 4)
                if len(confs)
                else None,
            }
        )
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--roots", nargs="+", required=True)
    ap.add_argument("--stride", type=int, default=8, help="frame subsample per game")
    ap.add_argument("--bins", type=int, default=20)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    dirs = find_game_dirs(args.roots)
    print(f"found {len(dirs)} game dirs with detections+viewport+polygon", flush=True)

    chunks: list[np.ndarray] = []
    per_game = []
    for d in dirs:
        try:
            res = collect_pairs(d, stride=args.stride)
        except Exception as e:  # noqa: BLE001 — robustness across 72 heterogeneous games
            print(f"  SKIP {d.name}: {e}", flush=True)
            continue
        if res is None:
            print(f"  skip {d.name}: no polygon / no pairs", flush=True)
            continue
        gid, arr = res
        chunks.append(arr)
        per_game.append({"game_id": gid, "n": len(arr)})
        print(f"  {gid}: {len(arr)} ball-conf samples", flush=True)

    if not chunks:
        raise SystemExit("no data collected")
    allp = np.concatenate(chunks, axis=0)
    rows = summarize(allp, args.bins)
    print(f"\nTOTAL {len(allp)} samples across {len(chunks)} games")
    print(f"{'depth':>14} {'n':>8} {'median':>8} {'mean':>8} {'p25':>8}")
    for r in rows:
        print(
            f"  {r['depth_lo']:.2f}-{r['depth_hi']:.2f} {r['n']:>8} "
            f"{(r['conf_median'] or 0):>8.3f} {(r['conf_mean'] or 0):>8.3f} "
            f"{(r['conf_p25'] or 0):>8.3f}"
        )
    if args.out:
        Path(args.out).write_text(
            json.dumps(
                {
                    "n_games": len(chunks),
                    "n_samples": len(allp),
                    "bins": rows,
                    "per_game": per_game,
                },
                indent=2,
            )
        )
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
