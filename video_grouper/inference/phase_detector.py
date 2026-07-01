r"""Multi-signal game-phase detector core (half-length AGNOSTIC).

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

This module is the delivered detector core (no CLI, no module-level execution, no I/O side effects):
``compute_signals`` runs the three per-game passes; ``fuse_phases`` segments + fuses them into phase
boundaries (in TRIMMED video time); ``detect_phases`` wires both together for a single video. The
sanity gate rejects implausible fits. Paths default to the training-box layout (override via env).
"""

import bisect
import glob
import json
import os

import av
import cv2
import numpy as np
import onnxruntime as ort

M = os.environ.get("YOLO_PERSON_MODEL", r"G:\pipeline_work\eval\yolo26n.onnx")
PLAY_THR = 5  # >= this many in-field persons => play
# END-whistle loudness gate: the full-time whistle must be at least this multiple of the p75 frame
# loudness. Wind resonating through the mic at the ref's pitch makes a faint, pitch-wandering
# post-game "multi" (06.06-S gusts ~8-11x p75) that the "last late multi" END rule wrongly picked
# over the loud (~30x) real whistle. Applied ONLY to END selection (a global gate over-prunes the
# quieter, game-variable KO/HT/2H whistles). From the cached per-blast ratios; env-tunable.
LOUD_GATE = float(os.environ.get("PHASE_LOUD_GATE", "12"))

# ---- game-block localization (untrimmed combined videos) ----
# A trimmed upload is the game (plus the coarse 4-min pre-kickoff backup the pipeline leaves in); a
# combined video is the full recording: a long warm-up -> game -> post-game, and the warm-up supplies
# stray whistles/ball-restarts that pull KO early. locate_game_block finds where the game body STARTS
# so the validated fusion runs anchored to it. The robust anchor is the KICKOFF WHISTLE: a warm-up
# whistle is followed by the field CLEARING for good as the teams line up (a sustained field-empty
# run), while the kickoff whistle is followed by continuous play. So the KO whistle = the first blast
# NOT immediately followed by a sustained clear. The clear must be LONG (>= LONG_CLEAR, a real lineup
# clear) so a brief player-detection gap does not count -- the player curve is too noisy at the head
# (slow/distant starts, dropouts) to anchor on, only to read the clear signature. We trim only when an
# EARLIER warm-up blast is skipped AND the KO whistle is >= MIN_WARMUP in, so a trimmed upload (first
# whistle is the kickoff, no long clear after it) returns (0, dur) -- fusion byte-identical (the 53/63
# scorecard + fixtures unaffected). Thresholds are env-tunable.
LOCATE_CLEAR_WINDOW_SEC = float(
    os.environ.get("PHASE_BLOCK_CLEAR_WINDOW_SEC", "75")
)  # warm-up clear must START within this of the whistle
LOCATE_LONG_CLEAR_SEC = float(
    os.environ.get("PHASE_BLOCK_LONG_CLEAR_SEC", "120")
)  # a real pre-kickoff clear, not a detection gap or first-half stoppage
LOCATE_HEAD_MARGIN_SEC = float(
    os.environ.get("PHASE_BLOCK_HEAD_MARGIN_SEC", "20")
)  # keep the KO whistle just inside the block
LOCATE_MIN_WARMUP_SEC = float(
    os.environ.get("PHASE_BLOCK_MIN_WARMUP_SEC", "240")
)  # KO whistle >= 4min in => untrimmed
LOCATE_MIN_BLOCK_SEC = float(
    os.environ.get("PHASE_BLOCK_MIN_SEC", "1500")
)  # >=25min game body
LOCATE_KO_FAR_SEC = float(
    os.environ.get("PHASE_BLOCK_KO_FAR_SEC", "90")
)  # KO far from block onset => untrustworthy

_SESS = None


def sess():
    global _SESS
    if _SESS is None:
        _SESS = ort.InferenceSession(
            M, providers=["DmlExecutionProvider", "CPUExecutionProvider"]
        )
    return _SESS


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
        return (
            [],
            [],
            0,
            [],
        )  # no audio stream (some dahua combined videos) -> no whistles
    sr = c.streams.audio[0].rate or 44100
    if sr < 9000:
        c.close()
        return [], [], sr, []  # audio too low-rate for whistle (~4.3kHz)
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
        return [], [], sr, []
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
        return [], [], sr, []
    hist, edges = np.histogram(tf, bins=np.arange(3000, 4900, 100))
    pitch = edges[int(np.argmax(hist))] + 50
    l75 = float(np.percentile(loud, 75))
    active = (ton > 0.35) & (np.abs(peakf - pitch) < 300) & (loud > l75)
    # each active run -> (start_time, peak-loudness ratio = max frame loudness / p75). The ratio lets
    # fusion drop faint wind-gust "whistles" (a gust resonating at the ref pitch is pitch-wandering
    # and only ~5-10x p75; a real whistle is ~14x+) -- see LOUD_GATE.
    out, i = [], 0
    while i < nf:
        if active[i]:
            j = i
            while j < nf and active[j]:
                j += 1
            if tframes[j - 1] - tframes[i] >= 0.08:
                out.append((tframes[i], float(loud[i:j].max() / (l75 + 1e-9))))
            i = j
        else:
            i += 1
    blasts, blast_loud = [], []
    for t, lr in out:
        if not blasts or t - blasts[-1] > 0.4:
            blasts.append(t)
            blast_loud.append(lr)
        else:
            blast_loud[-1] = max(
                blast_loud[-1], lr
            )  # loudest sub-blast of a merged cluster
    ev = []
    for t in blasts:
        if ev and t - ev[-1][2] <= 5.0:
            ev[-1][1] += 1
            ev[-1][2] = t
        else:
            ev.append([t, 1, t])
    multis = [e[0] for e in ev if e[1] >= 2]
    return blasts, multis, sr, blast_loud


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
                for x, y, _conf in best.values():
                    if cv2.pointPolygonTest(pf, (x, y), False) >= 0:
                        raw_in += 1
                    if cv2.pointPolygonTest(pf, (Wg - 1 - x, Hg - 1 - y), False) >= 0:
                        rot_in += 1
                flip = rot_in > raw_in
            T, X, Y = [], [], []
            for (seg, fr), (x, y, _conf) in best.items():
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
    return f"{int(s) // 60}:{s % 60:05.2f}"


def compute_signals(video_path, polyf, rot, poly_src, step, *, base=None, gj=None):
    """Run the three per-game passes and return the cacheable signal dict.

    Wraps player_curve + whistle_blasts + ball_restarts. ``polyf`` is the field polygon scaled to the
    decoded frame size (player-curve in-field test); ``poly_src`` is the upright field polygon in
    source px (ball-restart orientation auto-detect + fusion's center-circle reference). ``base`` is
    the archive folder ball_restarts searches for the ball sidecar (defaults to the video's own
    directory); ``gj`` is the dahua game.json enabling the per-segment detection path. Reolink /
    single-video callers leave gj=None and rely on the {t,xy} sidecar.

    Returns {"ts","cnt","dur","blasts","multis","blast_loud","ball_ev","ball_center","sr"} matching
    the on-disk cache schema. fuse_phases additionally reads a "poly" entry (the source-px polygon);
    callers inject it before fusing — see detect_phases."""
    if base is None:
        base = os.path.dirname(video_path)
    ts, cnt, dur = player_curve(video_path, polyf, rot, step)
    blasts, multis, sr, blast_loud = whistle_blasts(video_path)
    ball_ev, bcenter = ball_restarts(base, gj, poly_src)
    return {
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


def _empty_run_onsets(sm, ts, long_n):
    """Onset times of field-empty runs (sm <= 2) at least ``long_n`` samples long -- i.e. sustained
    field clears, not brief detection gaps. A pre-kickoff warm-up clear (both teams off while they
    line up) is one such run."""
    n = len(sm)
    out, i = [], 0
    while i < n:
        if sm[i] <= 2:
            j = i
            while j < n and sm[j] <= 2:
                j += 1
            if j - i >= long_n:
                out.append(float(ts[i]))
            i = j
        else:
            i += 1
    return out


def locate_game_block(signals):
    """Locate ``(block_start, block_end)`` in seconds = the game body inside an untrimmed combined
    video (warm-up -> game -> post-game), so the validated fusion runs anchored to the game and
    ignores the warm-up before kickoff (which supplies stray whistles/restarts that pull KO early).

    The anchor is the KICKOFF WHISTLE = the first blast NOT immediately followed by a sustained field
    clear (a warm-up whistle is followed by the field emptying as the teams line up; the kickoff
    whistle is followed by continuous play). A clear = a field-empty run of at least
    LOCATE_LONG_CLEAR_SEC whose onset falls within LOCATE_CLEAR_WINDOW_SEC after the blast -- long
    enough that a brief player-detection gap does NOT count as a clear (which is what made a strict
    2-sample test wrongly skip real kickoff whistles). ``block_start`` = that whistle minus a small
    margin, so it keeps the kickoff whistle while dropping earlier warm-up whistles/ball-restarts.

    We localize ONLY when an earlier (warm-up) blast is actually being skipped AND the kickoff whistle
    is at least LOCATE_MIN_WARMUP_SEC in. A cleanly-trimmed upload -- including the coarse 4-min
    pre-kickoff backup the pipeline leaves in (so its first whistle IS the kickoff, not followed by a
    long clear) -- returns ``(0, dur)``, so the fusion output is byte-identical (the 53/63 scorecard +
    fixtures are unaffected). No-whistle combined videos also return ``(0, dur)`` (the safety flag
    marks their KO untrustworthy instead). ``block_end`` is left at ``dur`` -- the post-game tail is
    handled by the existing END/off2 logic; trimming it risked cutting a real final whistle that
    follows a late field-empty stoppage. A degenerate/too-short block also falls back to ``(0, dur)``
    (never trims away a real game)."""
    ts = np.asarray(signals["ts"], dtype=float)
    if len(ts) < 6:
        return (0.0, float(ts[-1]) if len(ts) else 0.0)
    dur = float(ts[-1])
    blasts = signals.get("blasts") or []
    if not blasts:
        return (0.0, dur)  # no whistle -> can't anchor; the safety flag handles trust
    cnt = np.asarray(signals["cnt"], dtype=np.float32)
    sm = smooth(cnt, 3)
    step = float(ts[1] - ts[0]) if len(ts) > 1 else 12.0
    long_n = max(3, int(round(LOCATE_LONG_CLEAR_SEC / max(step, 1e-6))))
    clears = _empty_run_onsets(sm, ts, long_n)  # onsets of sustained field clears

    def cleared(b):  # a sustained clear starts within the window just after this blast
        return any(b < on <= b + LOCATE_CLEAR_WINDOW_SEC for on in clears)

    ko_i = next((i for i, b in enumerate(blasts) if not cleared(b)), None)
    # ko_i == 0: the first blast is already the kickoff (no earlier warm-up whistle) -> no-op.
    if ko_i is None or ko_i == 0 or blasts[ko_i] < LOCATE_MIN_WARMUP_SEC:
        return (0.0, dur)
    block_start = max(0.0, float(blasts[ko_i]) - LOCATE_HEAD_MARGIN_SEC)
    if block_start < 0.5 or (dur - block_start) < LOCATE_MIN_BLOCK_SEC:
        return (
            0.0,
            dur,
        )  # degenerate -> treat as trimmed (never trim away a real game)
    return (block_start, dur)


def _slice_signals(signals, t0, t1):
    """Restrict a signal dict to the window [t0, t1] with all times offset by -t0, so the validated
    fusion can run within the located game block. ``blasts`` and ``blast_loud`` are filtered together
    to stay index-aligned; ``multis``/``ball_ev`` times are shifted; spatial fields (poly, center),
    ``sr`` pass through unchanged. Callers add t0 back to the fused boundary times."""
    ts = np.asarray(signals["ts"], dtype=float)
    m = (ts >= t0) & (ts <= t1)
    out = dict(signals)
    out["ts"] = ts[m] - t0
    out["cnt"] = np.asarray(signals["cnt"], dtype=np.float32)[m]
    out["dur"] = float(t1 - t0)
    blasts = signals.get("blasts") or []
    bl = signals.get("blast_loud") or []
    if len(bl) == len(blasts):
        pairs = [
            (b - t0, lr) for b, lr in zip(blasts, bl, strict=True) if t0 <= b <= t1
        ]
        out["blasts"] = [p[0] for p in pairs]
        out["blast_loud"] = [p[1] for p in pairs]
    else:
        out["blasts"] = [b - t0 for b in blasts if t0 <= b <= t1]
        out["blast_loud"] = bl
    out["multis"] = [b - t0 for b in (signals.get("multis") or []) if t0 <= b <= t1]
    ev = signals.get("ball_ev")
    if ev:
        out["ball_ev"] = [(t - t0, x, y) for (t, x, y) in ev if t0 <= t <= t1]
    return out


def fuse_phases(signals, *, debug=False):
    """Locate the game block, then segment + fuse the (block-restricted) signals into phase
    boundaries in the ORIGINAL video timeline.

    Wraps the validated ``_fuse_core``: on an untrimmed combined video ``locate_game_block`` finds the
    game body, the signals are sliced to it, ``_fuse_core`` fuses in block-relative time, and the
    block offset is added back to every boundary. On a trimmed upload (game fills the file) the block
    is ``(0, dur)`` and ``_fuse_core`` runs on the original signals unchanged -- so trimmed behavior is
    byte-identical (the 53/63 scorecard + fixtures are unaffected). Adds two keys the S1 game-start
    resolver reads: ``block`` = [start, end], and ``ko_trustworthy`` -- False (fall back to NTFY, never
    mis-trim) when the KO is symmetric-prior-only, the combined video has no whistle, the KO is far
    from the block onset, or the fit was rejected. Returns None for a no-play plateau."""
    ts_full = np.asarray(signals["ts"], dtype=float)
    dur = float(ts_full[-1]) if len(ts_full) else 0.0
    t0, t1 = locate_game_block(signals)
    localized = t0 > 0.5 or t1 < dur - 0.5
    res = _fuse_core(
        _slice_signals(signals, t0, t1) if localized else signals, debug=debug
    )
    if res is None:
        return None
    if not localized:
        t0, t1 = 0.0, dur
    else:
        for k in res["times"]:
            res["times"][k] += t0
    res["block"] = [t0, t1]
    blasts = signals.get("blasts") or []
    ko = res["times"]["kickoff"]
    # "far from block onset" only means something once we've LOCALIZED (t0 = the game onset). On a
    # no-op (t0 = 0: trimmed, or a combined video whose first whistle is already the kickoff) t0 is
    # not the game onset, so this check is skipped -- the KO is still gated by no-whistle / symmetric
    # / the fusion sanity gate.
    far = localized and (ko - t0) > LOCATE_KO_FAR_SEC
    res["ko_trustworthy"] = bool(
        blasts and res.get("ko_anchor") != "sym" and not far and res["ok"]
    )
    return res


def _fuse_core(signals, *, debug=False):
    """Segment + fuse the cached signals into phase boundaries, in TRIMMED video time (no trim
    offset is applied here; callers add it at output/scoring time).

    ``signals`` is a compute_signals() dict plus a "poly" entry = the upright field polygon in source
    px (the center-circle reference for kickoff detection). Returns None for a no-play plateau (no
    sustained play detected), else a dict with the boundary times (kickoff un-clamped — callers clamp
    max(0, kickoff) at output), the sanity-gate verdict, and the fields the training CLI needs for its
    fit/meta dump."""
    ts = signals["ts"]
    cnt = signals["cnt"]
    blasts = signals["blasts"] or []  # guard no-audio games (cache may hold null)
    multis = signals["multis"] or []
    blast_loud = signals["blast_loud"] or []
    ball_ev = signals["ball_ev"]
    sr = signals["sr"]
    poly = signals.get("poly")
    if poly is not None:
        poly = np.asarray(poly, dtype=np.float32)
    # Per-multi peak loudness (max blast loudness ratio in its 5s cluster), used ONLY by the END
    # gate below. A global loudness gate over-prunes -- quieter real KO/HT/2H whistles vary a lot
    # game-to-game and dropping them wrecks accuracy (swept: a global 12x gate took reolink 52->44).
    # But wind resonating at the ref pitch makes a FAINT post-game "multi" (06.06-S gusts ~8-11x p75)
    # that the "last late multi" END rule wrongly picks over the loud (~30x) real full-time whistle.
    # So gate ONLY the END selection on per-multi loudness, with a fall-back to ungated (below).
    multi_loud: list = []
    if len(blast_loud) == len(blasts):
        ev2: list = []
        for t, lr in zip(blasts, blast_loud, strict=True):
            if ev2 and t - ev2[-1][1] <= 5.0:
                ev2[-1][1] = t
                ev2[-1][2] = max(ev2[-1][2], lr)
                ev2[-1][3] += 1
            else:
                ev2.append([t, t, lr, 1])  # [first_time, last_time, peak_loud, count]
        multi_loud = [(e[0], e[2]) for e in ev2 if e[3] >= 2]
    seg = segment(ts, cnt)
    if not seg:
        return None  # no-play plateau -> unusable
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

    if debug:  # (kick_time, has_whistle, full, kickoff?, in-field count, ball_ev time)
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
            f"med_play={med_play:.1f}",
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
    # Prefer the first whistle when the field is FULL again (both teams in kickoff formation,
    # >=0.6x median crowd): the plain "first whistle after on2" catches a stray warm-up whistle while
    # players are still trickling on (05.28: stray 48:06 at 5 in-field vs the real 2H 49:38 at 11 =
    # full). Fall back to the first whistle after on2 when no full-field whistle is found.
    # When the ball model misses the real 2H restart (no center restart at the kickoff), this whistle
    # is the only signal there; a no-whistle ball restart re-acquired later in the half must NOT
    # outrank it. Trust a ball-restart kick only if it is whistle-corroborated, there is no refill
    # whistle, or the kick is close after it -- and when the refill whistle is full-field-corroborated
    # (a confident kickoff signal) the kickoff ball moves within ~a minute, so a restart 86s later
    # (05.28 51:04) is a mid-half re-acquire (use the whistle) but a restart ~25s after a pre-kickoff
    # whistle (06.10 50:48 after 50:23) IS the kickoff (use the restart). Loose no-full case keeps 90s.
    w2_full = next(
        (b for b in w2 if b >= on2 - 30 and pcount_at(b) >= 0.6 * med_play), None
    )
    w2_refill = w2_full or next((b for b in w2 if b >= on2 - 30), None)
    override = 60 if w2_full is not None else 90
    use_s2k = bool(
        s2k0 and (s2k0[1] or w2_refill is None or s2k0[0] <= w2_refill + override)
    )
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
        end = late[-1] if late else None
        # TRAILING WIND-GUST guard: wind resonating at the ref pitch can fire a FAINT "multi" in the
        # minute AFTER the real full-time whistle (06.06-S: real 80:14 at ~30x, gust multis 80:51/81:03
        # at ~8-11x). If the last late multi is faint AND a LOUD multi (>=LOUD_GATE x p75) sits within
        # 120s before it, that loud multi is full-time and the faint trailer is wind -- use it. A loud
        # is NOT preferred over a quiet final whistle in general (faint real whistles exist in
        # wind-masked games and would be wrongly skipped); only a faint multi with a louder one right
        # before it is overridden, so quiet-final-whistle games are untouched.
        if late and multi_loud:
            ml = dict(multi_loud)
            loud_late = [
                m
                for (m, lr) in multi_loud
                if sh + 15 * 60 <= m <= off2 + 240 and lr >= LOUD_GATE
            ]
            if (
                ml.get(end, 999) < LOUD_GATE
                and loud_late
                and 0 < (end - loud_late[-1]) <= 120
            ):
                end = loud_late[-1]
        if end is None:
            end = end_blasts[-1] if end_blasts else (snap(blasts, off2, 120) or off2)
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
    # ko_anchor records what actually SET kickoff (not just the sig union): "kick"/"whistle" = a real
    # start signal in the video; "sym" = the symmetric equal-halves prior only (no whistle/kick to
    # anchor it). The S1 resolver treats a "sym" KO as not trustworthy for auto-trimming (03.21: a
    # no-whistle combined video whose KO falls to the prior and shifts 9 min late with ok=True).
    ko_anchor = "sym"
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
            ko_anchor = "kick"
        elif nxt_ko is not None:
            ko = nxt_ko  # no-whistle restart shortly followed by a whistle+full kickoff => kickoff
            ko_anchor = "kick"
        elif firstw is not None and kt < firstw <= kt + 90:
            ko = firstw  # warm-up restart shortly followed by the kickoff whistle => the whistle
            ko_anchor = "whistle"
        elif abs(kt - ko_sym) <= 75:
            ko = kt  # first full restart matches the symmetric prior => it is the kickoff
            ko_anchor = "kick"
        else:
            # first full restart is far from the prior => warm-up; prefer a whistle+full kickoff near
            # the prior, else the first whistle of the game, else the symmetric estimate itself.
            wfull = [k[0] for k in kicks if k[1] and k[2] and k[0] < ht - 60]
            near = [t for t in wfull if abs(t - ko_sym) <= 120]
            if near:
                ko = min(near, key=lambda t: abs(t - ko_sym))
                ko_anchor = "whistle"
            elif firstw is not None:
                ko = firstw
                ko_anchor = "whistle"
            else:
                ko = ko_sym
                ko_anchor = "sym"
        sig.append("kick")
    elif firstw is not None:
        ko = firstw
        ko_anchor = "whistle"
        sig.append("whistle")
    else:
        ko = ko_sym
        ko_anchor = "sym"
        sig.append("sym")
    sig = list(dict.fromkeys(sig))
    used = "+".join(sig) + (f"(sr={sr})" if blasts else "")
    h1, h2 = (ht - ko) / 60, (end - sh) / 60
    brkm = (sh - ht) / 60
    # sanity gate: reject implausible structures (never write garbage)
    reasons = []
    if ko < -2:
        reasons.append("KO<0")
    if not (2.5 <= brkm <= 18):
        reasons.append(f"break={brkm:.1f}")
    if not (15 <= h2 <= 50):
        reasons.append(f"h2={h2:.1f}")
    if abs(h1 - h2) > 3:
        reasons.append(f"asym={abs(h1 - h2):.1f}")
    if end <= sh or sh <= ht or ht <= ko:
        reasons.append("order")
    ok = not reasons
    return {
        "times": {
            "kickoff": ko,  # un-clamped; callers clamp max(0, ko) at output
            "halftime": ht,
            "second_half": sh,
            "end": end,
        },
        "ok": ok,
        "reasons": reasons,
        "used": used,
        "h1": h1,
        "h2": h2,
        "brk": brkm,
        "sm": sm,
        "sig": sig,
        "ko_anchor": ko_anchor,
        "ko_from_ball": bool(prek),
        "sh_from_ball": use_s2k,
    }


def detect_phases(video_path, field_polygon, *, ball_sidecar=None, rot=0, step=12.0):
    """End-to-end phase detection for a single video + field polygon.

    Scales the field polygon to the decoded frame size (mirroring the training CLI), runs
    compute_signals, then fuse_phases. ``field_polygon`` may be normalized (0..1) or in frame px;
    ``ball_sidecar`` overrides where the ball-restart {t,xy} sidecar is searched (defaults to the
    video's directory). Returns the fuse_phases dict (trimmed-time boundaries) or None for a no-play
    plateau. Single-video callers have no per-segment archive, so source px == frame px (polyf and
    poly_src coincide)."""
    poly = np.array(field_polygon or [], dtype=np.float32)
    if not len(poly):
        return None
    c = av.open(video_path)
    vs = c.streams.video[0]
    fw, fh = vs.codec_context.width, vs.codec_context.height
    c.close()
    if poly.max() <= 1.5:  # normalized -> source px (== frame px for a single video)
        poly = poly * np.array([fw, fh], np.float32)
    polyf = poly.reshape(-1, 1, 2).astype(np.float32)
    base = (
        os.path.dirname(ball_sidecar) if ball_sidecar else os.path.dirname(video_path)
    )
    signals = compute_signals(video_path, polyf, rot, poly, step, base=base)
    signals["poly"] = poly
    return fuse_phases(signals, debug=False)
