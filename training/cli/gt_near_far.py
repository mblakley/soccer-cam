"""Learn the near/far boundary from human far-ball GT + AutoCam viewport, and validate selection.

The human far-ball labels mark exactly the balls AutoCam struggles with (that's why they were
collected), and the viewport is AutoCam's own selected ball. So on the games that have *both* human
GT and a viewport, we can read the boundary directly: at each GT ball, how far is AutoCam's viewport
from the true ball? Where that error jumps with field position is where AutoCam loses the ball — the
near/far boundary. The same pass measures whether the ball is even **recoverable** from the raw
detections (a candidate near the GT) and how well two selection rules land on it:

* ``vp`` — the detection candidate nearest the viewport (today's teacher rule), and
* ``gt`` — the detection candidate nearest the GT (the achievable ceiling for any selector).

Binned by **apparent ball size** (perspective-correct, from the field geometry) and by curve-depth,
this says where to cut near/far and whether a viewport-free selector could match the viewport-gated
one (so the 24 games with no viewport could be filled from detections alone).

Run on the box::

    python -m training.cli.gt_near_far --roots F:/Flash_2013s F:/Heat_2012s \
        --out G:/ballresearch/distill/gt_near_far.json
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

from training.data_prep import distill_dataset as dd


def labeled_game_dirs(roots: list[str]) -> list[Path]:
    out: list[Path] = []
    seen: set[Path] = set()
    for r in roots:
        for gj in Path(r).glob("**/game.json"):
            d = gj.parent
            if d in seen:
                continue
            if all(
                (d / f).exists()
                for f in (
                    "ball_labels.jsonl",
                    "autocam_viewport.jsonl",
                    "autocam_detections.jsonl",
                )
            ):
                seen.add(d)
                out.append(d)
    return out


def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def collect(game_dir: Path) -> list[dict]:
    from training.world_model.geometry import build_field_geometry

    gj = json.loads(
        (game_dir / "game.json").read_text(encoding="utf-8", errors="ignore")
    )
    poly = gj.get("field_polygon")
    if not poly:
        return []
    offsets = dd.seg_offsets(gj["segments"])
    far_edge, near_edge = dd.field_edges(poly)
    geom = build_field_geometry(np.asarray(poly, float))
    dets = dd.load_detections(game_dir / "autocam_detections.jsonl", offsets)
    vps = dd.load_viewport(game_dir / "autocam_viewport.jsonl", offsets)
    balls, _novis = dd.load_human_labels(game_dir / "ball_labels.jsonl", offsets)

    rows: list[dict] = []
    for g, gt in balls.items():
        size = (
            float(geom.expected_ball_diameter_px(np.asarray(gt))[0])
            if geom.valid
            else float("nan")
        )
        depth = dd.curve_depth(gt[0], gt[1], far_edge, near_edge)
        vp = vps.get(g)
        cands = dets.get(g) or []
        # nearest candidate to GT (recoverability ceiling) and to viewport (current rule)
        gt_cand = min(
            ((_dist((c[0], c[1]), gt), c) for c in cands), default=(None, None)
        )
        vp_cand = (
            min(((_dist((c[0], c[1]), vp), c) for c in cands), default=(None, None))
            if vp
            else (None, None)
        )
        rows.append(
            {
                "gid": gj["game_id"],
                "depth": round(depth, 4),
                "size_px": round(size, 2) if not math.isnan(size) else None,
                "vp_err": round(_dist(vp, gt), 1) if vp else None,
                "gt_cand_dist": round(gt_cand[0], 1)
                if gt_cand[0] is not None
                else None,
                "vp_cand_to_gt": round(_dist((vp_cand[1][0], vp_cand[1][1]), gt), 1)
                if vp_cand[1] is not None
                else None,
                "n_cands": len(cands),
            }
        )
    return rows


def _binned(
    rows: list[dict], key: str, edges: np.ndarray, recover_px: float
) -> list[dict]:
    out = []
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        sub = [r for r in rows if r.get(key) is not None and lo <= r[key] < hi]
        if not sub:
            out.append({"lo": round(float(lo), 2), "hi": round(float(hi), 2), "n": 0})
            continue
        vperr = [r["vp_err"] for r in sub if r["vp_err"] is not None]
        recov = [r["gt_cand_dist"] for r in sub if r["gt_cand_dist"] is not None]
        vpc = [r["vp_cand_to_gt"] for r in sub if r["vp_cand_to_gt"] is not None]
        out.append(
            {
                "lo": round(float(lo), 2),
                "hi": round(float(hi), 2),
                "n": len(sub),
                "vp_err_med": round(float(np.median(vperr)), 1) if vperr else None,
                "recoverable_rate": round(
                    float(np.mean([d <= recover_px for d in recov])), 3
                )
                if recov
                else None,
                "vp_sel_hit_rate": round(
                    float(np.mean([d <= recover_px for d in vpc])), 3
                )
                if vpc
                else None,
            }
        )
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--roots", nargs="+", required=True)
    ap.add_argument(
        "--recover-px", type=float, default=25.0, help="dist<= = 'on the ball'"
    )
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    dirs = labeled_game_dirs(args.roots)
    print(f"{len(dirs)} games with human GT + viewport + detections", flush=True)
    rows: list[dict] = []
    for d in dirs:
        try:
            r = collect(d)
        except Exception as e:  # noqa: BLE001
            print(f"  SKIP {d.name}: {e}", flush=True)
            continue
        rows.extend(r)
        print(f"  {d.name}: {len(r)} GT balls", flush=True)

    rows = [r for r in rows if "depth" in r and not r.get("novis")]
    if not rows:
        raise SystemExit("no GT rows")
    print(f"\nTOTAL {len(rows)} human GT balls\n")

    print("=== by APPARENT BALL SIZE (px; small = far) ===")
    print(
        f"{'size_px':>14} {'n':>6} {'vp_err_med':>10} {'recover':>8} {'vp_sel_hit':>10}"
    )
    for r in _binned(
        rows, "size_px", np.array([0, 4, 6, 8, 10, 12, 15, 20, 30, 60]), args.recover_px
    ):
        print(
            f"  {r['lo']:>5.0f}-{r['hi']:<5.0f} {r['n']:>6} "
            f"{(r.get('vp_err_med') or 0):>10.1f} {(r.get('recoverable_rate') or 0):>8.2f} "
            f"{(r.get('vp_sel_hit_rate') or 0):>10.2f}"
        )

    print("\n=== by CURVE DEPTH (0=far touchline, 1=near) ===")
    print(
        f"{'depth':>14} {'n':>6} {'vp_err_med':>10} {'recover':>8} {'vp_sel_hit':>10}"
    )
    for r in _binned(rows, "depth", np.linspace(0, 1, 11), args.recover_px):
        print(
            f"  {r['lo']:.1f}-{r['hi']:.1f} {r['n']:>10} "
            f"{(r.get('vp_err_med') or 0):>10.1f} {(r.get('recoverable_rate') or 0):>8.2f} "
            f"{(r.get('vp_sel_hit_rate') or 0):>10.2f}"
        )

    if args.out:
        Path(args.out).write_text(json.dumps(rows, indent=2))
        print(f"\nwrote {args.out}  ({len(rows)} rows)")


if __name__ == "__main__":
    main()
