"""W1 operator scoreboard — score arms (campaths + AutoCam) against human GT views.

The EXP-72 session scorer promoted to committed code (EXP-OP-01 pre-registration):
per viewport-label set, every arm (named campaths + the AutoCam reference "AC") is
scored against the SAME GT views in every cell. The table is subset (ALL / original
/ ext-div) x range band (near/mid/far by expected ball diameter at the GT focal
point through the game polygon) x arm: capture@300/600, |dcx| median + p90, n; plus
the framing metrics per contiguous labeled segment (pan-velocity profile, reversal
rate with the GT-derived threshold, hold fidelity) and paired flip reads
champion-vs-AC. ``--fixture-exp72`` hard-fails unless the EXP-72 cells reproduce;
``--null-calibration`` banks split-half null bands for the framing metrics.

CPU-only, no torch; all inputs come from args (no server paths).

    python -m training.cli.operator_scoreboard \
        --set-dir D:/training_data/viewport_label/pittsford_dahua_gt \
        --game-dir "F:/Heat_2012s/2026.06.25 - vs Pittsford (home)" \
        --campath champ=G:/ballresearch/geodet/campath_pittsford.pkl \
        --fps 20 --out G:/ballresearch/geodet/scoreboard_pittsford.json \
        --fixture-exp72
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

from training.cli.build_viewport_label_queue import load_cams
from training.data_prep import distill_dataset as dd
from training.world_model import operator_metrics as om
from video_grouper.inference.world_geometry import build_field_geometry

GAP = om.DEFAULT_GAP
CAPTURE_RADII = (300, 600)
PAIR_RADIUS = 600.0
BAND_ORDER = ("all", "near", "mid", "far")

# EXP-72 correctness fixture (hard-fail gate, CLAUDE.md rule 8): the committed
# scorer must reproduce these cells from the same inputs before any new read.
FIXTURE_EXP72 = {
    "champion": {"capture@600": (0.542, 0.001), "median": (450.0, 1.0)},
    "AC": {"capture@600": (0.759, 0.001), "median": (207.0, 1.0)},
}


def _fail(msg: str) -> None:
    raise SystemExit(f"operator_scoreboard: {msg}")


def _finite(x: float | None) -> float | None:
    """Non-finite floats -> None (JSON-safe; inf hold ratios stay visible as null)."""
    return x if x is None or math.isfinite(x) else None


def _json_safe(obj):
    """Recursively make a report JSON-serializable: numpy scalars -> python,
    non-finite floats -> None, tuples -> lists."""
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, np.floating | float):
        return _finite(float(obj))
    if isinstance(obj, np.integer | int) and not isinstance(obj, bool):
        return int(obj)
    return obj


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_labels(set_dir: Path) -> dict[int, dict]:
    """``labels.json`` rows with ``action == "view"`` and a focal ``fx`` — the GT
    views every arm is scored against. Hard-fails on a missing file or an empty
    usable set (rule 8: no warnings in automated chains)."""
    p = set_dir / "labels.json"
    if not p.exists():
        _fail(f"missing labels.json in {set_dir}")
    rows = json.loads(p.read_text(encoding="utf-8"))
    labels = {
        int(r["frame_idx"]): r
        for r in rows
        if r.get("action") == "view" and r.get("fx") is not None
    }
    if not labels:
        _fail(
            f"{set_dir}: 0 usable labels (action=='view' with fx) -- nothing to score"
        )
    return labels


def load_kinds(set_dir: Path) -> dict[int, str]:
    """``manifest.json`` frame kinds (``ext-div`` marks the extension subset)."""
    p = set_dir / "manifest.json"
    if not p.exists():
        _fail(f"missing manifest.json in {set_dir}")
    man = json.loads(p.read_text(encoding="utf-8"))
    return {int(f["frame_idx"]): f.get("kind", "?") for f in man["frames"]}


def assign_bands(labels: dict[int, dict], gj: dict) -> tuple[dict[int, str], str]:
    """Range band per labeled frame from the game polygon FieldGeometry at
    ``(fx, fy)``. Returns ``({} , note)`` (band = 'all' only) when the labels have
    no fy, the game has no polygon, or the polygon fits no valid geometry — the
    note says so and goes in the report."""
    poly = gj.get("field_polygon")
    fy_frames = sorted(f for f, r in labels.items() if r.get("fy") is not None)
    if poly is None:
        return {}, "all-only: game.json has no field_polygon"
    if not fy_frames:
        return {}, "all-only: labels have no fy"
    geom = build_field_geometry(np.asarray(poly, dtype=np.float64))
    if not geom.valid:
        return {}, "all-only: field polygon fits no valid geometry (neutral fallback)"
    pts = np.asarray([[labels[f]["fx"], labels[f]["fy"]] for f in fy_frames], float)
    dias = geom.expected_ball_diameter_px(pts)
    return {f: om.band_of(d) for f, d in zip(fy_frames, dias, strict=True)}, "geometry"


def load_arms(
    labels: dict[int, dict],
    campaths: list[tuple[str, str]],
    game_dir: Path,
) -> tuple[dict[str, dict[int, float]], dict[str, dict], dict]:
    """Per-arm ``{labeled_frame: cx}`` for every named campath + the AutoCam
    reference "AC", plus per-arm coverage records.

    A campath shorter than the max label frame is NOT silently skipped: coverage
    (covered/labeled, campath length, max label frame) is computed, recorded, and
    printed — the covered n is what appears in every table cell. An arm covering
    ZERO labeled frames hard-fails."""
    gj_path = game_dir / "game.json"
    if not gj_path.exists():
        _fail(f"missing game.json in {game_dir}")
    gj = json.loads(gj_path.read_text(encoding="utf-8", errors="ignore"))
    vp_path = game_dir / "autocam_viewport.jsonl"
    if not vp_path.exists():
        _fail(f"missing autocam_viewport.jsonl in {game_dir}")
    offs = dd.seg_offsets(gj["segments"])
    vps = dd.load_viewport(vp_path, offs)

    max_f = max(labels)
    arms: dict[str, dict[int, float]] = {}
    coverage: dict[str, dict] = {}
    for name, path in campaths:
        cams, g0 = load_cams(path)
        # the padded head [0, g_start) of a camera_path/1 artifact is a constant
        # fill, not a planned camera — those frames count as UNCOVERED
        cx = {f: float(cams[f][0]) for f in labels if g0 <= f < len(cams)}
        cov = {
            "n_labeled": len(labels),
            "n_covered": len(cx),
            "campath_len": len(cams),
            "g_start": g0,
            "max_label_frame": max_f,
            "short": len(cams) <= max_f,
        }
        if not cx:
            _fail(f"arm {name}: campath covers 0 of {len(labels)} labeled frames")
        if cov["short"]:
            print(
                f"NOTE arm {name}: campath length {len(cams)} <= max label frame "
                f"{max_f} -- covers {len(cx)}/{len(labels)} labeled frames "
                "(coverage n is reported per cell, not silently skipped)"
            )
        arms[name] = cx
        coverage[name] = cov
    ac = {f: float(vps[f][0]) for f in labels if f in vps}
    if not ac:
        _fail(f"arm AC: autocam_viewport covers 0 of {len(labels)} labeled frames")
    arms["AC"] = ac
    coverage["AC"] = {"n_labeled": len(labels), "n_covered": len(ac)}
    return arms, coverage, gj


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def build_subsets(
    labels: dict[int, dict], kinds: dict[int, str]
) -> dict[str, list[int]]:
    """ALL always; original (kind != 'ext-div') and ext-div only when present."""
    frames = sorted(labels)
    subsets = {"ALL": frames}
    ext = [f for f in frames if kinds.get(f) == "ext-div"]
    if ext:
        subsets["original"] = [f for f in frames if kinds.get(f) != "ext-div"]
        subsets["ext-div"] = ext
    return subsets


def _deltas(
    arm_cx: dict[int, float], labels: dict[int, dict], frames: list[int]
) -> list[tuple[int, float]]:
    """The EXP-72 per-arm read: ``[(f, |cx - fx|)]`` over labeled frames where
    the arm has a cx at f."""
    return [(f, abs(arm_cx[f] - float(labels[f]["fx"]))) for f in frames if f in arm_cx]


def build_cells(
    labels: dict[int, dict],
    arms: dict[str, dict[int, float]],
    subsets: dict[str, list[int]],
    band_by_frame: dict[int, str],
) -> tuple[dict, dict]:
    """Subset x band x arm capture cells + the raw deltas (for cross-set pooling)."""
    bands = ["all"] + [b for b in BAND_ORDER[1:] if b in set(band_by_frame.values())]
    cells: dict = {}
    raw: dict[tuple[str, str, str], list[tuple[int, float]]] = {}
    for sname, sframes in subsets.items():
        cells[sname] = {}
        for bname in bands:
            bframes = (
                sframes
                if bname == "all"
                else [f for f in sframes if band_by_frame.get(f) == bname]
            )
            row = {"n_labeled": len(bframes), "arms": {}}
            for aname, cx in arms.items():
                ds = _deltas(cx, labels, bframes)
                row["arms"][aname] = om.capture_stats(ds, CAPTURE_RADII)
                raw[(sname, bname, aname)] = ds
            cells[sname][bname] = row
    return cells, raw


def build_framing(
    labels: dict[int, dict],
    arms: dict[str, dict[int, float]],
    fps: float,
) -> tuple[dict, dict]:
    """Framing metrics per contiguous labeled segment, pooled per arm: pan-velocity
    median/p90 vs GT's, reversal rate with the GT-derived threshold, hold fidelity.
    Non-computable on this instrument (no segment has 2+ GT labels) is STATED, not
    silently skipped, per the pre-registration."""
    gt_fx = {f: float(r["fx"]) for f, r in labels.items()}
    segments = om.segment_series(sorted(labels), gap=GAP)
    ctx = {"segments": segments, "gt_fx": gt_fx, "v_thresh": None}
    gt_vels = [v for seg in segments for v in om.pan_velocity(gt_fx, seg, fps)]
    if not gt_vels:
        note = "framing not computable: no labeled segment has 2+ GT frames"
        return {"computable": False, "note": note, "n_segments": len(segments)}, ctx
    v_thresh = om.gt_velocity_threshold(gt_vels)
    ctx["v_thresh"] = v_thresh

    def _pool(cx: dict[int, float]) -> dict:
        vels = [v for seg in segments for v in om.pan_velocity(cx, seg, fps)]
        flips, minutes = 0, 0.0
        for seg in segments:
            rr = om.reversal_rate(cx, seg, fps, v_thresh)
            flips += rr["flips"]
            minutes += rr["minutes"]
        return {
            "velocity": om.velocity_summary(vels),
            "reversal": {
                "flips": flips,
                "minutes": minutes,
                "rate": (flips / minutes) if minutes > 0 else None,
            },
        }

    out = {
        "computable": True,
        "n_segments": len(segments),
        "gt": {**_pool(gt_fx), "v_thresh": v_thresh},
        "arms": {},
    }
    for aname, cx in arms.items():
        block = _pool(cx)
        rows, med = om.hold_fidelity(cx, gt_fx, segments)
        block["hold"] = {
            "n_segments": len(rows),
            "median_ratio": _finite(med),
            "rows": [
                [seg[0], seg[-1], gsw, asw, _finite(ratio)]
                for seg, gsw, asw, ratio in rows
            ],
        }
        out["arms"][aname] = block
    return out, ctx


def build_pair_flips(
    labels: dict[int, dict],
    arms: dict[str, dict[int, float]],
    subsets: dict[str, list[int]],
    champ_names: list[str],
) -> dict:
    """Paired champion-vs-AC flip reads per subset: event + frame counts of
    a-captured-b-didn't (@600) and the reverse, on common frames."""
    out: dict = {}
    for sname, sframes in subsets.items():
        out[sname] = {}
        for name in champ_names:
            caps = {}
            for arm in (name, "AC"):
                cx = arms[arm]
                caps[arm] = {
                    f: abs(cx[f] - float(labels[f]["fx"])) <= PAIR_RADIUS
                    for f in sframes
                    if f in cx
                }
            out[sname][f"{name}_vs_AC"] = om.pair_flip_read(
                caps[name], caps["AC"], gap=GAP
            )
    return out


def score_set(
    set_dir: str, game_dir: str, campaths: list[tuple[str, str]], fps: float
) -> tuple[dict, dict, dict]:
    """Score one viewport-label set. Returns (report block, raw deltas for
    pooling, context for null calibration)."""
    sd, gd = Path(set_dir), Path(game_dir)
    labels = load_labels(sd)
    kinds = load_kinds(sd)
    arms, coverage, gj = load_arms(labels, campaths, gd)
    band_by_frame, banding_note = assign_bands(labels, gj)
    subsets = build_subsets(labels, kinds)
    cells, raw = build_cells(labels, arms, subsets, band_by_frame)
    framing, ctx = build_framing(labels, arms, fps)
    champ_names = [n for n, _ in campaths]
    block = {
        "set_dir": str(sd),
        "game_dir": str(gd),
        "n_labels": len(labels),
        "banding": "geometry" if band_by_frame else "all-only",
        "banding_note": banding_note,
        "coverage": coverage,
        "cells": cells,
        "framing": framing,
        "pair_flips": build_pair_flips(labels, arms, subsets, champ_names),
    }
    ctx["labels"] = labels
    ctx["arms"] = arms
    return block, raw, ctx


def pool_cells(raw_by_set: list[dict]) -> dict:
    """Pool raw deltas across sets into one (subset, band, arm) cell table."""
    pooled_raw: dict[tuple[str, str, str], list[tuple[int, float]]] = {}
    for raw in raw_by_set:
        for (sname, bname, aname), ds in raw.items():
            pooled_raw.setdefault((sname, bname, aname), []).extend(ds)
    cells: dict = {}
    for (sname, bname, aname), ds in pooled_raw.items():
        row = cells.setdefault(sname, {}).setdefault(bname, {"arms": {}})
        row["arms"][aname] = om.capture_stats(ds, CAPTURE_RADII)
    return cells


# ---------------------------------------------------------------------------
# Null calibration (instrument admission, DECISIONS (g))
# ---------------------------------------------------------------------------


def run_null_calibration(ctx: dict, champ: str, fps: float, seed: int) -> dict:
    """Split-half null bands for each framing metric on the champion arm of one
    instrument (set). 300 reps, event = contiguous labeled segment. A band that
    cannot be computed records its power floor EXPLICITLY (n_events + reason) in
    the report — never a silent absence."""
    segments, gt_fx, v_thresh = ctx["segments"], ctx["gt_fx"], ctx["v_thresh"]
    cx = ctx["arms"][champ]

    def _vels(frames: list[int]) -> list[float]:
        return [
            v
            for seg in om.segment_series(frames, gap=GAP)
            for v in om.pan_velocity(cx, seg, fps)
        ]

    def m_vel_median(frames: list[int]) -> float | None:
        return om.velocity_summary(_vels(frames))["median"]

    def m_vel_p90(frames: list[int]) -> float | None:
        return om.velocity_summary(_vels(frames))["p90"]

    def m_reversal(frames: list[int]) -> float | None:
        flips, minutes = 0, 0.0
        for seg in om.segment_series(frames, gap=GAP):
            rr = om.reversal_rate(cx, seg, fps, v_thresh)
            flips += rr["flips"]
            minutes += rr["minutes"]
        return (flips / minutes) if minutes > 0 else None

    def m_hold(frames: list[int]) -> float | None:
        _rows, med = om.hold_fidelity(cx, gt_fx, om.segment_series(frames, gap=GAP))
        return med

    metrics: dict = {
        "pan_velocity_median": m_vel_median,
        "pan_velocity_p90": m_vel_p90,
        "reversal_rate": m_reversal if v_thresh is not None else None,
        "hold_fidelity_median_ratio": m_hold,
    }
    out: dict = {"arm": champ, "reps": 300, "seed": seed}
    for mname, fn in metrics.items():
        if fn is None:
            out[mname] = {
                "band": None,
                "power_floor": {
                    "n_events": len(segments),
                    "reason": "no GT velocity threshold (framing not computable)",
                },
            }
            continue
        res = om.split_half_null(segments, cx, fn, reps=300, seed=seed)
        entry = {"n_events": res["n_events"], "reps_valid": res["reps_valid"]}
        if res["band"] is None:
            entry["band"] = None
            entry["power_floor"] = {
                "n_events": res["n_events"],
                "reason": res["reason"],
            }
        else:
            entry["band"] = [res["band"][0], res["band"][1]]
        out[mname] = entry
    return out


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _fmt(x, spec: str = ".3f") -> str:
    return "-" if x is None else format(x, spec)


def print_table(report: dict) -> None:
    hdr = (
        f"{'subset':10s} {'band':5s} {'arm':10s} {'n_lab':>6s} {'n':>6s} "
        f"{'cap@300':>8s} {'cap@600':>8s} {'med|dx|':>8s} {'p90|dx|':>8s}"
    )
    for set_name, blk in report["sets"].items():
        note = "" if blk["banding"] == "geometry" else f" -- {blk['banding_note']}"
        print(f"\n=== SET {set_name} (banding: {blk['banding']}{note}) ===")
        print(hdr)
        for sname, by_band in blk["cells"].items():
            for bname, row in by_band.items():
                for aname, st in row["arms"].items():
                    print(
                        f"{sname:10s} {bname:5s} {aname:10s} "
                        f"{row['n_labeled']:6d} {st['n']:6d} "
                        f"{_fmt(st['capture'][300]):>8s} {_fmt(st['capture'][600]):>8s} "
                        f"{_fmt(st['median'], '.1f'):>8s} {_fmt(st['p90'], '.1f'):>8s}"
                    )
        fr = blk["framing"]
        if not fr.get("computable"):
            print(f"framing: {fr['note']}")
        else:
            gt = fr["gt"]
            print(
                f"framing ({fr['n_segments']} segments): GT vel med/p90 "
                f"{_fmt(gt['velocity']['median'], '.1f')}/{_fmt(gt['velocity']['p90'], '.1f')} px/s, "
                f"v_thresh {gt['v_thresh']:.1f}, GT reversal rate "
                f"{_fmt(gt['reversal']['rate'], '.2f')}/min"
            )
            for aname, ab in fr["arms"].items():
                hold = ab["hold"]
                print(
                    f"  {aname:10s} vel med/p90 "
                    f"{_fmt(ab['velocity']['median'], '.1f')}/{_fmt(ab['velocity']['p90'], '.1f')} px/s, "
                    f"reversal {ab['reversal']['flips']} flips / "
                    f"{ab['reversal']['minutes']:.2f} min = "
                    f"{_fmt(ab['reversal']['rate'], '.2f')}/min, "
                    f"hold median ratio {_fmt(hold['median_ratio'], '.2f')} "
                    f"(n={hold['n_segments']})"
                )
        for sname, pairs in blk["pair_flips"].items():
            for pname, pf in pairs.items():
                print(
                    f"pair {sname:10s} {pname}: a-only {pf['a_only_events']} ev / "
                    f"{pf['a_only_frames']} fr, b-only {pf['b_only_events']} ev / "
                    f"{pf['b_only_frames']} fr (common n={pf['n_common']})"
                )
    if "pooled" in report:
        print("\n=== POOLED (all sets) ===")
        print(hdr)
        for sname, by_band in report["pooled"].items():
            for bname, row in by_band.items():
                for aname, st in row["arms"].items():
                    print(
                        f"{sname:10s} {bname:5s} {aname:10s} {'-':>6s} {st['n']:6d} "
                        f"{_fmt(st['capture'][300]):>8s} {_fmt(st['capture'][600]):>8s} "
                        f"{_fmt(st['median'], '.1f'):>8s} {_fmt(st['p90'], '.1f'):>8s}"
                    )


def run_fixture(pooled: dict, champ: str) -> None:
    """Hard-fail correctness gate: the ALL-subset cells must reproduce EXP-72."""
    cell = pooled.get("ALL", {}).get("all", {}).get("arms", {})
    checks = []
    for arm_key, arm in (("champion", champ), ("AC", "AC")):
        st = cell.get(arm)
        want = FIXTURE_EXP72[arm_key]
        got600 = None if st is None else st["capture"][600]
        gotmed = None if st is None else st["median"]
        checks.append((arm, "capture@600", got600, *want["capture@600"]))
        checks.append((arm, "median|dx|", gotmed, *want["median"]))
    bad = [c for c in checks if c[2] is None or abs(c[2] - c[3]) > c[4]]
    if bad:
        print("FIXTURE EXP-72 MISMATCH -- actual cells:")
        for arm in (champ, "AC"):
            st = cell.get(arm)
            print(f"  {arm}: {json.dumps(_json_safe(st))}")
        for arm, metric, got, want, tol in bad:
            print(
                f"  FAIL {arm} {metric}: got "
                f"{'-' if got is None else format(got, '.4f')} "
                f"want {want} +/- {tol}"
            )
        raise SystemExit(1)
    ch, ac = cell[champ], cell["AC"]
    print(
        f"FIXTURE EXP-72 OK: {champ} capture@600 {ch['capture'][600]:.3f} "
        f"median {ch['median']:.0f}px (n={ch['n']}); "
        f"AC capture@600 {ac['capture'][600]:.3f} median {ac['median']:.0f}px "
        f"(n={ac['n']})"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--set-dir",
        action="append",
        required=True,
        help="viewport label set dir (labels.json + manifest.json); repeatable",
    )
    ap.add_argument(
        "--game-dir",
        action="append",
        required=True,
        help="game dir with game.json + autocam_viewport.jsonl, matching "
        "--set-dir order; repeatable",
    )
    ap.add_argument(
        "--campath",
        action="append",
        required=True,
        metavar="NAME=PATH",
        help="arm NAME=PATH (camera_path/1 JSON or the load_cams pickle); "
        "repeatable; the FIRST one is the champion",
    )
    ap.add_argument("--out", required=True, help="JSON report path")
    ap.add_argument(
        "--fixture-exp72",
        action="store_true",
        help="hard-fail unless the pooled ALL cells reproduce the EXP-72 numbers",
    )
    ap.add_argument(
        "--null-calibration",
        action="store_true",
        help="bank split-half null bands (300 reps) for each framing metric x "
        "instrument on the champion arm",
    )
    ap.add_argument("--seed", type=int, default=72)
    ap.add_argument("--fps", type=float, default=30.0)
    args = ap.parse_args(argv)

    if len(args.set_dir) != len(args.game_dir):
        _fail(
            f"--set-dir count ({len(args.set_dir)}) != --game-dir count "
            f"({len(args.game_dir)}) -- they pair by order"
        )
    campaths: list[tuple[str, str]] = []
    for spec in args.campath:
        if "=" not in spec:
            _fail(f"--campath must be NAME=PATH: {spec!r}")
        name, path = spec.split("=", 1)
        if name == "AC":
            _fail("arm name 'AC' is reserved for the AutoCam reference")
        campaths.append((name, path))
    champ = campaths[0][0]

    report: dict = {
        "schema": "operator_scoreboard/1",
        "fps": args.fps,
        "gap": GAP,
        "capture_radii": list(CAPTURE_RADII),
        "pair_radius": PAIR_RADIUS,
        "arms": [n for n, _ in campaths] + ["AC"],
        "champion_arm": champ,
        "sets": {},
    }
    raw_by_set: list[dict] = []
    ctx_by_set: dict[str, dict] = {}
    for set_dir, game_dir in zip(args.set_dir, args.game_dir, strict=True):
        set_name = Path(set_dir).name
        block, raw, ctx = score_set(set_dir, game_dir, campaths, args.fps)
        report["sets"][set_name] = block
        raw_by_set.append(raw)
        ctx_by_set[set_name] = ctx
    report["pooled"] = pool_cells(raw_by_set)

    if args.null_calibration:
        report["null_calibration"] = {}
        for set_name, ctx in ctx_by_set.items():
            nc = run_null_calibration(ctx, champ, args.fps, args.seed)
            report["null_calibration"][set_name] = nc
            for mname, entry in nc.items():
                if isinstance(entry, dict) and entry.get("band") is None:
                    print(
                        f"NULL-CAL {set_name}/{mname}: band NOT computable -- "
                        f"power floor recorded ({entry['power_floor']})"
                    )

    print_table(report)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(_json_safe(report), indent=1))
    print(f"\nWROTE {args.out}")

    if args.fixture_exp72:
        run_fixture(report["pooled"], champ)


if __name__ == "__main__":
    main()
