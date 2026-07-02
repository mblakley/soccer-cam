r"""GT-simulation eval for parent event-tap phase anchors.

Real parent taps don't exist for the historical GT games (parents tag *live*
games in moment-tagger; these were tagged by Mark). So we validate the anchor
design by SIMULATION: for each GT game we synthesize realistic parent taps at
the true boundaries under several scenarios, re-fuse WITH vs WITHOUT the anchors
(``video_grouper.inference.event_tap_anchors`` + ``fuse_phases(anchors=...)``),
and measure the per-boundary error vs the human GT.

The bar (matches Mark's trust model):
  * a **trusted cluster** (>=2 parents agreeing) should IMPROVE the boundary,
    especially KO/END where the detector is weakest -- and never regress badly.
  * a **lone / scattered / wrong / confidently-offset** tap must NOT make things
    worse than the detector alone (the whole point of the low-confidence tier +
    snap-to-whistle).

Reuses the cached signals + GT that ``phase_detect.py`` / ``phase_eval.py`` use:
signals at ``<CACHE>/<gid>.json``, trim offset ``voff`` at ``<CACHE>/<gid>.fit.json``,
GT from ``game.json`` game_state source=human. ``times[t] + voff`` = GT global
seconds, so a tap for boundary b lands at ``GT[b] - voff`` in the video timeline.

Usage: phase_anchor_eval.py [--game GID] [--seed N] [--verbose]
Env: GAME_REGISTRY, PHASE_CACHE.
"""

import json
import os
import random
import statistics
import sys

import numpy as np

try:
    from video_grouper.inference.event_tap_anchors import (
        REACTION_LAG_S,
        build_anchors,
    )
    from video_grouper.inference.phase_detector import fuse_phases
except ImportError:  # box-scratch: core co-located flat in the same dir
    from event_tap_anchors import REACTION_LAG_S, build_anchors
    from phase_detector import fuse_phases

REG = os.environ.get("GAME_REGISTRY", "")
CACHE = os.environ.get("PHASE_CACHE", "")
ONLY = sys.argv[sys.argv.index("--game") + 1] if "--game" in sys.argv else None
SEED = int(sys.argv[sys.argv.index("--seed") + 1]) if "--seed" in sys.argv else 42
VERBOSE = "--verbose" in sys.argv
TRANSITIONS = ["kickoff", "halftime", "second_half", "end"]
BOUNDARY_LABEL = {  # detector boundary -> moment-tagger EventSyncPrompt label
    "kickoff": "kickoff",
    "halftime": "halftime_start",
    "second_half": "halftime_end",
    "end": "game_end",
}
PHASE_START = {
    "first_half": "kickoff",
    "halftime": "halftime",
    "second_half": "second_half",
    "post_game": "end",
}


def vd(g):
    p = g.get("path")
    return (os.path.dirname(p) if os.path.isfile(p) else p) if p else None


def human_times(gj):
    """game.json game_state (human) -> {transition: global_seconds}, source set."""
    segs = gj.get("segments") or []
    if not segs:
        return {}, ""
    offs = [int(s["global_offset"]) for s in segs]
    fps = segs[0].get("fps") or 25.0
    idx = {s["seg"]: i for i, s in enumerate(segs)}

    def gsec(sf):
        seg, f = sf
        return (offs[idx.get(seg, 0)] + int(f)) / fps

    out, srcs = {}, set()
    for ph in gj.get("game_state") or []:
        t = PHASE_START.get(ph.get("phase"))
        if t:
            out[t] = gsec(ph["start"])
        srcs.add(ph.get("source", "?"))
    return out, ",".join(sorted(srcs))


def load_signals(gid):
    cpath = os.path.join(CACHE, gid + ".json")
    fpath = os.path.join(CACHE, gid + ".fit.json")
    if not (os.path.exists(cpath) and os.path.exists(fpath)):
        return None, None, None
    cc = json.load(open(cpath, encoding="utf-8"))
    signals = {
        "ts": np.array(cc["ts"]),
        "cnt": np.array(cc["cnt"], np.float32),
        "dur": cc["dur"],
        "blasts": cc["blasts"],
        "multis": cc["multis"],
        "blast_loud": cc.get("blast_loud"),
        "ball_ev": cc.get("ball_ev"),
        "ball_center": cc.get("ball_center"),
        "sr": cc["sr"],
    }
    fit = json.load(open(fpath, encoding="utf-8"))
    return signals, float(fit.get("voff", 0.0)), fit


def _tap(boundary, video_time):
    """A synthesized parent tap in the VIDEO timeline (video_time_seconds path)."""
    return {
        "anchor_type": "event_tap",
        "label": BOUNDARY_LABEL[boundary],
        "video_time_seconds": float(video_time),
    }


# --- scenarios: GT (video-time per boundary) + rng -> list[tap] --------------
# ``ev[b]`` is the true event time in the video timeline. Parents tap ~REACTION_LAG_S
# late; the tap carries that (we do NOT pre-correct video_time_seconds), so the
# fusion must recover it (snap-to-whistle) or wear the small lag.


def scen_trusted_cluster(ev, rng):
    """3 parents agreeing within a few seconds at each boundary."""
    taps = []
    for b, t in ev.items():
        for _ in range(3):
            taps.append(_tap(b, t + REACTION_LAG_S + rng.uniform(-2.5, 2.5)))
    return taps


def scen_lone(ev, rng):
    """One parent taps each boundary (lowest-quality tier)."""
    return [_tap(b, t + REACTION_LAG_S + rng.uniform(-2, 2)) for b, t in ev.items()]


def scen_scattered(ev, rng):
    """3 parents but they disagree by >10s (no tight cluster -> low confidence)."""
    taps = []
    for b, t in ev.items():
        for k in (-1, 0, 1):
            taps.append(_tap(b, t + REACTION_LAG_S + k * rng.uniform(20, 45)))
    return taps


def scen_wrong_kickoff(ev, rng):
    """A trusted 'kickoff' cluster placed at the SECOND-HALF time (parent tapped
    the 2nd-half restart as the game kickoff). Safety test: must not drag KO to
    the 2nd half. Real boundaries otherwise untouched (kickoff only)."""
    if "second_half" not in ev:
        return []
    t = ev["second_half"]
    return [
        _tap("kickoff", t + REACTION_LAG_S + rng.uniform(-2.5, 2.5)) for _ in range(3)
    ]


def scen_confidently_offset(ev, rng):
    """A trusted cluster that AGREES but is ~40s off the true time (and not near a
    whistle). Tests the direct-use failure mode of a confidently-wrong cluster."""
    taps = []
    off = 40.0
    for b, t in ev.items():
        for _ in range(3):
            taps.append(_tap(b, t + off + rng.uniform(-2.5, 2.5)))
    return taps


SCENARIOS = {
    "trusted_cluster": scen_trusted_cluster,
    "lone": scen_lone,
    "scattered": scen_scattered,
    "wrong_kickoff": scen_wrong_kickoff,
    "confidently_offset": scen_confidently_offset,
}


def boundary_errors(times, voff, gt):
    """|detector_global - GT| per boundary that has GT."""
    out = {}
    for t in TRANSITIONS:
        if t in gt and t in times:
            out[t] = abs((times[t] + voff) - gt[t])
    return out


def main():
    reg = json.load(open(REG, encoding="utf-8"))
    if ONLY:
        reg = [g for g in reg if g["game_id"] == ONLY]
    # per (scenario, boundary): lists of (baseline_err, anchored_err)
    agg: dict = {s: {t: [] for t in TRANSITIONS} for s in SCENARIOS}
    n_games = 0
    for g in sorted(reg, key=lambda g: g["game_id"]):
        gid = g["game_id"]
        d = vd(g)
        gjp = os.path.join(d, "game.json") if d else None
        if not gjp or not os.path.exists(gjp):
            continue
        gj = json.load(open(gjp, encoding="utf-8"))
        gt, src = human_times(gj)
        if "human" not in src or not gt:
            continue
        signals, voff, _fit = load_signals(gid)
        if signals is None:
            continue
        signals["poly"] = np.array(gj.get("field_polygon") or [], dtype=np.float32)
        base = fuse_phases(signals)
        if base is None:
            continue
        n_games += 1
        base_err = boundary_errors(base["times"], voff, gt)
        # event time in the video timeline for each GT boundary
        ev = {t: gt[t] - voff for t in TRANSITIONS if t in gt}
        rng = random.Random(SEED + hash(gid) % 10000)
        for sname, sfn in SCENARIOS.items():
            taps = sfn(ev, rng)
            anchors = build_anchors(taps, None)
            res = fuse_phases(signals, anchors=anchors)
            a_err = boundary_errors(res["times"], voff, gt)
            for t in TRANSITIONS:
                if t in base_err and t in a_err:
                    agg[sname][t].append((base_err[t], a_err[t]))
            if VERBOSE and sname == "trusted_cluster":
                print(
                    "  [%s] base KO/END err %.0f/%.0f -> anchored %.0f/%.0f"
                    % (
                        gid[:40],
                        base_err.get("kickoff", -1),
                        base_err.get("end", -1),
                        a_err.get("kickoff", -1),
                        a_err.get("end", -1),
                    )
                )

    print("\nGT-simulation: %d games, seed=%d\n" % (n_games, SEED))
    print(
        "%-19s %-11s %6s %6s %6s %8s %8s"
        % ("scenario", "boundary", "base", "anch", "delta", "improved", "worse")
    )
    print("-" * 74)
    for sname in SCENARIOS:
        all_pairs = []
        for t in TRANSITIONS:
            pairs = agg[sname][t]
            all_pairs += pairs
            if not pairs:
                continue
            b = statistics.mean(p[0] for p in pairs)
            a = statistics.mean(p[1] for p in pairs)
            imp = sum(1 for x, y in pairs if y < x - 0.5)
            wor = sum(1 for x, y in pairs if y > x + 0.5)
            print(
                "%-19s %-11s %6.1f %6.1f %+6.1f %5d/%-2d %6d/%-2d"
                % (sname, t, b, a, a - b, imp, len(pairs), wor, len(pairs))
            )
        if all_pairs:
            b = statistics.mean(p[0] for p in all_pairs)
            a = statistics.mean(p[1] for p in all_pairs)
            wor = sum(1 for x, y in all_pairs if y > x + 0.5)
            maxw = max((y - x for x, y in all_pairs), default=0.0)
            print(
                "%-19s %-11s %6.1f %6.1f %+6.1f  worse=%d/%d maxΔ+%.0fs"
                % (sname, "ALL", b, a, a - b, wor, len(all_pairs), maxw)
            )
        print("-" * 74)


if __name__ == "__main__":
    main()
