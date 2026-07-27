"""Build the distillation ``games`` list for :func:`build_heatmap_crops` — per-frame **ball** labels
to train our homegrown ball *detector* (not a viewport model; the viewport is produced downstream by
the existing tracker consuming our detector's detections).

**Teacher signal (validated with Mark, 2026-06-30).** The label each frame is the ball position from
the **existing tracker** (``world_model.reranker.track_ball``) run over AutoCam's per-frame detection
candidates, anchored by the human GT, and **snapped to the real detection the tracker is following**
(the actual ball pixel that frame — we train a detector, so the target must be the ball, not a
Kalman-smoothed estimate). Frames with no detection backing the track (coasted / guessed) are
dropped; ``not_visible`` human frames are emitted with no ball.

Why this and not AutoCam's viewport or a raw argmax: measured on 1,880 human far-GT balls (the frames
AutoCam loses), the existing tracker over AutoCam detections lands within **R15 m 0.77** of the GT
(median 2.1 m) vs AutoCam's own viewport at **0.15** (median 41 m), and the ball is in the detection
candidate set 0.97 of the time. So the detections are good and the existing tracker turns them into a
ball track that beats AutoCam — the distillation just needs our detector to reproduce those
detections. Human ``ball`` frames override (anchoring through AutoCam's failures, so far balls are
trained from GT); every human label is kept, the dense tracked frames are subsampled.

Inputs per game come from the per-video JSONL store on F: (canonical per DECISIONS.md 2026-06-26):
``autocam_detections.jsonl`` (``{seg,f,x,y,conf}``, one row/candidate), ``game.json``
(``field_polygon``, ``segments[].global_offset``, ``video_rotation``) and optional ``ball_labels.jsonl``
(``{seg,f,a,p}``, ``a in {ball,not_visible,out_of_play}``). All coordinates are source px on the same
global-frame axis (``global = segment.global_offset + f``). ``field_edges`` / ``curve_depth`` remain
for the far-vs-near *evaluation* split (report where we beat AutoCam), not the teacher.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict

import numpy as np

# ---------------------------------------------------------------------------
# Segment / global-frame mapping
# ---------------------------------------------------------------------------


def seg_offsets(segments: list[dict]) -> dict[str, int]:
    """``{segment_name: global_offset}`` from ``game.json`` ``segments`` (global = offset + f)."""
    return {s["seg"]: int(s["global_offset"]) for s in segments}


def active_play_ranges(segments, game_state) -> list[tuple[int, int]]:
    """Global-frame ``[(lo, hi), ...]`` for the actual halves (``first_half``/``second_half``) from
    ``game.json`` ``game_state``. Used to drop warm-up / halftime / pre- & post-game frames, where
    AutoCam tracks players (not a game ball) — the dominant teacher-label noise. Returns ``[]`` when
    no phases are known (caller then keeps all frames).
    """
    offs = seg_offsets(segments)
    out: list[tuple[int, int]] = []
    for ph in game_state or []:
        if ph.get("phase") in ("first_half", "second_half"):
            s, e = ph.get("start"), ph.get("end")
            if s and e and s[0] in offs and e[0] in offs:
                out.append((offs[s[0]] + int(s[1]), offs[e[0]] + int(e[1])))
    return out


# ---------------------------------------------------------------------------
# Sidecar loaders (JSONL next to the video on F:)
# ---------------------------------------------------------------------------


def load_detections(jsonl_path, offsets: dict[str, int]) -> dict[int, list[tuple]]:
    """Parse ``autocam_detections.jsonl`` into ``{global_frame: [(x, y, conf), ...]}``.

    One row per candidate; rows are grouped by frame. ``offsets`` maps the per-segment ``f`` to the
    global decode index. Lines without ``x/y`` or with an unknown ``seg`` are skipped.
    """
    out: dict[int, list[tuple]] = defaultdict(list)
    with open(jsonl_path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith('{"_meta') or '"_meta"' in line[:12]:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "x" not in o or "y" not in o or o.get("seg") not in offsets:
                continue
            g = offsets[o["seg"]] + int(o["f"])
            out[g].append((float(o["x"]), float(o["y"]), float(o.get("conf", 0.0))))
    # keep each frame's candidates conf-sorted (high first)
    for g in out:
        out[g].sort(key=lambda c: c[2], reverse=True)
    return dict(out)


def load_viewport(
    jsonl_path, offsets: dict[str, int]
) -> dict[int, tuple[float, float]]:
    """Parse ``autocam_viewport.jsonl`` into ``{global_frame: (x, y)}`` (AutoCam's selected ball)."""
    out: dict[int, tuple[float, float]] = {}
    with open(jsonl_path) as fh:
        for line in fh:
            line = line.strip()
            if not line or '"_meta"' in line[:12]:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "x" not in o or "y" not in o or o.get("seg") not in offsets:
                continue
            out[offsets[o["seg"]] + int(o["f"])] = (float(o["x"]), float(o["y"]))
    return out


def parse_trim_offset_seconds(match_info_path) -> float | None:
    """``start_time_offset`` from ``match_info.ini`` (``[MATCH]``, ``mm:ss`` or
    ``hh:mm:ss``) as seconds, or ``None`` when the file/key is missing/empty.
    This is the head-trim the processed video applied to the combined timeline —
    the timebase the legacy AutoCam sidecars were recorded on (EXP-OP-13)."""
    import configparser
    from pathlib import Path

    p = Path(match_info_path)
    if not p.exists():
        return None
    cp = configparser.ConfigParser()
    try:
        cp.read(p, encoding="utf-8-sig")
    except configparser.Error:
        return None
    raw = cp.get("MATCH", "start_time_offset", fallback="").strip()
    if not raw:
        return None
    parts = raw.split(":")
    if not all(x.strip().isdigit() for x in parts) or len(parts) not in (2, 3):
        return None
    nums = [int(x) for x in parts]
    if len(nums) == 2:
        m, s = nums
        return float(m * 60 + s)
    h, m, s = nums
    return float(h * 3600 + m * 60 + s)


def load_viewport_trim_remapped(
    jsonl_path,
    offsets: dict[str, int],
    *,
    trim_seconds: float,
    fps_mean: float,
    anchors_x: dict[int, float],
    min_anchors: int = 100,
    min_pooled_r: float = 0.70,
    max_fit_drift: int = 60,
    fit_window: int = 400,
) -> tuple[dict[int, tuple[float, float]], dict]:
    """Load a LEGACY seg-keyed ``autocam_viewport.jsonl`` remapped onto the true
    (untrimmed) global timeline, verified against ball-GT anchors (EXP-OP-15).

    EXP-OP-13/15: the legacy jsonls were recorded on the TRIMMED timeline but
    carry untrimmed segment labels, so the naive ``load_viewport`` mapping
    decorrelates genuine AutoCam tracking. The true frame is
    ``t + D`` where ``t`` is the naive mapping and ``D = trim_seconds x fps``.
    The APPLIED offset is the predicted ``D`` — fitting D against sparse ball
    GT is biased by AutoCam's ~1 s follower lag (measured −32 fr on spc), so
    the fit only CONFIRMS the prediction.

    Verification gates (all ``ValueError`` — callers hard-fail per rule 8):
    - at least ``min_anchors`` ball-GT anchors overlap the legacy rows;
    - pooled Pearson r at the fitted offset ``>= min_pooled_r``
      (spc 0.81 / fair 0.98 pass; the naive mapping's ~0.2 fails);
    - the fitted offset (argmax r over ``D_pred +- fit_window``) lies within
      ``max_fit_drift`` of the prediction — alignment must CONFIRM the trim,
      not discover an unexplainable one.

    A per-segment r table (segments with >= 30 anchors) is returned in the meta
    for provenance. There is deliberately NO per-segment r floor: per-seg r
    against instantaneous ball GT is bounded by AutoCam's LOCAL tracking
    quality, not remap alignment (on spc seg9 the validated fresh aim itself
    scores r 0.18) — locally-bad AC is exactly what the composite's GT
    override tier handles.

    Returns ``({true_global: (x, y)}, meta)``.
    """
    naive = load_viewport(jsonl_path, offsets)
    if not naive:
        raise ValueError(f"legacy viewport matched no game.json segment: {jsonl_path}")
    d_pred = round(trim_seconds * fps_mean)

    t_max = max(naive) + 1
    leg = np.full(t_max, np.nan)
    for t, (x, _y) in naive.items():
        leg[t] = x
    gs = np.array(sorted(anchors_x))
    bx = np.array([anchors_x[g] for g in gs], dtype=np.float64)

    def pooled_r(d: int) -> tuple[float | None, int]:
        t = gs - d
        ok = (t >= 0) & (t < t_max)
        lx = leg[t[ok]]
        m = ~np.isnan(lx)
        n = int(m.sum())
        if n < min_anchors:
            return None, n
        a, b = lx[m], bx[ok][m]
        if np.std(a) == 0 or np.std(b) == 0:
            return None, n
        return float(np.corrcoef(a, b)[0, 1]), n

    r_pred, n_pred = pooled_r(d_pred)
    if r_pred is None:
        raise ValueError(
            f"legacy viewport remap NOT verifiable: {n_pred} usable ball-GT "
            f"anchors at D_pred={d_pred} (need >= {min_anchors}): {jsonl_path}"
        )
    fits: list[tuple[int, float]] = []
    for d in range(d_pred - fit_window, d_pred + fit_window + 1):
        r, _n = pooled_r(d)
        if r is not None:
            fits.append((d, r))
    d_fit, r_fit = max(fits, key=lambda x: x[1])
    if r_fit < min_pooled_r:
        raise ValueError(
            f"legacy viewport remap REJECTED: pooled r {r_fit:.3f} at fitted "
            f"offset {d_fit} < {min_pooled_r} over {n_pred} anchors — not the "
            f"genuine AutoCam signal on the predicted timeline: {jsonl_path}"
        )
    if abs(d_fit - d_pred) > max_fit_drift:
        raise ValueError(
            f"legacy viewport remap REJECTED: fitted offset {d_fit} drifts "
            f"{abs(d_fit - d_pred)} fr from the match_info prediction {d_pred} "
            f"(> {max_fit_drift}) — the alignment does not confirm the trim: "
            f"{jsonl_path}"
        )
    r_naive, _ = pooled_r(0)

    # per-segment r at the applied offset (provenance; no floor — see above)
    bounds = sorted(offsets.items(), key=lambda kv: kv[1])
    per_seg = []
    for i, (seg, lo) in enumerate(bounds):
        hi = bounds[i + 1][1] if i + 1 < len(bounds) else int(gs.max()) + 1
        sel = (gs >= lo) & (gs < hi)
        if int(sel.sum()) < 30:
            continue
        ts = gs[sel] - d_pred
        ok = (ts >= 0) & (ts < t_max)
        lx = leg[ts[ok]]
        m = ~np.isnan(lx)
        if int(m.sum()) < 30:
            continue
        a, b = lx[m], bx[sel][ok][m]
        r = float(np.corrcoef(a, b)[0, 1]) if np.std(a) > 0 and np.std(b) > 0 else None
        per_seg.append({"seg": seg, "n": int(m.sum()), "r": r})

    remapped = {t + d_pred: xy for t, xy in naive.items()}
    meta = {
        "trim_seconds": trim_seconds,
        "fps_mean": round(fps_mean, 3),
        "d_pred": int(d_pred),
        "d_fit": int(d_fit),
        "pooled_r": round(r_fit, 3),
        "pooled_r_at_pred": round(r_pred, 3),
        "pooled_r_naive": None if r_naive is None else round(r_naive, 3),
        "n_anchors": n_pred,
        "per_seg_r": per_seg,
    }
    return remapped, meta


def load_human_labels(
    jsonl_path, offsets: dict[str, int]
) -> tuple[dict[int, tuple[float, float]], set[int]]:
    """Parse ``ball_labels.jsonl`` into ``({global_frame: (x, y)} balls, {global_frame} not_visible)``.

    ``a == "ball"`` with a point ``p`` is a positive far/normal label; ``a in {not_visible,
    out_of_play}`` marks a frame with no findable ball (forces removal of any AutoCam pick there).
    """
    balls: dict[int, tuple[float, float]] = {}
    novis: set[int] = set()
    with open(jsonl_path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "_meta" in o or o.get("seg") not in offsets:
                continue
            g = offsets[o["seg"]] + int(o["f"])
            a = o.get("a")
            if a == "ball" and o.get("p") is not None:
                balls[g] = (float(o["p"][0]), float(o["p"][1]))
            elif a in ("not_visible", "out_of_play"):
                novis.add(g)
    return balls, novis


# ---------------------------------------------------------------------------
# Field-relative far classification (curve-following depth from the polygon)
# ---------------------------------------------------------------------------


def field_edges(polygon) -> tuple[np.ndarray, np.ndarray]:
    """Split the 10-point field polygon into ``(far_edge, near_edge)``, each ``(5, 2)`` sorted by x.

    The polygon is a perimeter loop: points 0-4 are one touchline, 5-9 the other. The **far**
    touchline is the one higher in the (corrected, upright) frame — smaller mean image-y — so this
    is correct regardless of the polygon's point ordering and on the flipped early-Dahua games
    (``video_rotation`` makes every frame upright before this runs, putting far at the top).
    """
    poly = np.asarray(polygon, dtype=np.float64)
    a, b = poly[:5], poly[5:10]
    far, near = (a, b) if a[:, 1].mean() <= b[:, 1].mean() else (b, a)
    return far[np.argsort(far[:, 0])], near[np.argsort(near[:, 0])]


def curve_depth(
    x: float, y: float, far_edge: np.ndarray, near_edge: np.ndarray
) -> float:
    """Field depth following the touchline curves: 0 = on the far touchline, 1 = on the near one.

    Interpolates the far- and near-edge curves at the ball's ``x`` (so the iso-depth boundary is a
    curve **parallel to the far touchline**, hugging the fisheye bow — not a flat horizontal line)
    and returns the ball's normalized position between them.
    """
    yf = float(np.interp(x, far_edge[:, 0], far_edge[:, 1]))
    yn = float(np.interp(x, near_edge[:, 0], near_edge[:, 1]))
    span = yn - yf
    if abs(span) < 1.0:
        return 0.5
    return float(np.clip((y - yf) / span, 0.0, 1.0))


def far_frac_from_squish(
    far_edge: np.ndarray,
    near_edge: np.ndarray,
    *,
    size_frac: float = 0.55,
    cap: float = 0.45,
) -> tuple[float, float]:
    """Derive **how much** of the field counts as far from the polygon's foreshortening (squish).

    The far touchline is squished to ``sq = W_far / W_near`` of the near touchline's pixel length —
    that ratio *is* the perspective, and apparent ball size scales with the local field width. A ball
    is "far" once its apparent size drops below ``size_frac`` × the near-edge size; with apparent size
    ~ linear in curve-depth, that boundary is at depth ``(size_frac - sq) / (1 - sq)``. So a
    **less-squished** (close) field gets ``far_frac ≈ 0`` (trust AutoCam almost everywhere) and a
    **more-squished** (distant) field reserves a larger far band for the human GT — self-calibrating
    per game from the polygon, and it leans the AutoCam signal toward the clean close games.

    Returns ``(far_frac, sq)``. ``size_frac`` is the one physical knob ("a ball smaller than this
    fraction of the near-edge ball is far"); ``cap`` bounds the reserve.
    """
    w_far = float(far_edge[:, 0].max() - far_edge[:, 0].min())
    w_near = float(near_edge[:, 0].max() - near_edge[:, 0].min())
    if w_near <= 1.0:
        return 0.0, 1.0
    sq = w_far / w_near
    if sq >= size_frac:
        return 0.0, sq
    return float(min(cap, (size_frac - sq) / max(1e-3, 1.0 - sq))), sq


# ---------------------------------------------------------------------------
# Viewport-gated candidate selection (the non-far teacher)
# ---------------------------------------------------------------------------


def select_teacher(
    detections: dict[int, list[tuple]],
    viewport: dict[int, tuple[float, float]],
    geom,
    *,
    gate_mult: float = 4.0,
    gate_floor_px: float = 50.0,
    support_margin_px: float = 50.0,
    fallback_ball_px: float = 14.0,
) -> dict[int, tuple[float, float]]:
    """For each frame with a viewport, pick the in-field detection candidate nearest the viewport
    (within a depth-scaled gate). Returns ``{global_frame: (x, y)}`` — AutoCam's selection recovered
    at the precise, de-lagged raw-detection location. Frames with no supporting candidate (AutoCam
    parked / lost) are omitted.
    """
    have_geom = geom is not None and getattr(geom, "valid", False)
    teacher: dict[int, tuple[float, float]] = {}
    for g, vp in viewport.items():
        cands = detections.get(g)
        if not cands:
            continue
        if have_geom:
            exp = float(geom.expected_ball_diameter_px(np.asarray(vp))[0])
        else:
            exp = fallback_ball_px
        gate = max(gate_mult * exp, gate_floor_px)
        best = None
        best_d = gate
        for cx, cy, _conf in cands:
            d = math.hypot(cx - vp[0], cy - vp[1])
            if d > best_d:
                continue
            if have_geom and not bool(
                geom.is_in_support(np.asarray((cx, cy)), margin_px=support_margin_px)[0]
            ):
                continue
            best, best_d = (cx, cy), d
        if best is not None:
            teacher[g] = best
    return teacher


def split_far(
    stream: dict[int, tuple[float, float]],
    far_edge: np.ndarray,
    near_edge: np.ndarray,
    *,
    far_frac: float = 0.22,
) -> tuple[dict[int, tuple[float, float]], set[int]]:
    """Partition a teacher stream into ``(non_far, far_frames)`` by curve-following field depth.

    A frame is *far* when its ball sits within ``far_frac`` of the way from the far touchline (the
    curved far-edge) toward the near one — the regime where AutoCam loses the ball and human GT must
    own it. The boundary follows the touchline curve, not a flat row.
    """
    non_far: dict[int, tuple[float, float]] = {}
    far_frames: set[int] = set()
    for g, (x, y) in stream.items():
        if curve_depth(x, y, far_edge, near_edge) < far_frac:
            far_frames.add(g)
        else:
            non_far[g] = (x, y)
    return non_far, far_frames


# ---------------------------------------------------------------------------
# Filters on the single-point teacher stream
# ---------------------------------------------------------------------------


def drop_frozen_runs(
    stream: dict[int, tuple[float, float]],
    *,
    vel_px: float = 1.5,
    min_run: int = 20,
) -> tuple[dict[int, tuple[float, float]], int]:
    """Drop sustained-frozen runs (AutoCam holding a stale position when lost). A run is
    ``>= min_run`` consecutive frames each moving ``< vel_px`` from the previous. Brief dead-ball
    pauses survive; only sustained holds are removed. Operates on the dense series."""
    frames = sorted(stream)
    if not frames:
        return dict(stream), 0
    slow = {frames[0]: False}
    for i in range(1, len(frames)):
        f, pf = frames[i], frames[i - 1]
        d = math.hypot(stream[f][0] - stream[pf][0], stream[f][1] - stream[pf][1])
        slow[f] = (f - pf) <= 4 and d < vel_px
    drop: set[int] = set()
    i, n = 0, len(frames)
    while i < n:
        if slow[frames[i]]:
            j = i
            while j < n and slow[frames[j]]:
                j += 1
            if (j - i) >= min_run:
                drop.update(frames[i:j])
            i = j
        else:
            i += 1
    return {f: xy for f, xy in stream.items() if f not in drop}, len(drop)


def subsample(
    stream: dict[int, tuple[float, float]],
    *,
    base_stride: int = 4,
    dense_stride: int = 2,
    turn_deg: float = 45.0,
    turn_window: int = 10,
) -> dict[int, tuple[float, float]]:
    """Thin the dense stream: keep every ``base_stride``-th frame (by sorted **index**, since the
    teacher frames are already stride-4 in source units), densify to ``dense_stride`` within
    ``turn_window`` frames of a > ``turn_deg`` heading change (the most appearance-varied frames).
    Raise ``base_stride`` to thin further (the crop-count / balance lever)."""
    frames = sorted(stream)
    if not frames:
        return {}
    head: dict[int, float | None] = {frames[0]: None}
    for i in range(1, len(frames)):
        f, pf = frames[i], frames[i - 1]
        dx, dy = stream[f][0] - stream[pf][0], stream[f][1] - stream[pf][1]
        head[f] = math.atan2(dy, dx) if (dx or dy) else head[pf]
    turn_frames: set[int] = set()
    for i in range(1, len(frames)):
        a, b = head[frames[i - 1]], head[frames[i]]
        if a is None or b is None:
            continue
        d = abs(math.degrees(b - a))
        d = min(d, 360 - d)
        if d > turn_deg:
            turn_frames.update(frames[max(0, i - turn_window) : i + turn_window])
    out: dict[int, tuple[float, float]] = {}
    for idx, f in enumerate(frames):
        stride = dense_stride if f in turn_frames else base_stride
        if idx % stride == 0:
            out[f] = stream[f]
    return out


def _assert_no_eval_leak(labels: dict, game_id: str, exclude: dict) -> None:
    """Fail loudly if any excluded (held-out eval) frame survived into the training labels."""
    if game_id in exclude.get("game_ids", set()):
        raise AssertionError(
            f"{game_id} is a held-out eval game and must not be in training"
        )
    for lo, hi in exclude.get("frame_ranges", {}).get(game_id, []):
        leaked = [f for f in labels if lo <= f <= hi]
        if leaked:
            raise AssertionError(
                f"{game_id}: {len(leaked)} labels in held-out range {lo}..{hi} (e.g. {leaked[:5]})"
            )


# ---------------------------------------------------------------------------
# Teacher track = the EXISTING tracker over AutoCam detections + human GT override
# ---------------------------------------------------------------------------


def teacher_track(
    detections: dict[int, list[tuple]],
    polygon,
    *,
    geom=None,
    human_balls: dict[int, tuple[float, float]] | None = None,
    human_novis: set[int] | None = None,
    conf_floor: float = 0.06,
    backing_px: float = 45.0,
) -> dict[int, tuple[float, float]]:
    """Per-frame ball labels for distillation = the **existing** tracker (``world_model.track_ball``)
    run over AutoCam's per-frame detection candidates, anchored by the human GT, kept only where a
    real detection backs the track (so coasted / guessed frames are dropped).

    This is the validated teacher signal: the existing tracker over AutoCam detections lands within
    R15 m of the human far-GT **0.77** of the time (median 2.1 m) vs AutoCam's own viewport at 0.15
    (median 41 m). Human ``ball`` frames override (anchoring the track through AutoCam's failures);
    ``not_visible`` frames are emitted as empty (no ball). Needs a **valid** field geometry (the
    meters-smooth tracker); returns ``{}`` for neutral geometry.
    """
    from training.world_model.geometry import build_field_geometry
    from training.world_model.reranker import track_ball
    from training.world_model.tbd import Candidate

    human_balls = human_balls or {}
    human_novis = human_novis or set()
    if geom is None and polygon is not None:
        geom = build_field_geometry(np.asarray(polygon, dtype=np.float64))
    if geom is None or not getattr(geom, "valid", False):
        return {}

    gframes = sorted(set(detections) | set(human_balls))
    frames: list[list] = []
    for g in gframes:
        if g in human_balls:
            frames.append(
                [Candidate(x=human_balls[g][0], y=human_balls[g][1], score=1.0)]
            )
        elif g in human_novis:
            frames.append([])
        else:
            frames.append(
                [
                    Candidate(x=x, y=y, score=max(c, 1e-3))
                    for (x, y, c) in detections.get(g, [])
                    if c >= conf_floor
                ]
            )
    # frame_gaps[t] = gap INTO frame t (from t-1) — track_ball scales its smoothness budget and
    # teleport gate by it, so this MUST be the backward difference (gaps[0] unused).
    gaps = [4] + [gframes[i] - gframes[i - 1] for i in range(1, len(gframes))]
    track = track_ball(frames, geom, frame_gaps=gaps)

    out: dict[int, tuple[float, float]] = {}
    b2 = backing_px * backing_px
    for i, g in enumerate(gframes):
        if g in human_novis:
            continue
        if g in human_balls:
            out[g] = human_balls[g]
            continue
        if i not in track:
            continue
        tx, ty = track[i]
        # snap the label to the REAL detection the tracker is following (the actual ball pixel that
        # frame), not the Kalman-smoothed position — we train a ball DETECTOR, so the target must be
        # the ball, not a smoothed estimate. Drop the frame if no detection backs the track (coasted).
        cands = detections.get(g, [])
        if not cands:
            continue
        cx, cy, _ = min(cands, key=lambda c: (c[0] - tx) ** 2 + (c[1] - ty) ** 2)
        # the snapped detection must be IN-FIELD — the marathon's raw detections include off-field
        # false positives (sky/trees/nets), and the tracker can coast onto one; an off-field label is
        # garbage. Drop it if the backing detection is off the field polygon.
        if (cx - tx) ** 2 + (cy - ty) ** 2 <= b2 and bool(
            geom.is_in_support(np.asarray((cx, cy)), margin_px=50.0)[0]
        ):
            out[g] = (cx, cy)
    return out


# ---------------------------------------------------------------------------
# Top-level builder
# ---------------------------------------------------------------------------


def build_distill_games(
    game_configs: list[dict],
    *,
    exclude: dict | None = None,
    base_stride: int = 4,
    dense_stride: int = 2,
    conf_floor: float = 0.06,
    backing_px: float = 45.0,
    max_per_game: int | None = None,
    report: bool = True,
) -> list[dict]:
    """Turn per-game configs into the ``games`` list consumed by ``build_heatmap_crops``.

    Each ``game_config``: ``{game_id, video, segments, polygon, detections, human_labels?, split?,
    camera?, team?, target_width?, video_rotation?}`` (``detections``/``human_labels`` are sidecar
    paths). The per-frame teacher = :func:`teacher_track` (existing tracker over AutoCam detections,
    human-GT-anchored, detection-backed); human ``ball`` frames are kept in full, the rest are
    subsampled. Output game: ``{game_id, video, polygon, labels:{frame:(x,y)}, split, ...}``.
    """
    exclude = exclude or {"game_ids": set(), "frame_ranges": {}}
    games: list[dict] = []
    for gc in game_configs:
        gid = gc["game_id"]
        if gid in exclude.get("game_ids", set()):
            if report:
                print(f"{gid}: SKIP (held-out eval game)")
            continue

        offsets = seg_offsets(gc["segments"])
        polygon = gc.get("polygon")
        detections = load_detections(gc["detections"], offsets)
        human_balls: dict[int, tuple[float, float]] = {}
        human_novis: set[int] = set()
        if gc.get("human_labels"):
            human_balls, human_novis = load_human_labels(gc["human_labels"], offsets)

        track = teacher_track(
            detections,
            polygon,
            human_balls=human_balls,
            human_novis=human_novis,
            conf_floor=conf_floor,
            backing_px=backing_px,
        )
        if not track:
            if report:
                print(f"{gid}: 0 teacher labels (neutral geometry?) — skipped")
            continue

        # strip held-out eval frames before subsampling
        for lo, hi in exclude.get("frame_ranges", {}).get(gid, []):
            track = {f: xy for f, xy in track.items() if not (lo <= f <= hi)}

        # keep every human GT label; subsample the (dense) tracked frames
        human_frames = set(human_balls)
        auto = {f: xy for f, xy in track.items() if f not in human_frames}
        # restrict AutoCam labels to the actual halves — drop warm-up / halftime / pre-&-post-game
        # where AutoCam tracks players, not a game ball (human far-GT is exempt: it's in-play & clean)
        ranges = active_play_ranges(gc["segments"], gc.get("game_state"))
        if ranges:
            n_before = len(auto)
            auto = {
                f: xy
                for f, xy in auto.items()
                if any(lo <= f <= hi for lo, hi in ranges)
            }
            if report:
                print(
                    f"  {gid}: active-play filter {n_before} -> {len(auto)} auto labels"
                )
        kept = subsample(auto, base_stride=base_stride, dense_stride=dense_stride)
        # hard per-game cap (uniform over the game) — bounds crop count / build time for a fast
        # first run; the turn-densification means base_stride alone doesn't reliably thin.
        if max_per_game and len(kept) > max_per_game:
            ks = sorted(kept)
            picks = {ks[int(i * len(ks) / max_per_game)] for i in range(max_per_game)}
            kept = {f: xy for f, xy in kept.items() if f in picks}
        labels = dict(kept)
        labels.update({f: track[f] for f in human_frames if f in track})

        _assert_no_eval_leak(labels, gid, exclude)
        if not labels:
            if report:
                print(f"{gid}: 0 labels — skipped")
            continue

        out = {
            "game_id": gid,
            "video": gc["video"],
            "polygon": polygon,
            "labels": labels,
            "split": gc.get("split", "train"),
        }
        if gc.get("target_width"):
            out["target_width"] = gc["target_width"]
        if gc.get("video_rotation"):
            out["video_rotation"] = gc["video_rotation"]
        games.append(out)
        if report:
            print(
                f"{gid} [{gc.get('camera', '?')}/{gc.get('team', '?')}]: "
                f"track {len(track)} (human {len(human_balls)}) -> "
                f"subsample {len(kept)} + human = {len(labels)} labels"
            )
    return games
