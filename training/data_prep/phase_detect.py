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


def player_curve(path, polyf, rot, step):
    """Decode ~1 frame / step sec, count in-field persons. Returns (times[], counts[], dur)."""
    c = av.open(path)
    vs = c.streams.video[0]
    dur = float(vs.duration * vs.time_base) if vs.duration else None
    if not dur:
        dur = float(c.duration / 1e6) if c.duration else 0.0
    ts, cnt = [], []
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
        if rot == 180:
            fr = fr[::-1, ::-1].copy()
        ps = persons(fr)
        nin = sum(
            1
            for x1, y1, x2, y2 in ps
            if cv2.pointPolygonTest(polyf, (float((x1 + x2) / 2), float(y2)), False)
            >= 0
        )
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
    onset/offset of each half in video time (refined to whistles later)."""
    n = len(cnt)
    sm = smooth(cnt, 3)  # light denoise of single-frame yolo misses
    dur = ts[-1]
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
    if not mid:
        return None
    a_ht, b_ht = max(mid, key=lambda r: r[1] - r[0])  # longest mid low-run = halftime
    play = [k for k in range(n) if sm[k] >= 4]
    if not play:
        return None
    on1 = ts[play[0]]
    off1 = (ts[a_ht - 1] + ts[a_ht]) / 2 if a_ht > 0 else ts[a_ht]
    on2 = (ts[b_ht] + ts[b_ht + 1]) / 2 if b_ht + 1 < n else ts[b_ht]
    off2 = ts[play[-1]]
    # all candidate mid-game player dips (onset,offset) — fusion picks the halftime one by the
    # multi-whistle that precedes it, not by length alone (a spurious long dip != halftime).
    dips = [(float(ts[a]), float(ts[b])) for a, b in mid]
    return on1, off1, on2, off2, sm, dips


# ---------- whistle refinement ----------
def whistle_blasts(path):
    c = av.open(path)
    if not c.streams.audio:
        c.close()
        return None, None, 0
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


def ball_restarts(base, gj=None):
    """Ball restart events (ball static ~1.5s then moving off) + center spot, on the game timeline.
    Returns (events[(t,x,y)], center_xy); center = densest restart-spot cluster.

    Source priority, robust to header/short lines + missing keys (never raises):
      1) reolink Once-native <video>.mp4.jsonl with {t, xy} (already on the game timeline, seconds);
      2) dahua-segment games: ball_track.jsonl with {seg, f, x, y} (per-segment frame) -> global
         seconds via game.json segment global_offset/fps. (The dahua <...>-once-processed.mp4.jsonl
         carries xy but no t, so path 1 yields nothing for it and we fall through to the track.)"""
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
    # --- path 2: per-segment ball track {seg, f, x, y} -> global seconds (dahua) ---
    if len(T) < 100 and gj:
        segmap = {
            s.get("seg"): (
                float(s.get("global_offset", 0)),
                float(s.get("fps") or 20.0),
            )
            for s in (gj.get("segments") or [])
        }
        bt = os.path.join(base, "ball_track.jsonl")
        if os.path.exists(bt):
            T, X, Y = [], [], []
            for ln in open(bt, encoding="utf-8"):
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                seg, fr, x, y = r.get("seg"), r.get("f"), r.get("x"), r.get("y")
                if seg not in segmap or fr is None or x is None or y is None:
                    continue
                off, fps = segmap[seg]
                T.append((off + float(fr)) / fps)
                X.append(float(x))
                Y.append(float(y))
            if T:  # segments may be out of order in the file -> sort by global time
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


games = [g for g in reg if str(g.get("video_format", "")).startswith("reolink")]
if TEAM:
    games = [g for g in games if g.get("team") == TEAM]
if ONLY:
    games = [g for g in reg if g["game_id"] == ONLY]
games.sort(key=lambda g: g["game_id"])
print("reolink games: %d WRITE=%s FORCE=%s STEP=%ss" % (len(games), WRITE, FORCE, STEP))

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
            d, gj
        )  # ball restart events + center spot (source px)
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
    # center-circle ball restarts (kickoffs recur there): near field-centroid-x, in the far half
    cen = []
    if ball_ev:
        cxp, cyp = float(poly[:, 0].mean()), float(poly[:, 1].mean())
        # tolerance vs the field centroid-x; generous (the center circle is offset from the
        # centroid under panorama distortion). Absolute px works across the scored resolutions.
        cen = sorted(e[0] for e in ball_ev if abs(e[1] - cxp) < 350 and e[2] < cyp)

    def mstr(m):  # multi-blast strength = # of whistle blasts in its 5s cluster
        return sum(1 for b in blasts if m <= b <= m + 5)

    def pre_single(
        t, tol=10
    ):  # the single whistle just before event t (the restart whistle)
        c = [b for b in blasts if t - tol <= b <= t + 2]
        return max(c) if c else None

    # HALFTIME: a multi-whistle that a player dip FOLLOWS (players leave after the whistle), nearest
    # the game centre. Centrality (halves are ~equal) rejects spurious water-break dips; the
    # following-dip requirement rejects loud crowd multis that aren't halftime.
    ht = ht_dip = None
    htc = [
        (m, d)
        for m in multis
        if 0.30 * dur <= m <= 0.68 * dur
        for d in [next((dp for dp in dips if m - 45 <= dp[0] <= m + 240), None)]
        if d
    ]
    if htc:
        ht, ht_dip = min(htc, key=lambda md: abs(md[0] - dur / 2))
        sig.append("whistle")
    if ht is None:  # fallback: longest dip itself, snapped to any nearby whistle
        ht_dip = (off1, on2)
        ht = (
            (snap(multis, off1, 120) or snap(blasts, off1, 60) or off1)
            if blasts
            else off1
        )
        sig.append("player")
    on2 = ht_dip[1]

    # ---- KO/2H derived from the phase ORDER + structure, anchored to the reliable HT/END ----
    # 2H: ordered AFTER halftime within a plausible break; first center restart in that window
    # (else first whistle there; else HT + a typical break). Rejects 1st-half + warm-up-return
    # restarts that wrecked the from-scratch pick.
    MIN_BREAK, MAX_BREAK = 3 * 60, 18 * 60
    s2 = [t for t in cen if ht + MIN_BREAK <= t <= ht + MAX_BREAK]
    if s2:
        sh = pre_single(s2[0]) or s2[0]
        sig.append("ball")
    elif blasts:
        w2 = [b for b in blasts if ht + MIN_BREAK <= b <= ht + MAX_BREAK]
        sh = w2[0] if w2 else ht + 8 * 60
        sig.append("whistle" if w2 else "order")
    else:
        sh = ht + 8 * 60
        sig.append("order")

    # END: the last late multi-whistle (full-time) after 2H; else last-play.
    if multis:
        late = [m for m in multis if m >= sh + 15 * 60 and m <= off2 + 240]
        end = late[-1] if late else (snap(blasts, off2, 120) or off2)
        if "whistle" not in sig:
            sig.append("whistle")
    else:
        end = off2

    # KO: the equal-halves symmetric estimate (KO = HT - 2nd-half-length) is the PRIOR; snap to the
    # nearest real center restart before HT (rejects warm-up, which is far from the prior). No nearby
    # restart -> first whistle of the game (no-ball games); none -> the symmetric estimate itself.
    ko_sym = max(0.0, ht - (end - sh))
    pre_ko = [t for t in cen if t < ht]
    cand = min(pre_ko, key=lambda t: abs(t - ko_sym)) if pre_ko else None
    near_r = [cand] if (cand is not None and abs(cand - ko_sym) < 300) else []
    fw = next((b for b in blasts if b >= 20), None) if blasts else None
    if near_r:
        ko = pre_single(near_r[0]) or near_r[0]
        if "ball" not in sig:
            sig.append("ball")
    elif fw is not None:
        ko = fw
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
        "ko_from_ball": bool(near_r),
        "sh_from_ball": bool(s2),
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
