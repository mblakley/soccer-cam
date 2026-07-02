r"""Training CLI for the multi-signal game-phase detector.

The detector core (signals + fusion) lives in video_grouper.inference.phase_detector; this script is
the training-box driver. It walks the game registry, resolves each game's video + field polygon +
trim offset, loads-or-computes the three per-game signals with a JSON cache (<cache>/<gid>.json) so
the expensive pass runs once (segmentation/fusion re-runs are ~instant), scores the fit
(<gid>.fit.json, with the trim offset applied), and -- with --write -- writes game_state source
"phase_fused" + _phase_meta. The sanity gate rejects implausible fits (never writes garbage).

Paths default to the training-box layout (override via env). Usage:
  phase_detect.py [--write] [--force] [--recompute] [--team T] [--game GID] [--step N] [--debug]
"""

import bisect
import configparser
import glob
import json
import os
import sys

import av
import numpy as np

try:
    from video_grouper.inference.phase_detector import (
        compute_signals,
        fuse_phases,
        mmss,
        whistle_blasts,
    )
except ImportError:  # box-scratch: core co-located flat in the same dir
    from phase_detector import (
        compute_signals,
        fuse_phases,
        mmss,
        whistle_blasts,
    )

REG = os.environ.get("GAME_REGISTRY", "")
SS = os.environ.get("SOCCERCAM_STORAGE", "")
CACHE = os.environ.get("PHASE_CACHE", "")
WRITE = "--write" in sys.argv
FORCE = "--force" in sys.argv
RECOMPUTE = "--recompute" in sys.argv  # ignore cached curve/whistle, recompute
# --recompute-whistle: re-run ONLY whistle_blasts (cheap audio re-decode) and merge into the cache,
# keeping the slow cached player-curve + ball signals. For rolling out a whistle-detection change
# (e.g. populating the loudness gate's per-blast ratios) without a full --recompute.
RECOMPUTE_WHISTLE = "--recompute-whistle" in sys.argv
# --predict: compute the fit + dump <gid>.fit.json for ALL games (even human-verified) but NEVER
# write game_state. For scoring the current detector against human GT without touching it.
PREDICT = "--predict" in sys.argv
DEBUG = "--debug" in sys.argv
os.makedirs(CACHE, exist_ok=True)
TEAM = sys.argv[sys.argv.index("--team") + 1] if "--team" in sys.argv else None
ONLY = sys.argv[sys.argv.index("--game") + 1] if "--game" in sys.argv else None
STEP = float(sys.argv[sys.argv.index("--step") + 1]) if "--step" in sys.argv else 12.0
# Game selection. Default = ALL games (dahua + reolink); the per-game loop handles dahua
# (16kHz combined audio -> no whistle, falls back to player+ball; ball via ball_track.jsonl).
#   --reolink-only : legacy behaviour, whistle-capable reolink games only
#   --gt-only      : only games with human-verified game_state (cheap coverage/--predict runs)
REOLINK_ONLY = "--reolink-only" in sys.argv
GT_ONLY = "--gt-only" in sys.argv
reg = json.load(open(REG, encoding="utf-8"))


def vd(g):
    p = g.get("path")
    return (os.path.dirname(p) if os.path.isfile(p) else p) if p else None


def files_offsets(base):
    """The F: archive folder (game.json's dir) is canonical: it holds the named-subdir trimmed
    upload (+ -raw sibling), combined.mp4, and match_info.ini. Search there, not D: storage."""
    trim = raw = None
    for sub in glob.glob(os.path.join(base, "*")):
        if os.path.isdir(sub):
            for f in glob.glob(os.path.join(sub, "*.mp4")):
                if "raw" in os.path.basename(f).lower():
                    raw = f
                else:
                    trim = f
    comb = os.path.join(base, "combined.mp4")
    comb = comb if os.path.exists(comb) else None
    off = 0.0
    mi = os.path.join(base, "match_info.ini")
    if os.path.exists(mi):
        cp = configparser.ConfigParser()
        try:
            cp.read(mi)
            s = cp.get("MATCH", "start_time_offset", fallback="0")
            parts = [int(x) for x in s.split(":")]
            off = parts[0] * 60 + parts[1] if len(parts) == 2 else float(s)
        except Exception:
            off = 0.0
    return trim, raw, comb, off


def find_fullframe_video(base, gj):
    """Largest full-frame combined mp4 in a dahua archive folder. game.json's combined_video is
    frequently missing or points at a slow/odd derivative across the 2024 archive, so pick the
    biggest base-level .mp4 that is NOT a per-segment ch1 file and NOT the cropped once-processed
    output. Orientation is sorted out later by player_curve's auto-detect."""
    segnames = {s.get("seg") for s in (gj.get("segments") or [])}
    best, bestsz = None, 0
    for f in glob.glob(os.path.join(base, "*.mp4")):
        b = os.path.basename(f).lower()
        stem = os.path.splitext(os.path.basename(f))[0]
        if stem in segnames or "_ch1" in b:
            continue
        if "once-processed" in b or "viewport" in b or "detections" in b:
            continue
        sz = os.path.getsize(f)
        if sz > bestsz:
            best, bestsz = f, sz
    return best if bestsz > 1e9 else None


def _has_human_gt(g):
    d = vd(g)
    gjp = os.path.join(d, "game.json") if d else None
    if not gjp or not os.path.exists(gjp):
        return False
    try:
        gs = json.load(open(gjp, encoding="utf-8")).get("game_state") or []
    except Exception:
        return False
    return any(p.get("source") == "human" for p in gs)


games = list(reg)
if REOLINK_ONLY:
    games = [g for g in games if str(g.get("video_format", "")).startswith("reolink")]
if GT_ONLY:
    games = [g for g in games if _has_human_gt(g)]
if TEAM:
    games = [g for g in games if g.get("team") == TEAM]
if ONLY:
    games = [g for g in reg if g["game_id"] == ONLY]
games.sort(key=lambda g: g["game_id"])
print(
    "games: %d (reolink_only=%s gt_only=%s) WRITE=%s FORCE=%s STEP=%ss"
    % (len(games), REOLINK_ONLY, GT_ONLY, WRITE, FORCE, STEP)
)

for g in games:
    gid = g["game_id"]
    d = vd(g)
    gjp = os.path.join(d, "game.json") if d else None
    if not gjp or not os.path.exists(gjp):
        print("  [%s] no game.json" % gid)
        continue
    gj = json.load(open(gjp, encoding="utf-8"))
    existing = gj.get("game_state") or []
    esrcs = sorted({p.get("source", "?") for p in existing})
    # human-verified GT is sacred: never overwrite it, even with --force/--game.
    # --predict bypasses the skips (to refresh .fit.json) but never writes game_state (below).
    if existing and "human" in esrcs and not PREDICT:
        print("  [%s] SKIP (human-verified GT)" % gid)
        continue
    if existing and not (FORCE or ONLY or PREDICT):
        print("  [%s] SKIP (game_state source=%s)" % (gid, ",".join(esrcs)))
        continue
    trim, raw, comb, off = files_offsets(d)  # d = F: archive folder (game.json's dir)
    vid = trim or raw or comb
    if (
        not vid
    ):  # dahua flat layout: game.json combined_video, else largest full-frame mp4
        cv = gj.get("combined_video")
        vid = cv if (cv and os.path.exists(cv)) else find_fullframe_video(d, gj)
    if not vid:
        print("  [%s] no video file in %s" % (gid, d))
        continue
    # combined.mp4 starts at recording start (off=0, includes warm-up); trim uses match_info offset
    voff = off if vid == trim else 0.0
    vkind = "trim" if vid == trim else ("raw" if vid == raw else "combined")
    seg0 = gj["segments"][0]
    W, H = int(seg0["w"]), int(seg0["h"])
    rot = int(gj.get("video_rotation", 0) or 0)
    poly = np.array(gj.get("field_polygon") or [], dtype=np.float32)
    if not len(poly):
        print("  [%s] no field_polygon" % gid)
        continue
    if poly.max() <= 1.5:
        poly = poly * np.array([W, H], np.float32)
    # scale polygon to actual frame size (trimmed uploads are downscaled/anamorphic)
    c = av.open(vid)
    vs = c.streams.video[0]
    fw, fh = vs.codec_context.width, vs.codec_context.height
    c.close()
    polyf = (
        (poly * np.array([fw / W, fh / H], np.float32))
        .reshape(-1, 1, 2)
        .astype(np.float32)
    )

    cpath = os.path.join(CACHE, gid + ".json")
    if os.path.exists(cpath) and not RECOMPUTE:
        cc = json.load(open(cpath, encoding="utf-8"))
        ts, cnt, dur = np.array(cc["ts"]), np.array(cc["cnt"], np.float32), cc["dur"]
        blasts, multis, sr = cc["blasts"], cc["multis"], cc["sr"]
        blast_loud = cc.get("blast_loud")
        ball_ev, bcenter = cc.get("ball_ev"), cc.get("ball_center")
        if (
            RECOMPUTE_WHISTLE
        ):  # re-run just the whistle signal; keep cached curve + ball
            blasts, multis, sr, blast_loud = whistle_blasts(vid)
            cc.update(
                {
                    "blasts": blasts,
                    "multis": multis,
                    "blast_loud": blast_loud,
                    "sr": sr,
                }
            )
            json.dump(cc, open(cpath, "w", encoding="utf-8"))
        signals = {
            "ts": ts,
            "cnt": cnt,
            "dur": dur,
            "blasts": blasts,
            "multis": multis,
            "blast_loud": blast_loud,
            "ball_ev": ball_ev,
            "ball_center": bcenter,
            "sr": sr,
        }
    else:
        signals = compute_signals(vid, polyf, rot, poly, STEP, base=d, gj=gj)
        ts, dur, sr = signals["ts"], signals["dur"], signals["sr"]
        json.dump(
            {
                "ts": signals["ts"].tolist(),
                "cnt": signals["cnt"].tolist(),
                "dur": dur,
                "blasts": signals["blasts"],
                "multis": signals["multis"],
                "blast_loud": signals["blast_loud"],
                "sr": sr,
                "fw": fw,
                "fh": fh,
                "step": STEP,
                "ball_ev": signals["ball_ev"],
                "ball_center": signals["ball_center"],
            },
            open(cpath, "w", encoding="utf-8"),
        )
    # fusion needs the upright source-px polygon for its center-circle reference (not cached; the
    # CLI recomputes poly from game.json each run, so inject it here for both cache + compute paths).
    signals["poly"] = poly
    ts, sr = signals["ts"], signals["sr"]

    fused = fuse_phases(signals, debug=DEBUG)
    if fused is None:
        print(
            "  [%s] no-play-plateau (frame=%dx%d dur=%.0fs)"
            % (gid, fw, fh, signals["dur"])
        )
        continue
    times = fused["times"]
    ko = times["kickoff"]
    ht = times["halftime"]
    sh = times["second_half"]
    end = times["end"]
    used = fused["used"]
    ok = fused["ok"]
    reasons = fused["reasons"]
    h1, h2, brkm = fused["h1"], fused["h2"], fused["brk"]
    sm, sig = fused["sm"], fused["sig"]
    dur = float(ts[-1])  # curve span (used for the fit-dump vdur, mirrors the detector)
    print(
        "  [%s] %s frame=%dx%d | KO=%s HT=%s 2H=%s END=%s | h1=%.1f h2=%.1f brk=%.1f %s"
        % (
            gid,
            used,
            fw,
            fh,
            mmss(max(0, ko)),
            mmss(ht),
            mmss(sh),
            mmss(end),
            h1,
            h2,
            brkm,
            "OK" if ok else "REJECT(" + ",".join(reasons) + ")",
        )
    )
    if DEBUG:
        print(
            "      curve(t:cnt):",
            " ".join(
                "%d:%d" % (int(ts[i]), int(sm[i]))
                for i in range(0, len(ts), max(1, len(ts) // 40))
            ),
        )
    json.dump(
        {
            "gid": gid,
            "ok": ok,
            "reasons": reasons,
            "used": used,
            "vid": vid,
            "voff": voff,
            "rot": rot,
            "fw": fw,
            "fh": fh,
            "times": {
                "kickoff": max(0.0, ko),
                "halftime": ht,
                "second_half": sh,
                "end": end,
            },
            "h1": round(h1, 1),
            "h2": round(h2, 1),
            # video span vs GT span: if they differ a lot the chosen video is incomplete or
            # multi-game (e.g. flash 06.01 -20min, 10.13 +76min), so its timeline can't map to GT.
            "vdur": round(float(dur), 1),
            "gt_dur": round(
                sum(int(s["frames"]) for s in gj["segments"])
                / (gj["segments"][0].get("fps") or 25.0),
                1,
            ),
        },
        open(os.path.join(CACHE, gid + ".fit.json"), "w", encoding="utf-8"),
    )
    if (
        not WRITE or not ok or PREDICT
    ):  # --predict dumps .fit.json above but never writes game_state
        continue
    ko = max(0.0, ko)
    off = voff
    segs = gj["segments"]
    offs = [int(s["global_offset"]) for s in segs]
    names = [s["seg"] for s in segs]
    frs = [int(s["frames"]) for s in segs]
    fps = segs[0].get("fps") or 25.0
    total = sum(frs)

    def g2sf(tt):
        gfr = max(0, min(total - 1, int(round((tt + off) * fps))))
        i = bisect.bisect_right(offs, gfr) - 1
        return [names[max(0, i)], int(gfr - offs[max(0, i)])]

    src = (
        "phase_fused"  # player-curve + whistle + ball restart (see _phase_meta.signals)
    )
    gj["game_state"] = [
        {"phase": "pre_game", "start": [names[0], 0], "end": g2sf(ko), "source": src},
        {"phase": "first_half", "start": g2sf(ko), "end": g2sf(ht), "source": src},
        {"phase": "halftime", "start": g2sf(ht), "end": g2sf(sh), "source": src},
        {"phase": "second_half", "start": g2sf(sh), "end": g2sf(end), "source": src},
        {
            "phase": "post_game",
            "start": g2sf(end),
            "end": [names[-1], frs[-1] - 1],
            "source": src,
        },
    ]
    gj["_phase_meta"] = {
        "source": src,
        "signals": sig,
        "half_min": [round(h1, 1), round(h2, 1)],
        "ko_from_ball": fused["ko_from_ball"],
        "sh_from_ball": fused["sh_from_ball"],
        "audio_sr": int(sr or 0),
        "step_sec": STEP,
        "times_sec": {
            "kickoff": round(ko, 1),
            "halftime": round(ht, 1),
            "second_half": round(sh, 1),
            "end": round(end, 1),
        },
    }
    tmp = gjp + ".tmp"
    json.dump(gj, open(tmp, "w", encoding="utf-8"), indent=1)
    os.replace(tmp, gjp)
print("DONE")
