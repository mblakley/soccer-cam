"""Verdict composer — emits the BOUND verdict composition (DECISIONS 2026-07-23):
the tie-break hierarchy walked top-down with every row stated decisive-or-zero,
plus the full arm x instrument x strata table with no cell omitted as noise.

Protocol lives in tooling: a verdict that takes longer to read than to run is a
protocol bug. Runs on cached eval dumps (zero GPU).

Usage (server):
    python -m training.experiments.compose_verdict \
        --dump-dir G:/ballresearch/geodet --arms mg_ctrl mg_geo mg_norm

Dump naming: cands_<instrument>_<arm>.pkl. Instrument registry (EXP-DIST-71):
spc = SPC-134 (continuity) | spc18k = SPC-18k@5473 | iron18k = IRON-18k@1501 |
fair = FAIR-6k@47640. Pittsford-human + viewport rows are appended manually
when available (printed as PENDING otherwise).
"""

from __future__ import annotations

import argparse
import json
import pickle
from pathlib import Path

import numpy as np

GAP = 64
NBOOT = 2000
FAR_PX = 8.0
# hierarchy order (DECISIONS 07-23 amended): detector-metric instruments only —
# the human/viewport rows sit above these and are appended by the caller.
HIERARCHY = ["iron18k", "spc18k", "spc", "fair"]
INSTRUMENT_NAMES = {
    "spc": "SPC-134",
    "spc18k": "SPC-18k@5473",
    "iron18k": "IRON-18k@1501",
    "fair": "FAIR-6k@47640",
}
SPC_DIR = Path(r"F:\Heat_2012s\2026.05.31 - vs Spencerport gold 2 (away)")


def _events(frames: list[int]) -> list[list[int]]:
    frames = sorted(frames)
    if not frames:
        return []
    ev = [[frames[0]]]
    for f in frames[1:]:
        (ev[-1].append(f) if f - ev[-1][-1] <= GAP else ev.append([f]))
    return ev


def _load_strata(game_dir: Path) -> dict[int, str]:
    """frame -> NORMAL/HARD/AGREE from ball_labels set provenance."""
    from training.data_prep import distill_dataset as dd

    out: dict[int, str] = {}
    p = game_dir / "ball_labels.jsonl"
    if not p.exists():
        return out
    gj = json.loads(
        (game_dir / "game.json").read_text(encoding="utf-8", errors="ignore")
    )
    offs = dd.seg_offsets(gj["segments"])
    with open(p, encoding="utf-8", errors="ignore") as fh:
        for ln in fh:
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if r.get("seg") is None or r.get("f") is None:
                continue
            g = offs.get(r["seg"], 0) + int(r["f"])
            s = str(r.get("set", "")).lower()
            out[g] = (
                "AGREE"
                if "agree" in s
                else "HARD"
                if any(k in s for k in ("clip", "hard", "diverge", "uncertain"))
                else "NORMAL"
            )
    return out


def score_dump(pkl_path: Path, rng: np.random.Generator):
    """{metric: (point, lo, hi, n_events)} for far/near ceiling/argmax."""
    from video_grouper.inference.world_geometry import build_field_geometry

    with open(pkl_path, "rb") as fh:
        d = pickle.load(fh)
    geom = build_field_geometry(np.asarray(d["polygon"], float))
    if not geom.valid:
        return None, None
    ef = d["ef"]
    pf: dict[int, tuple[bool, bool, bool]] = {}
    for g, gt in d["balls"].items():
        nf = min(ef, key=lambda f: abs(f - g))
        if abs(nf - g) > 4:
            continue
        cl = d["cands"].get(nf) or []
        gt = tuple(gt)
        gw = geom.image_to_world(np.asarray([gt], float))[0]
        far = float(geom.expected_ball_diameter_px(np.asarray([gt], float))[0]) < float(
            d.get("far_size_px", FAR_PX)
        )
        ch = ah = False
        if cl:
            ws = geom.image_to_world(np.asarray([(c[0], c[1]) for c in cl], float))
            dist = np.linalg.norm(ws - gw, axis=1)
            ch = bool((dist <= 15.0).any())
            ah = bool(dist[int(np.argmax([c[2] for c in cl]))] <= 15.0)
        pf[g] = (ch, ah, far)
    out = {}
    for band in ("far", "near"):
        ev = [[f for f in e if pf[f][2] == (band == "far")] for e in _events(list(pf))]
        ev = [e for e in ev if e]
        if not ev:
            continue
        for mi, mn in ((0, "ceil"), (1, "arg")):

            def rate(el):
                fs = [f for e in el for f in e]
                return sum(pf[f][mi] for f in fs) / len(fs)

            pt = rate(ev)
            boots = [
                rate([ev[i] for i in rng.integers(0, len(ev), len(ev))])
                for _ in range(NBOOT)
            ]
            lo, hi = np.percentile(boots, [2.5, 97.5])
            out[f"{band}-{mn}"] = (pt, float(lo), float(hi), len(ev))
    return out, pf


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump-dir", required=True)
    ap.add_argument("--arms", nargs="+", required=True)
    ap.add_argument(
        "--pairs",
        nargs="*",
        default=None,
        help="arm pairs 'A:B' for decisive-or-zero calls (default: all vs first arm + geo:norm)",
    )
    args = ap.parse_args()
    dd_ = Path(args.dump_dir)
    rng = np.random.default_rng(7)

    scored: dict[tuple[str, str], dict] = {}
    frames: dict[tuple[str, str], dict] = {}
    for inst in HIERARCHY:
        for arm in args.arms:
            p = dd_ / f"cands_{inst}_{arm}.pkl"
            if not p.exists():
                continue
            m, pf = score_dump(p, rng)
            if m:
                scored[(inst, arm)] = m
                frames[(inst, arm)] = pf

    print("=== FULL TABLE (arm x instrument; point [95% event-CI] (events)) ===")
    metrics = ("far-ceil", "far-arg", "near-ceil", "near-arg")
    for inst in HIERARCHY:
        rows = [(a, scored.get((inst, a))) for a in args.arms]
        if not any(m for _, m in rows):
            print(f"\n  {INSTRUMENT_NAMES[inst]}: PENDING (no dumps)")
            continue
        print(f"\n  {INSTRUMENT_NAMES[inst]}")
        for arm, m in rows:
            if not m:
                print(f"    {arm:<10} PENDING")
                continue
            cells = "  ".join(
                f"{k}={m[k][0]:.3f}[{m[k][1]:.2f},{m[k][2]:.2f}]e{m[k][3]}"
                for k in metrics
                if k in m
            )
            print(f"    {arm:<10} {cells}")

    # strata columns for SPC instruments
    strata = _load_strata(SPC_DIR)
    if strata:
        print("\n=== SPC STRATA (hit-rate by NORMAL/HARD/AGREE, argmax) ===")
        for inst in ("spc", "spc18k"):
            for arm in args.arms:
                pf = frames.get((inst, arm))
                if not pf:
                    continue
                cells = []
                for st in ("NORMAL", "HARD", "AGREE"):
                    fs = [f for f in pf if strata.get(f) == st]
                    if fs:
                        cells.append(
                            f"{st}={sum(pf[f][1] for f in fs) / len(fs):.3f}(n{len(fs)})"
                        )
                if cells:
                    print(
                        f"  {INSTRUMENT_NAMES[inst]:<14} {arm:<10} " + "  ".join(cells)
                    )

    # hierarchy walk: decisive-or-zero per pair
    pairs = args.pairs or []
    if not pairs and len(args.arms) >= 2:
        base = args.arms[0]
        pairs = [f"{a}:{base}" for a in args.arms[1:]]
        if len(args.arms) >= 3:
            pairs.append(f"{args.arms[1]}:{args.arms[2]}")
    print(
        "\n=== HIERARCHY WALK (rows above: Pittsford-human, viewport-v1 = PENDING/manual) ==="
    )
    import math

    def _sign_p(k: int, n: int) -> float:
        if n == 0:
            return 1.0
        p = sum(math.comb(n, i) for i in range(0, min(k, n - k) + 1)) / 2**n * 2
        return min(1.0, p)

    for pair in pairs:
        a, b = pair.split(":")
        print(f"\n  pair {a} vs {b}:")
        assigned = False
        for inst in HIERARCHY:
            ma, mb = scored.get((inst, a)), scored.get((inst, b))
            pfa, pfb = frames.get((inst, a)), frames.get((inst, b))
            if not ma or not mb or pfa is None or pfb is None:
                print(f"    {INSTRUMENT_NAMES[inst]:<14} PENDING")
                continue
            # decisive-or-zero = the PRE-REGISTERED pairwise read: paired EVENT
            # sign test on shared frames (EXP-DIST-68 protocol). The v1 composer
            # used unpaired mutual CI-exclusion and wrongly called diff5's
            # far-argmax gap DECISIVE (settled p=0.549) — caught by validating
            # against the settled factorial before refereeing Phase 2.
            verdicts = []
            common = sorted(set(pfa) & set(pfb))
            for mi, k in ((0, "ceil"), (1, "arg")):
                a_only = [g for g in common if pfa[g][mi] and not pfb[g][mi]]
                b_only = [g for g in common if pfb[g][mi] and not pfa[g][mi]]
                ea, eb = len(_events(a_only)), len(_events(b_only))
                p = _sign_p(ea, ea + eb)
                decisive = p < 0.05
                verdicts.append((k, decisive, ea, eb, p))
            dec = [k for k, d, *_ in verdicts if d]
            line = "  ".join(
                f"{k}:{'DECISIVE' if d else 'zero'}(ev{ea}v{eb},p={p:.2f})"
                for k, d, ea, eb, p in verdicts
            )
            print(f"    {INSTRUMENT_NAMES[inst]:<14} {line}")
            if dec and not assigned:
                print(f"    >>> first decisive row: {INSTRUMENT_NAMES[inst]} ({dec})")
                assigned = True
        if not assigned:
            print(
                "    >>> no decisive detector row: pattern 4 (nothing separates) unless a human/viewport row decides"
            )


if __name__ == "__main__":
    main()
