r"""Flag games whose recording missed the kickoff and/or the final whistle, with a CLEAR,
differentiated representation (start vs end). Derived from the human GT boundaries.

game.json truncation fields (canonical representation — keep these consistent everywhere):
  truncated        bool  overall = (truncated_start OR truncated_end)
  truncated_start  bool  KICKOFF missing: recording started after KO, so first_half.start is
                         clamped to [seg0, 0] (0:00). The real kickoff is not in the video.
  truncated_end    bool  FINAL WHISTLE missing: recording ended before the end, so post_game.start
                         sits at ~full duration. The real end whistle is not in the video.
  truncated_reason str   human detail, comma-joined: "start@0:00" and/or "end@fulldur"

Detection (on the GT): kickoff < 15s => start-truncated; end > fulldur-15s => end-truncated.
Truncated games are EXCLUDED from train/val/eval because their clamped boundaries aren't real
whistles. Code that needs to tell start- from end-truncation should read the booleans, not parse
truncated_reason. Run after editing GT (e.g. clamping a KO to 0:00). Usage: trunc_flag.py [--write]
"""

import json
import os
import sys

WRITE = "--write" in sys.argv
REG = os.environ.get("GAME_REGISTRY", r"F:\training_data\game_registry.json")
# default reolink-only (the whistle-verified YT games); --all also scans dahua
ALL = "--all" in sys.argv
reg = json.load(open(REG, encoding="utf-8"))


def vd(g):
    p = g.get("path")
    return (os.path.dirname(p) if (p and os.path.isfile(p)) else p) if p else None


def mm(x):
    return "%d:%02d" % (int(x) // 60, int(x) % 60) if x is not None else "-"


games = sorted(
    [g for g in reg if ALL or str(g.get("video_format", "")).startswith("reolink")],
    key=lambda g: g["game_id"],
)
print("%-50s %7s %7s %7s  %s" % ("game", "KO", "END", "fulldur", "verdict"))
n = 0
for g in games:
    gid = g["game_id"]
    d = vd(g)
    gjp = os.path.join(d, "game.json") if d else None
    if not gjp or not os.path.exists(gjp):
        continue
    gj = json.load(open(gjp, encoding="utf-8"))
    ps = gj.get("game_state") or []
    if not ps:
        print("%-50s %7s %7s %7s  no game_state" % (gid[:50], "-", "-", "-"))
        continue
    segs = gj["segments"]
    fps = segs[0].get("fps", 20)
    offs = {s["seg"]: int(s["global_offset"]) for s in segs}
    full = sum(int(s.get("frames", 0)) for s in segs) / fps

    def gsec(sf):
        return (offs.get(sf[0], 0) + int(sf[1])) / fps

    ko = end = None
    for p in ps:
        if p["phase"] == "first_half":
            ko = gsec(p["start"])
        if p["phase"] == "post_game":
            end = gsec(p["start"])
    src = ",".join(sorted({p.get("source", "?") for p in ps}))
    ts = ko is not None and ko < 15  # kickoff missing
    te = end is not None and end > full - 15  # final whistle missing
    reasons = (["start@0:00"] if ts else []) + (["end@fulldur"] if te else [])
    trunc = bool(reasons)
    print(
        "%-50s %7s %7s %7s  %s [%s]"
        % (
            gid[:50],
            mm(ko),
            mm(end),
            mm(full),
            ("TRUNCATED(" + ",".join(reasons) + ")") if trunc else "ok",
            src,
        )
    )
    if WRITE:
        if trunc:
            gj["truncated"] = True
            gj["truncated_start"] = bool(ts)
            gj["truncated_end"] = bool(te)
            gj["truncated_reason"] = ",".join(reasons)
        else:
            for k in (
                "truncated",
                "truncated_start",
                "truncated_end",
                "truncated_reason",
            ):
                gj.pop(k, None)
        tmp = gjp + ".tmp"
        json.dump(gj, open(tmp, "w", encoding="utf-8"), indent=1)
        os.replace(tmp, gjp)
        n += 1
if WRITE:
    print("\nupdated %d game.json" % n)
else:
    print("\n(dry run; --write to flag, --all to include dahua)")
