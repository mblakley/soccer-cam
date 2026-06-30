r"""Score the phase detector against reference game_state, per transition, in seconds.

The iteration loop: phase_detect.py writes a per-game prediction to <PHASE_CACHE>/<gid>.fit.json
(trimmed-time boundaries + the trim offset). When Mark verifies a game in the phase editor it is
saved to game.json as game_state source="human" (ground truth, to within ~10s). This tool compares
the detector's prediction to that game_state and reports |error| per transition (kickoff, halftime,
2nd-half, end) so we can see exactly where the detector is off and iterate.

A game_state source of "human" is true GT; others (whistle_40min, play_windows, phase_fused) are
shown as reference. Summary reports mean/median/max error and the fraction of transitions within
10s (Mark's bar). Paths default to the training-box layout (override via env).

Usage: phase_eval.py [--human-only] [--game GID]
"""

import json
import os
import sys

REG = os.environ.get("GAME_REGISTRY", r"F:\training_data\game_registry.json")
CACHE = os.environ.get("PHASE_CACHE", r"G:\ballresearch\phase_cache")
HUMAN_ONLY = "--human-only" in sys.argv
ONLY = sys.argv[sys.argv.index("--game") + 1] if "--game" in sys.argv else None
TRANSITIONS = ["kickoff", "halftime", "second_half", "end"]
# game_state phase -> the transition that its .start marks
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
    """game_state [{phase,start:[seg,f],...}] -> {transition: global_seconds}."""
    segs = gj.get("segments") or []
    if not segs:
        return {}, None
    names = [s["seg"] for s in segs]
    offs = [int(s["global_offset"]) for s in segs]
    fps = segs[0].get("fps") or 25.0
    idx = {n: i for i, n in enumerate(names)}

    def gsec(sf):
        seg, f = sf
        i = idx.get(seg, 0)
        return (offs[i] + int(f)) / fps

    out, srcs = {}, set()
    for ph in gj.get("game_state") or []:
        t = PHASE_START.get(ph.get("phase"))
        if t:
            out[t] = gsec(ph["start"])
        srcs.add(ph.get("source", "?"))
    return out, ",".join(sorted(srcs))


def det_times(gid):
    """detector .fit.json -> {transition: global_seconds} (trimmed time + trim offset)."""
    fp = os.path.join(CACHE, gid + ".fit.json")
    if not os.path.exists(fp):
        return None
    fit = json.load(open(fp, encoding="utf-8"))
    voff = fit.get("voff", 0.0)
    vdur, gtd = fit.get("vdur"), fit.get("gt_dur")
    # misaligned: the video's GLOBAL span (voff .. voff+vdur) doesn't reach the GT recording span,
    # i.e. the chosen video is incomplete or multi-game, so its timeline can't map to GT -- a data
    # issue, not a detector error. (Must add voff: reolink trimmed uploads have vdur < gt_dur by the
    # trim offset, which voff corrects -- comparing vdur alone wrongly flags every reolink game.)
    mis = bool(vdur and gtd and abs(vdur + voff - gtd) > 120)
    return (
        {t: fit["times"][t] + voff for t in TRANSITIONS},
        fit.get("ok"),
        fit.get("used", ""),
        mis,
    )


def mmss(s):
    return "%d:%02d" % (int(s) // 60, int(s) % 60)


reg = json.load(open(REG, encoding="utf-8"))
if ONLY:
    reg = [g for g in reg if g["game_id"] == ONLY]
rows = []
misaligned = []
errs: dict[str, list[float]] = {t: [] for t in TRANSITIONS}
for g in sorted(reg, key=lambda g: g["game_id"]):
    gid = g["game_id"]
    d = vd(g)
    gjp = os.path.join(d, "game.json") if d else None
    if not gjp or not os.path.exists(gjp):
        continue
    gj = json.load(open(gjp, encoding="utf-8"))
    if not gj.get("game_state"):
        continue
    if gj.get("truncated") and "--include-truncated" not in sys.argv:
        continue  # truncated games (start@0:00 / end@fulldur) are excluded from train/val/eval
    ht, src = human_times(gj)
    if HUMAN_ONLY and "human" not in src:
        continue
    dt = det_times(gid)
    if not dt:
        continue
    dtimes, ok, used, mis = dt
    if mis and "--include-misaligned" not in sys.argv:
        misaligned.append(
            gid
        )  # video span != GT span (data issue) -> excluded from accuracy
        continue
    cells = []
    for t in TRANSITIONS:
        if t in ht and t in dtimes:
            e = dtimes[t] - ht[t]
            errs[t].append(abs(e))
            cells.append("%s%+ds" % (mmss(ht[t]), round(e)))
        else:
            cells.append("-")
    rows.append((gid, src, ok, cells))

print(
    "%-50s %-14s %-3s | %-13s %-13s %-13s %-13s"
    % ("game", "gt_source", "ok", *("%s(gt+err)" % t[:4] for t in TRANSITIONS))
)
print("-" * 130)
for gid, src, ok, cells in rows:
    print(
        "%-50s %-14s %-3s | %-13s %-13s %-13s %-13s"
        % (gid[:50], src[:14], "OK" if ok else "rej", *cells)
    )

print("\n==== per-transition error vs reference game_state ====")
allerr = []
for t in TRANSITIONS:
    e = errs[t]
    allerr += e
    if e:
        import statistics

        w10 = sum(1 for x in e if x <= 10)
        print(
            "  %-12s n=%2d  mean=%5.1fs  median=%5.1fs  max=%5.1fs  within10s=%d/%d"
            % (t, len(e), statistics.mean(e), statistics.median(e), max(e), w10, len(e))
        )
if allerr:
    import statistics

    w10 = sum(1 for x in allerr if x <= 10)
    print(
        "  %-12s n=%2d  mean=%5.1fs  median=%5.1fs  max=%5.1fs  within10s=%d/%d (%.0f%%)"
        % (
            "ALL",
            len(allerr),
            statistics.mean(allerr),
            statistics.median(allerr),
            max(allerr),
            w10,
            len(allerr),
            100 * w10 / len(allerr),
        )
    )
if misaligned:
    print(
        "\n%d games EXCLUDED (video span != GT span; data issue, not detector): %s"
        % (len(misaligned), ", ".join(misaligned))
    )
    print("  (re-include with --include-misaligned)")
