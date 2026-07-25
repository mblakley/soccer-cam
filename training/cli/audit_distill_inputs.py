"""Audit the per-game distillation inputs across every marathon game and flag what's missing.

The distill dataset needs, per game: the raw AutoCam ``detections.json`` (marathon output, in
``F:\\autocam_data\\<gid>``), the converted per-video sidecars ``autocam_detections.jsonl`` +
``autocam_viewport.jsonl`` (next to the video), and a ``field_polygon`` in ``game.json``. All 72
trainable games should have all of it; this lists exactly which games are missing which input (and
which viewports look truncated vs ``total_frames``) so the gaps can be filled before the build.

Run on the box::

    python -m training.cli.audit_distill_inputs --autocam-root F:/autocam_data \
        --roots F:/Flash_2013s F:/Heat_2012s F:/Camera F:/Guest --out G:/ballresearch/distill/input_audit.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _lines(p: Path) -> int:
    n = 0
    with open(p, encoding="utf-8", errors="ignore") as f:
        for _ in f:
            n += 1
    return n


def build_video_map(roots: list[str]) -> dict[str, Path]:
    """``{game_id: video_dir}`` from every ``game.json`` under ``roots``."""
    out: dict[str, Path] = {}
    for r in roots:
        for gj in Path(r).glob("**/game.json"):
            try:
                j = json.loads(gj.read_text(encoding="utf-8", errors="ignore"))
            except Exception:  # noqa: BLE001
                continue
            gid = j.get("game_id")
            if gid and gid not in out:
                out[gid] = gj.parent
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--autocam-root", default="F:/autocam_data")
    ap.add_argument("--roots", nargs="+", required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    det_gids = sorted(
        d.name
        for d in Path(args.autocam_root).iterdir()
        if d.is_dir() and (d / "detections.json").exists()
    )
    vmap = build_video_map(args.roots)
    print(
        f"{len(det_gids)} games have detections.json; {len(vmap)} game.json found under roots\n",
        flush=True,
    )

    rows = []
    for gid in det_gids:
        vd = vmap.get(gid)
        row: dict = {"game_id": gid}
        if vd is None:
            row["status"] = "NO_VIDEO_DIR (no game.json with this id under roots)"
            rows.append(row)
            continue
        gj = json.loads((vd / "game.json").read_text(encoding="utf-8", errors="ignore"))
        tf = gj.get("total_frames") or 0
        row["total_frames"] = tf
        row["has_polygon"] = bool(gj.get("field_polygon"))
        det_p = vd / "autocam_detections.jsonl"
        vp_p = vd / "autocam_viewport.jsonl"
        row["det_sidecar"] = det_p.exists()
        row["vp_lines"] = _lines(vp_p) if vp_p.exists() else 0
        # viewport should have ~one row per frame; flag a stub (< 50% of total_frames)
        row["vp_truncated"] = bool(tf and row["vp_lines"] < 0.5 * tf)
        miss = []
        if not row["has_polygon"]:
            miss.append("polygon")
        if not row["det_sidecar"]:
            miss.append("det_sidecar")
        if row["vp_lines"] == 0:
            miss.append("viewport")
        elif row["vp_truncated"]:
            miss.append("viewport_truncated")
        row["missing"] = miss
        rows.append(row)

    ok = [r for r in rows if r.get("missing") == []]
    bad = [r for r in rows if r.get("missing")]
    nodir = [r for r in rows if "status" in r]
    print(f"COMPLETE: {len(ok)} / {len(rows)}")
    print(f"INCOMPLETE: {len(bad)}   NO_VIDEO_DIR: {len(nodir)}\n")

    def _tally(key):
        return sum(1 for r in bad if key in r.get("missing", []))

    print(
        f"missing polygon: {_tally('polygon')}   missing det_sidecar: {_tally('det_sidecar')}   "
        f"missing viewport: {_tally('viewport')}   viewport_truncated: {_tally('viewport_truncated')}\n"
    )
    for r in bad:
        print(
            f"  {r['game_id']:<48} missing={','.join(r['missing'])}  "
            f"(vp_lines={r['vp_lines']}/{r.get('total_frames', '?')})"
        )
    for r in nodir:
        print(f"  {r['game_id']:<48} {r['status']}")

    if args.out:
        Path(args.out).write_text(json.dumps(rows, indent=2))
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
