"""Build the distillation ``games`` list for :func:`build_heatmap_crops` from AutoCam's
re-run ball detections, split by field-relative depth.

**Strategy (locked with Mark, 2026-06-30).** Our homegrown detector is already *better than
AutoCam on far balls* — that is what the ~thousands of human far-ball labels bought us. It is
*worse on near/normal balls*, because it only ever trained on the sparse far distribution (the
"dim-ball" problem: the ball is in its candidate set but ranked ~3rd). So:

* **Non-far band → distill AutoCam.** AutoCam is reliable on near/normal balls; its dense per-frame
  detections teach our detector to rank the easy ball #1.
* **Far band → human ground truth only.** AutoCam loses far balls, so we do *not* distill it there
  — the human far labels (``ball``) own the far band, and ``not_visible`` frames (no findable ball)
  override any spurious AutoCam pick. This is what *preserves* our far advantage.
* **Preserve far by composition, not loss-weighting** (loss up-weighting was tried — EXP-DIST-13/14
  — and hurt): subsample the huge near-ball mass, keep every human far label.

**Teacher label per non-far frame = viewport-gated nearest candidate.** AutoCam's raw top-conf
candidate is the ball only ~13.5 % of the time on hard frames (off-field false positives win), but
AutoCam's *viewport* (its own internally-selected, smoothed ball) tells us *which* candidate is the
ball. So we pick the raw detection candidate nearest the viewport (within a depth-scaled gate, and
in the field) — recovering AutoCam's selection at the precise, **de-lagged** raw-detection location,
with detector confidence available for filtering. Frames where the viewport has no nearby
supporting candidate (AutoCam parked / lost) get no label.

**Far is field-relative and follows the touchline curve** (the camera is on a movable tripod, and the
far touchline bows up at the sides in the dewarped band — a flat horizontal cut is wrong). We measure
depth **between the polygon's two edge-curves**: interpolate the far-edge ``y_far(x)`` and near-edge
``y_near(x)`` at the ball's x and take ``(y - y_far(x)) / (y_near(x) - y_far(x))`` (0 = far touchline,
1 = near). A constant-depth boundary is then a curve parallel to the far touchline. The far edge is
picked as the 5 polygon points higher in the (corrected, upright) frame, so this is correct
regardless of the polygon's point ordering and on the flipped early-Dahua games. **How much** of that
depth counts as far is derived per game from the polygon's **squish** (``far_frac_from_squish``: the
far touchline's pixel-foreshortening) — a less-squished close field reserves almost nothing as far
(trust AutoCam), a more-squished distant field reserves more for the human GT.

Inputs per game come from the per-video JSONL store on F: (canonical per DECISIONS.md 2026-06-26):
``autocam_detections.jsonl`` (``{seg,f,x,y,conf}``, one row/candidate), ``autocam_viewport.jsonl``
(``{seg,f,x,y}``), ``game.json`` (``field_polygon``, ``segments[].global_offset``, ``video_rotation``)
and optional ``ball_labels.jsonl`` (``{seg,f,a,p}``, ``a in {ball,not_visible,out_of_play}``). All
coordinates are source px on the same global-frame axis (``global = segment.global_offset + f``).
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
    """Thin the dense stream: keep every ``base_stride``-th frame, densify to ``dense_stride``
    within ``turn_window`` frames of a > ``turn_deg`` heading change (the most appearance-varied
    frames). This is the near-mass balance lever — raise ``base_stride`` to keep far from drowning."""
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
    for f in frames:
        stride = dense_stride if f in turn_frames else base_stride
        if f % stride == 0:
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
# Top-level builder
# ---------------------------------------------------------------------------


def build_distill_games(
    game_configs: list[dict],
    *,
    exclude: dict | None = None,
    base_stride: int = 4,
    dense_stride: int = 2,
    far_frac: float | None = None,
    size_frac: float = 0.55,
    frozen_vel_px: float = 1.5,
    frozen_min_run: int = 20,
    gate_mult: float = 4.0,
    gate_floor_px: float = 50.0,
    max_nonfar_per_game: int | None = None,
    report: bool = True,
) -> list[dict]:
    """Turn per-game configs into the ``games`` list consumed by ``build_heatmap_crops``.

    Each ``game_config``: ``{game_id, video, segments, polygon?, detections, viewport,
    human_labels?, split?, camera?, team?, target_width?}`` where ``segments`` is the ``game.json``
    ``segments`` list, ``detections``/``viewport``/``human_labels`` are sidecar paths.

    Each output game: ``{game_id, video, polygon, labels:{frame:(x,y)}, split, target_width?,
    video_rotation?}``. ``labels`` = subsampled non-far AutoCam teacher + every human far ``ball``,
    with ``not_visible`` frames removed. Human labels win on conflict and are exempt from the far
    drop and the frozen/subsample filters.
    """
    from training.world_model.geometry import build_field_geometry

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
        geom = (
            build_field_geometry(np.asarray(polygon, dtype=np.float64))
            if polygon
            else None
        )
        far_edge, near_edge = field_edges(polygon) if polygon else (None, None)

        detections = load_detections(gc["detections"], offsets)
        viewport = load_viewport(gc["viewport"], offsets)
        human_balls, human_novis = ({}, set())
        if gc.get("human_labels"):
            human_balls, human_novis = load_human_labels(gc["human_labels"], offsets)

        # 1. recover AutoCam's selected ball at the raw-detection location (de-lagged)
        teacher = select_teacher(
            detections, viewport, geom, gate_mult=gate_mult, gate_floor_px=gate_floor_px
        )
        n_sel = len(teacher)

        # 2. far band → drop AutoCam (human GT owns it); keep non-far.
        # far_frac is per-game from the polygon's squish unless explicitly overridden.
        if far_edge is not None:
            if far_frac is None:
                ff, sq = far_frac_from_squish(far_edge, near_edge, size_frac=size_frac)
            else:
                ff, sq = far_frac, float("nan")
            non_far, far_frames = split_far(teacher, far_edge, near_edge, far_frac=ff)
        else:
            ff, sq = 0.0, float("nan")
            non_far, far_frames = dict(teacher), set()
        n_far = len(far_frames)

        # 3. drop AutoCam-lost holds, then strip excluded eval frames BEFORE subsampling
        non_far, n_frozen = drop_frozen_runs(
            non_far, vel_px=frozen_vel_px, min_run=frozen_min_run
        )
        for lo, hi in exclude.get("frame_ranges", {}).get(gid, []):
            non_far = {f: xy for f, xy in non_far.items() if not (lo <= f <= hi)}

        # 4. subsample the near mass (balance lever), optional hard cap per game
        kept = subsample(non_far, base_stride=base_stride, dense_stride=dense_stride)
        if max_nonfar_per_game and len(kept) > max_nonfar_per_game:
            keep_frames = set(
                sorted(kept)[:: max(1, len(kept) // max_nonfar_per_game)][
                    :max_nonfar_per_game
                ]
            )
            kept = {f: xy for f, xy in kept.items() if f in keep_frames}

        # 5. merge human far GT: ball wins on conflict & is exempt from filters; not_visible removes
        labels = dict(kept)
        labels.update(human_balls)
        for g in human_novis:
            labels.pop(g, None)
        # never let a human-labeled eval frame leak either
        for lo, hi in exclude.get("frame_ranges", {}).get(gid, []):
            labels = {f: xy for f, xy in labels.items() if not (lo <= f <= hi)}

        _assert_no_eval_leak(labels, gid, exclude)
        if not labels:
            if report:
                print(f"{gid}: 0 labels after filtering — skipped")
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
            n_human = len(human_balls)
            print(
                f"{gid} [{gc.get('camera', '?')}/{gc.get('team', '?')}]: "
                f"sel {n_sel} -> far-drop {n_far}, frozen {n_frozen} -> subsample {len(kept)} "
                f"+ human {n_human} (novis {len(human_novis)}) = {len(labels)} labels"
                f"  squish={sq:.2f} -> far<{ff:.0%} of field depth"
            )
    return games
