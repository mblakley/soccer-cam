r"""Multi-signal game-phase detector (half-length AGNOSTIC).

Fuses three signals that each don't assume a half length, picking the most precise per boundary and
cross-checking by relative timing (half-length range is only a sanity gate):
  * PLAYER curve  - yolo26n person detections inside the field polygon, sampled ~1 frame/step sec.
    Low->high(1H)->low(HALFTIME: field empties)->high(2H)->low. Halftime = the longest sustained
    low-player run mid-game; brackets HT and the 2H region. (the backbone)
  * BALL restart  - from the AutoCam ball sidecar (<video>.mp4.jsonl, per-frame {t,xy}): the ball
    sits static at the CENTER CIRCLE then moves = a kickoff. KO = first center restart; 2H = first
    center restart after the halftime dip. (precise KO/2H, ~0-5s)
  * WHISTLE       - STFT at the ref's pitch; multi-blast (>=2 in 5s) = halftime/full-time. HT and
    END snap to multi-blasts. Needs Nyquist > ~4.4kHz (44.1kHz trimmed audio; 16kHz combined cuts
    the whistle, so those games fall back to player+ball only).

Per-game compute (player curve + whistle STFT + ball parse) is cached to <cache>/<gid>.json so the
expensive pass runs once; segmentation/fusion re-runs are ~instant. Writes game_state source
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
import cv2
import numpy as np
import onnxruntime as ort

REG = os.environ.get("GAME_REGISTRY", r"F:\training_data\game_registry.json")
SS = os.environ.get("SOCCERCAM_STORAGE", r"D:\soccer-cam-storage")
M = os.environ.get("YOLO_PERSON_MODEL", r"G:\pipeline_work\eval\yolo26n.onnx")
CACHE = os.environ.get("PHASE_CACHE", r"G:\ballresearch\phase_cache")
WRITE = "--write" in sys.argv
FORCE = "--force" in sys.argv
RECOMPUTE = "--recompute" in sys.argv  # ignore cached curve/whistle, recompute
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
PLAY_THR = 5  # >= this many in-field persons => play
reg = json.load(open(REG, encoding="utf-8"))

_SESS = None


def sess():
    global _SESS
    if _SESS is None:
        _SESS = ort.InferenceSession(
            M, providers=["DmlExecutionProvider", "CPUExecutionProvider"]
        )
    return _SESS


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


# ---------- player-on-field curve ----------
def persons(frame, thr=0.30):
    h, w = frame.shape[:2]
    r = min(1280 / h, 1280 / w)
    nh, nw = int(round(h * r)), int(round(w * r))
    canvas = np.full((1280, 1280, 3), 114, np.uint8)
    top, left = (1280 - nh) // 2, (1280 - nw) // 2
    canvas[top : top + nh, left : left + nw] = cv2.resize(frame, (nw, nh))
    blob = canvas[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0
    s = sess()
    out = s.run(None, {s.get_inputs()[0].name: blob})[0][0]
    res = []
    for row in out:
        x1, y1, x2, y2, conf, cls = row[:6]
        if conf < thr or int(round(cls)) != 0:
            continue
        res.append(((x1 - left) / r, (y1 - top) / r, (x2 - left) / r, (y2 - top) / r))
    return res


def _count_in(ps, polyf):
    return sum(
        1
        for x1, y1, x2, y2 in ps
        if cv2.pointPolygonTest(polyf, (float((x1 + x2) / 2), float(y2)), False) >= 0
    )


def player_curve(path, polyf, rot, step):
    """Decode ~1 frame / step sec, count in-field persons. Returns (times[], counts[], dur).

    Orientation is AUTO-DETECTED, not taken from video_rotation: the field_polygon is upright, but
    the combined video may be raw (camera mounted inverted) OR already rotated, and the dahua
    archive's video_rotation is inconsistent (raw files tagged rot=0, rotated files too). For the
    first frames we count in-field persons BOTH ways and commit to whichever orientation wins."""
    c = av.open(path)
    vs = c.streams.video[0]
    vs.thread_type = "AUTO"  # 4K panorama files are decode-bound; use all cores
    dur = float(vs.duration * vs.time_base) if vs.duration else None
    if not dur:
        dur = float(c.duration / 1e6) if c.duration else 0.0
    ts, cnt = [], []
    flip = None  # None = still deciding; True = rotate 180 to make upright
    vote = [0, 0]  # cumulative in-field persons [as-decoded, 180-rotated]
    t = 2.0
    while t < dur - 1:
        try:
            c.seek(int(t / vs.time_base), stream=vs, backward=True)
        except Exception:
            pass
        fr = None
        for f in c.decode(video=0):
            if f.time is not None and f.time >= t - 0.6:
                fr = f.to_ndarray(format="bgr24")
                break
        if fr is None:
            break
        if flip is None:  # decide orientation from the first populated frames
            a = _count_in(persons(fr), polyf)
            b = _count_in(persons(fr[::-1, ::-1].copy()), polyf)
            vote[0] += a
            vote[1] += b
            nin = max(a, b)
            if vote[0] + vote[1] >= 40 or len(ts) >= 16:
                flip = vote[1] > vote[0]
        else:
            frame = fr[::-1, ::-1].copy() if flip else fr
            nin = _count_in(persons(frame), polyf)
        ts.append(t)
        cnt.append(nin)
        t += step
    c.close()
    return np.array(ts), np.array(cnt, np.float32), dur


def smooth(a, k=5):
    if len(a) < k:
        return a
    pad = k // 2
    ap = np.pad(a, pad, mode="edge")
    return np.array([np.median(ap[i : i + k]) for i in range(len(a))])


def segment(ts, cnt):
    """Half-length agnostic. Halftime = the longest SUSTAINED low-player run in the middle of the
    game (field empties). 1H = first-play -> halftime; 2H = halftime -> last-play. Returns the
    onset/offset of each half in video time (refined to whistles/ball later).

    Never returns None when there IS play: some (esp. youth) games don't clearly empty the field at
    halftime, so when no mid-game dip is found we still return a structure (midpoint placeholder +
    empty dips) and let the whistle/ball fusion locate halftime. Only a total absence of detected
    play -> None."""
    n = len(cnt)
    sm = smooth(cnt, 3)  # light denoise of single-frame yolo misses
    dur = ts[-1]
    play = [k for k in range(n) if sm[k] >= 4]
    if not play:
        return None  # no sustained play detected at all -> unusable
    on1, off2 = ts[play[0]], ts[play[-1]]
    low = sm <= 2  # field essentially empty
    runs, i = [], 0
    while i < n:
        if low[i]:
            j = i
            while j < n and low[j]:
                j += 1
            runs.append((i, j - 1))
            i = j
        else:
            i += 1
    mid = [
        (a, b)
        for a, b in runs
        if (b - a + 1) >= 3 and 0.20 * dur <= ts[a] <= 0.80 * dur
    ]
    # all candidate mid-game player dips (onset,offset) — fusion picks the halftime one by the
    # multi-whistle that precedes it, not by length alone (a spurious long dip != halftime).
    dips = [(float(ts[a]), float(ts[b])) for a, b in mid]
    if mid:
        a_ht, b_ht = max(
            mid, key=lambda r: r[1] - r[0]
        )  # longest mid low-run = halftime
        off1 = (ts[a_ht - 1] + ts[a_ht]) / 2 if a_ht > 0 else ts[a_ht]
        on2 = (ts[b_ht] + ts[b_ht + 1]) / 2 if b_ht + 1 < n else ts[b_ht]
    else:  # field never clearly emptied -> placeholder; fusion leans on whistle/ball for HT
        off1 = on2 = (on1 + off2) / 2
    return on1, off1, on2, off2, sm, dips


# ---------- whistle refinement ----------
def whistle_blasts(path):
    c = av.open(path)
    if not c.streams.audio:
        c.close()
        return [], [], 0  # no audio stream (some dahua combined videos) -> no whistles
    sr = c.streams.audio[0].rate or 44100
    if sr < 9000:
        c.close()
        return [], [], sr  # audio too low-rate for whistle (~4.3kHz)
    buf = []
    try:
        for fr in c.decode(audio=0):
            try:
                a = fr.to_ndarray()
            except Exception:
                continue
            if a.ndim > 1:
                a = a.mean(axis=0)
            buf.append(a)
    except Exception:
        pass
    c.close()
    if not buf:
        return [], [], sr
    audio = np.concatenate(buf).astype(np.float32)
    win, hop = 1024, 512
    freqs = np.fft.rfftfreq(win, 1.0 / sr)
    hann = np.hanning(win).astype(np.float32)
    nf = (len(audio) - win) // hop + 1
    b18 = (freqs >= 1000) & (freqs <= 8000)
    msel = (freqs >= 2000) & (freqs <= 5500)
    mfreqs = freqs[msel]
    peakf = np.zeros(nf, np.float32)
    ton = np.zeros(nf, np.float32)
    loud = np.zeros(nf, np.float32)
    for i in range(nf):
        sp = np.abs(np.fft.rfft(audio[i * hop : i * hop + win] * hann))
        f0 = mfreqs[int(np.argmax(sp[msel]))]
        band = (freqs >= f0 - 250) & (freqs <= f0 + 250)
        ton[i] = sp[band].sum() / (sp[b18].sum() + 1e-9)
        peakf[i] = f0
        loud[i] = sp[band].sum()
    tframes = np.arange(nf) * hop / sr
    tf = peakf[(ton > 0.4) & (loud > np.percentile(loud, 80))]
    if len(tf) < 3:
        return [], [], sr
    hist, edges = np.histogram(tf, bins=np.arange(3000, 4900, 100))
    pitch = edges[int(np.argmax(hist))] + 50
    active = (
        (ton > 0.35) & (np.abs(peakf - pitch) < 300) & (loud > np.percentile(loud, 75))
    )
    out, i = [], 0
    while i < nf:
        if active[i]:
            j = i
            while j < nf and active[j]:
                j += 1
            if tframes[j - 1] - tframes[i] >= 0.08:
                out.append(tframes[i])
            i = j
        else:
            i += 1
    blasts = []
    for t in out:
        if not blasts or t - blasts[-1] > 0.4:
            blasts.append(t)
    ev = []
    for t in blasts:
        if ev and t - ev[-1][2] <= 5.0:
            ev[-1][1] += 1
            ev[-1][2] = t
        else:
            ev.append([t, 1, t])
    multis = [e[0] for e in ev if e[1] >= 2]
    return blasts, multis, sr


def snap(cands, target, tol):
    c = [x for x in cands if abs(x - target) < tol]
    return min(c, key=lambda x: abs(x - target)) if c else None


def ball_restarts(base, gj=None, poly_src=None):
    """Ball restart events (ball static ~1.5s then moving off) + center spot, on the game timeline.
    Returns (events[(t,x,y)], center_xy); center = densest restart-spot cluster. poly_src is the
    upright field polygon in source px (used to auto-detect dahua detection orientation).

    Source priority, robust to header/short lines + missing keys (never raises):
      1) reolink Once-native <video>.mp4.jsonl with {t, xy} (already on the game timeline, seconds);
      2) dahua-segment games: autocam_detections.jsonl {seg, f, x, y, conf} (per-segment frame,
         raw space) -> global seconds via segment global_offset/fps, made upright by the auto-
         detected 180 flip; ball_track.jsonl (empty for these games) is the fallback."""
    T, X, Y = [], [], []
    # --- path 1: {t, xy} global-seconds sidecar (reolink) ---
    for f in glob.glob(
        os.path.join(base, "**", "*.mp4.jsonl"), recursive=True
    ) + glob.glob(os.path.join(base, "**", "*.mkv.jsonl"), recursive=True):
        for ln in open(f, encoding="utf-8"):
            if '"xy"' not in ln:
                continue
            try:
                r = json.loads(ln)
            except Exception:
                continue
            t, xy = r.get("t"), r.get("xy")
            if t is None or not xy:
                continue
            T.append(float(t))
            X.append(float(xy[0]))
            Y.append(float(xy[1]))
        if len(T) >= 100:
            break
    # --- path 2: per-segment dahua detections {seg,f,x,y(,conf)} -> global seconds, made upright ---
    # AutoCam ran on the raw (inverted) video so its x,y are in RAW space; field_polygon is upright
    # => rotate 180 when video_rotation is +/-180. Primary source autocam_detections.jsonl (raw
    # per-frame ball candidates, deduped to the max-conf detection per frame); ball_track.jsonl
    # (empty for these games) is the fallback. Maps {seg,f} -> global sec via global_offset/fps.
    if len(T) < 100 and gj:
        segs0 = gj.get("segments") or []
        segmap = {
            s.get("seg"): (
                float(s.get("global_offset", 0)),
                float(s.get("fps") or 25.0),
            )
            for s in segs0
        }
        Wg = int(segs0[0]["w"]) if segs0 else 0
        Hg = int(segs0[0]["h"]) if segs0 else 0
        det = os.path.join(base, "autocam_detections.jsonl")
        bt = os.path.join(base, "ball_track.jsonl")
        src = (
            det
            if (os.path.exists(det) and os.path.getsize(det) > 0)
            else (bt if os.path.exists(bt) else None)
        )
        if src:
            best = {}  # (seg,f) -> (x,y,conf), keep the highest-conf detection per frame
            for ln in open(src, encoding="utf-8"):
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                seg, fr, x, y = r.get("seg"), r.get("f"), r.get("x"), r.get("y")
                if seg not in segmap or fr is None or x is None or y is None:
                    continue
                conf = float(r.get("conf", 1.0))
                if (
                    conf < 0.3
                ):  # drop low-confidence noise (autocam dumps candidates down to 0.05)
                    continue
                k = (seg, fr)
                if k not in best or conf > best[k][2]:
                    best[k] = (float(x), float(y), conf)
            # auto-detect orientation: rotate 180 iff that puts more detections in the upright field
            flip = False
            if best and poly_src is not None and len(poly_src) and Wg and Hg:
                pf = poly_src.reshape(-1, 1, 2).astype(np.float32)
                raw_in = rot_in = 0
                for x, y, conf in best.values():
                    if cv2.pointPolygonTest(pf, (x, y), False) >= 0:
                        raw_in += 1
                    if cv2.pointPolygonTest(pf, (Wg - 1 - x, Hg - 1 - y), False) >= 0:
                        rot_in += 1
                flip = rot_in > raw_in
            T, X, Y = [], [], []
            for (seg, fr), (x, y, conf) in best.items():
                off, fps = segmap[seg]
                if flip:
                    x, y = Wg - 1 - x, Hg - 1 - y
                T.append((off + float(fr)) / fps)
                X.append(x)
                Y.append(y)
            if T:  # frames may be out of order in the file -> sort by global time
                order = np.argsort(T)
                T = list(np.asarray(T)[order])
                X = list(np.asarray(X)[order])
                Y = list(np.asarray(Y)[order])
    if len(T) < 100:
        return None, None
    T, X, Y = np.array(T, float), np.array(X, float), np.array(Y, float)
    n = len(T)
    fps = n / T[-1] if T[-1] else 20.0
    SW = MW = max(10, int(1.5 * fps))
    ev, i = [], SW
    while i < n - MW:
        xs, ys = X[i - SW : i], Y[i - SW : i]
        if xs.max() - xs.min() < 25 and ys.max() - ys.min() < 25:
            sx, sy = xs.mean(), ys.mean()
            if (
                not (abs(sx - 3840) < 3 and abs(sy - 1080) < 3)
                and np.hypot(X[i : i + MW] - sx, Y[i : i + MW] - sy).max() > 180
            ):
                ev.append((float(T[i]), sx, sy))
                i += int(5 * fps)
                continue
        i += 1
    if not ev:
        return [], None
    pts = np.array([(x, y) for _, x, y in ev])
    best = max(
        (
            (np.sum(np.hypot(pts[:, 0] - px, pts[:, 1] - py) < 160), px, py)
            for px, py in pts
        ),
        key=lambda c: c[0],
    )
    return ev, (best[1], best[2])


def mmss(s):
    return "%d:%05.2f" % (int(s) // 60, s % 60)


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
        ball_ev, bcenter = cc.get("ball_ev"), cc.get("ball_center")
    else:
        ts, cnt, dur = player_curve(vid, polyf, rot, STEP)
        blasts, multis, sr = whistle_blasts(vid)
        ball_ev, bcenter = ball_restarts(
            d, gj, poly
        )  # ball restart events + center spot (upright source px)
        json.dump(
            {
                "ts": ts.tolist(),
                "cnt": cnt.tolist(),
                "dur": dur,
                "blasts": blasts,
                "multis": multis,
                "sr": sr,
                "fw": fw,
                "fh": fh,
                "step": STEP,
                "ball_ev": ball_ev,
                "ball_center": bcenter,
            },
            open(cpath, "w", encoding="utf-8"),
        )
    blasts = blasts or []  # guard no-audio games (cache may hold null from older runs)
    multis = multis or []
    seg = segment(ts, cnt)
    if not seg:
        print("  [%s] no-play-plateau (frame=%dx%d dur=%.0fs)" % (gid, fw, fh, dur))
        continue
    on1, off1, on2, off2, sm, dips = seg
    dur = float(ts[-1])
    # ---- FUSION by the boundary signatures (each cross-checks the others) ----
    #   KO  : single whistle, then a center ball-restart (static ball -> moves)
    #   HT  : MULTI-whistle, then the players leave the field (a player dip FOLLOWS it)
    #   2H  : single whistle, then the next center ball-restart
    #   END : late MULTI-whistle, after which the ball stops + players leave
    sig = []
    # field centroid in px. Center-circle restarts (kickoffs recur there) sit near centroid-x in the
    # far half; cxp/cyp feed the signature-driven kickoff detector below. The x-tolerance is generous
    # (the center circle is offset from the centroid under panorama distortion); absolute px works
    # across the scored resolutions.
    cxp = cyp = None
    if ball_ev:
        cxp, cyp = float(poly[:, 0].mean()), float(poly[:, 1].mean())

    def mstr(m):  # multi-blast strength = # of whistle blasts in its 5s cluster
        return sum(1 for b in blasts if m <= b <= m + 5)

    def pre_single(
        t, tol=10
    ):  # the single whistle just before event t (the restart whistle)
        c = [b for b in blasts if t - tol <= b <= t + 2]
        return max(c) if c else None

    # WIDE multi-whistles: re-cluster the cached `blasts` with a wider gap than the 5s `multis`. A
    # halftime double/triple-blow can be spread (~10s) or so tight it merged (<0.4s) into one blast;
    # the 5s clustering misses both, so the spot-on HT whistle never registered as a multi. Chain
    # blasts while the gap <= WIDE_GAP; a chain of >=2 is a wide multi (its time = the first blast).
    # Recomputed here from cache so no audio re-decode is needed.
    WIDE_GAP = 11.0
    wev = []
    for b in blasts:
        if wev and b - wev[-1][1] <= WIDE_GAP:
            wev[-1][1] = b
            wev[-1][2] += 1
        else:
            wev.append([b, b, 1])
    wmultis = [e[0] for e in wev if e[2] >= 2]

    # HALFTIME: a multi-whistle that a player dip FOLLOWS (players leave after the whistle), nearest
    # the game centre. Centrality (halves are ~equal) rejects spurious water-break dips; the
    # following-dip requirement rejects loud crowd multis that aren't halftime.
    ht = ht_dip = None
    central_multis = [m for m in multis if 0.30 * dur <= m <= 0.68 * dur]
    htc = [
        (m, d)
        for m in central_multis
        for d in [next((dp for dp in dips if m - 45 <= dp[0] <= m + 240), None)]
        if d
    ]
    if htc:
        ht, ht_dip = min(htc, key=lambda md: abs(md[0] - dur / 2))
        sig.append("whistle")
    elif not dips and central_multis:
        # field never clearly emptied (youth games) but there ARE central multi-whistles: halftime
        # and the 2nd-half kickoff are a PAIR of central multis a break (3-18min) apart, straddling
        # game centre. Pick the EARLIER of the best such pair as HT (HT precedes 2H); else the
        # earliest central multi. Gated on no-dips so games with a real dip are unaffected.
        prov_ko = next((b for b in blasts if b >= 20), 0.0)  # first whistle ~ kickoff
        prov_end = max(multis) if multis else off2  # last multi ~ full-time
        pairs = [
            (a, b)
            for a in central_multis
            for b in central_multis
            if 3 * 60 <= b - a <= 18 * 60
        ]
        best = (
            min(pairs, key=lambda p: abs((p[0] - prov_ko) - (prov_end - p[1])))
            if pairs
            else None
        )
        if best and abs((best[0] - prov_ko) - (prov_end - best[1])) < 5 * 60:
            # a SYMMETRIC HT/2H pair (1H = HT-KO ~ 2H = END-2Hkick) => confident HT = its earlier
            # whistle. If the best pair is still asymmetric, only one of HT/2H was detected as a
            # multi (the other wind-masked), so a pair is unreliable -> use the central multi.
            ht = best[0]
        else:
            ht = min(central_multis, key=lambda m: abs(m - dur / 2))
        ht_dip = (ht, ht)
        sig.append("whistle")
    if ht is None:
        # No central 5s-multi with a following dip registered (the HT whistle was spread/tight so it
        # never clustered as a 5s multi, or there was no central one). Anchor HT to the WHISTLE that
        # PRECEDES the halftime field-empty dip: youth players take up to ~3min to clear the field, so
        # the dip onset LAGS the whistle and must not itself be HT (the old 60s snap was too narrow).
        # Look back from the halftime dip onset for a wide multi, else the nearest single blast.
        ht_dip = (off1, on2)
        dip_on = off1
        if dips:
            # halftime dip = the longest mid low-run; a brief mid-break refill can split it into
            # adjacent runs, so extend backward over runs separated by only a short gap and use the
            # cluster's earliest onset (the moment the field actually started to empty).
            hi_i = max(range(len(dips)), key=lambda i: dips[i][1] - dips[i][0])
            lo_i = hi_i
            while lo_i > 0 and dips[lo_i][0] - dips[lo_i - 1][1] <= 60:
                lo_i -= 1
            dip_on = dips[lo_i][0]
        lo, hi = dip_on - 180, dip_on + 30
        wmw = [m for m in wmultis if lo <= m <= hi]
        bw = [b for b in blasts if lo <= b <= hi]
        if wmw:
            ht = min(
                wmw, key=lambda m: abs(m - dip_on)
            )  # wide multi nearest before the dip
            sig.append("whistle")
        elif bw:
            ht = max(bw)  # the single blast nearest before the dip onset
            sig.append("whistle")
        else:
            ht = (snap(multis, off1, 120) or off1) if blasts else off1
            sig.append("player")
    on2 = ht_dip[1]

    # ---- KICKOFFS: signature-driven, applied throughout the video ----
    # A KICKOFF = a CENTER static-ball restart that ALSO has a single kickoff whistle just before it
    # AND/OR a full field of players. We detect every such event, then assign the first before HT to
    # KO and the first in the post-HT break to 2H. This replaces the old symmetric-halves prior +
    # "first center restart in window" pick (whose 20s whistle floor lost early kickoffs).
    def pcount_at(t):
        i = min(bisect.bisect_left(list(ts), float(t)), len(sm) - 1)
        return sm[i]

    # a real kickoff has BOTH teams on the field; a one-team restart (opponent still walking on) has
    # only ~half. Gate "full field" RELATIVE to this game's normal in-play crowd, not an absolute
    # floor — one-team restarts cleared the old PLAY_THR=5 and were mis-called as KO/2H.
    med_play = (
        float(np.median([x for x in sm if x >= 4]))
        if any(x >= 4 for x in sm)
        else float(PLAY_THR)
    )
    kicks = []
    for et, ex, ey in ball_ev or []:
        if not (abs(ex - cxp) < 350 and ey < cyp):  # must be a CENTER restart
            continue
        w = pre_single(et, 12)  # single whistle just before
        full = (
            pcount_at(et) >= 0.6 * med_play
        )  # both teams on (relative to this game's crowd)
        if w is None and not full:  # need a corroborator beyond "center"
            continue
        # tuple: (time, has_whistle, full, ball_event_time, whistle_time). The kick TIME is the
        # whistle (when present, == the moment play is signalled) else the ball restart; et and w are
        # kept so kickoff() can test how tightly the whistle couples to the ball.
        kicks.append((w if w is not None else et, bool(w), bool(full), et, w))
    kicks.sort()

    def is_multi(
        w,
    ):  # whistle w belongs to a multi-blast cluster (2H "ready"/HT/END double-blow)
        return w is not None and any(m - 1 <= w <= m + 5 for m in multis)

    def kickoff(k):
        """A real kickoff restart: full field AND its whistle (if any) is a genuine start signal — a
        lone single whistle several seconds BEFORE the ball moves is positioning/noise, not a kickoff
        (this is the spurious pre-kickoff restart that put 05.30/05.31 2H ~50-80s early). A real
        kickoff whistle is tight to the kick (<=3s) or part of the 2H ready multi; a full restart with
        no detected whistle stays eligible (some kickoff whistles are wind-masked, e.g. 06.10 2H)."""
        _, has_w, full, et, w = k
        if not full:
            return False
        return w is None or abs(w - et) <= 3.0 or is_multi(w)

    if DEBUG:  # (kick_time, has_whistle, full, kickoff?, in-field count, ball_ev time)
        print(
            "      kicks:",
            [
                (
                    mmss(k[0]),
                    k[1],
                    k[2],
                    kickoff(k),
                    round(float(pcount_at(k[0])), 1),
                    mmss(k[3]),
                )
                for k in kicks
            ],
            "med_play=%.1f" % med_play,
        )

    # 2H: the first real KICKOFF (full field + a tight/multi whistle or none — see kickoff()) in the
    # post-halftime break window; else first whistle there; else HT + a typical break. Computed before
    # KO so KO's symmetric fallback can use it. kickoff() rejects the spurious pre-kickoff restart
    # (a lone whistle a few seconds before the ball moves) that put 05.30/05.31 2H ~50-80s early.
    MIN_BREAK, MAX_BREAK = 3 * 60, 18 * 60
    s2k = [k for k in kicks if ht + MIN_BREAK <= k[0] <= ht + MAX_BREAK and kickoff(k)]
    # a no-whistle restart immediately followed (<=75s) by a whistled full kickoff = the first was a
    # warm-up restart and the whistle marks the real 2H kickoff -- prefer the whistled one (05.27 2H:
    # no-whistle restart 54:04 then whistle+full+center 55:04, real 2H 55:04). Mirrors the KO nxt_ko
    # rule. Only reorders within the break window; if no whistled kickoff follows, s2k[0] is unchanged.
    s2k0 = s2k[0] if s2k else None
    if s2k0 is not None and not s2k0[1]:
        nxt2 = next((k for k in s2k if k[1] and s2k0[0] < k[0] <= s2k0[0] + 75), None)
        if nxt2 is not None:
            s2k0 = nxt2
    w2 = [b for b in blasts if ht + MIN_BREAK <= b <= ht + MAX_BREAK]
    # the 2nd-half kickoff WHISTLE = the first whistle once the field has refilled (HT dip offset on2).
    # When the ball model misses the real 2H restart (no center restart at the kickoff), this whistle
    # is the only signal there; a no-whistle ball restart re-acquired later in the half must NOT
    # outrank it. So trust a ball-restart kick only if it is whistle-corroborated, or there is no
    # refill whistle, or the kick is not far (<=90s) after that whistle.
    w2_refill = next((b for b in w2 if b >= on2 - 30), None)
    use_s2k = bool(s2k0 and (s2k0[1] or w2_refill is None or s2k0[0] <= w2_refill + 90))
    if use_s2k:
        sh = s2k0[0]
        sig.append("kick")
    elif w2_refill is not None:
        sh = w2_refill
        sig.append("whistle")
    elif w2:
        sh = w2[0]
        sig.append("whistle")
    else:
        sh = ht + 8 * 60
        sig.append("order")

    # END: the full-time whistle. Prefer a late multi-blow; but the final whistle often collapses to a
    # SINGLE blast ("twit..twit.....tweeeeeeeeet" merges into one event) and players can stay on the
    # field after it (so the player-curve last-play `off2` overshoots toward end-of-file). The final
    # whistle is the LAST whistle of the game -- refs don't whistle after full-time -- so when no late
    # multi registered, anchor END to the last blast in the valid 2H window (06.08 END: GT 93:57, one
    # merged final-whistle blast at 93:59, players stayed on -> off2 sat at 99:12 = end-of-file, +315s).
    end_blasts = [b for b in blasts if sh + 15 * 60 <= b <= off2 + 240]
    if multis:
        late = [m for m in multis if m >= sh + 15 * 60 and m <= off2 + 240]
        end = (
            late[-1]
            if late
            else (end_blasts[-1] if end_blasts else (snap(blasts, off2, 120) or off2))
        )
        if "whistle" not in sig:
            sig.append("whistle")
    elif end_blasts:
        end = end_blasts[-1]
        if "whistle" not in sig:
            sig.append("whistle")
    else:
        end = off2

    # KO: assign the first pre-halftime kickoff signature. A kickoff whistle right at the start must
    # NOT be lost to a floor (the old >=20s floor lost West_Seneca's 0:11 kickoff), so the whistle
    # fallback uses >=5s. The symmetric equal-halves estimate (KO = HT - 2nd-half-length) is the
    # prior used to reject pre-game warm-up restarts (a CENTER static ball + full field also occurs
    # during warm-up, well before the real kickoff).
    firstw = next((b for b in blasts if b >= 5), None) if blasts else None
    ko_sym = max(0.0, ht - (end - sh))
    # kickoff candidates before halftime: full-field restarts OR center restarts with a TIGHTLY
    # coupled whistle (<=3s) AND a MODERATE field (>=0.4x this game's normal crowd). A whistle that
    # fires the instant a center static ball moves is a kickoff even when the in-field count is
    # under-detected at the opening kickoff (06.08 KO at 1:59: real center restart 1:57 + whistle 1:59
    # had only 5 in-field detections < the full-field gate, so it was dropped and KO jumped to a 4:42
    # warm-up restart). The 0.4x floor still rejects an early warm-up whistle over a near-empty field
    # (05.31 Spencerport: a coincidental tight whistle at 0:30 over just 2 players is NOT the kickoff;
    # the real KO is the full restart at 1:45).
    prek = [
        k
        for k in kicks
        if k[0] < ht - 60
        and (
            k[2]
            or (
                k[4] is not None
                and abs(k[4] - k[3]) <= 3.0
                and pcount_at(k[3]) >= 0.4 * med_play
            )
        )
    ]
    if prek:
        kt, kw = prek[0][0], prek[0][1]
        # a whistle+full kickoff within ~75s AFTER the first full restart = the warm-up restart was
        # immediately followed by the real kickoff whistle; trust that kickoff over the no-whistle
        # restart (06.06-S: first full restart 3:54 no-whistle, real KO whistle+full 4:25, 31s later).
        nxt_ko = next(
            (k[0] for k in kicks if k[1] and k[2] and kt < k[0] <= kt + 75), None
        )
        if kw:
            ko = kt  # whistle + full + center => a clean kickoff; trust it
        elif nxt_ko is not None:
            ko = nxt_ko  # no-whistle restart shortly followed by a whistle+full kickoff => kickoff
        elif firstw is not None and kt < firstw <= kt + 90:
            ko = firstw  # warm-up restart shortly followed by the kickoff whistle => the whistle
        elif abs(kt - ko_sym) <= 75:
            ko = kt  # first full restart matches the symmetric prior => it is the kickoff
        else:
            # first full restart is far from the prior => warm-up; prefer a whistle+full kickoff near
            # the prior, else the first whistle of the game, else the symmetric estimate itself.
            wfull = [k[0] for k in kicks if k[1] and k[2] and k[0] < ht - 60]
            near = [t for t in wfull if abs(t - ko_sym) <= 120]
            if near:
                ko = min(near, key=lambda t: abs(t - ko_sym))
            elif firstw is not None:
                ko = firstw
            else:
                ko = ko_sym
        sig.append("kick")
    elif firstw is not None:
        ko = firstw
        sig.append("whistle")
    else:
        ko = ko_sym
        sig.append("sym")
    sig = list(dict.fromkeys(sig))
    used = "+".join(sig) + ("(sr=%d)" % sr if blasts else "")
    h1, h2 = (ht - ko) / 60, (end - sh) / 60
    brkm = (sh - ht) / 60
    # sanity gate: reject implausible structures (never write garbage)
    reasons = []
    if ko < -2:
        reasons.append("KO<0")
    if not (2.5 <= brkm <= 18):
        reasons.append("break=%.1f" % brkm)
    if not (15 <= h2 <= 50):
        reasons.append("h2=%.1f" % h2)
    if abs(h1 - h2) > 3:
        reasons.append("asym=%.1f" % abs(h1 - h2))
    if end <= sh or sh <= ht or ht <= ko:
        reasons.append("order")
    ok = not reasons
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
        "ko_from_ball": bool(prek),
        "sh_from_ball": use_s2k,
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
