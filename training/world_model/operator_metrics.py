"""Operator-scoreboard metrics — the EXP-72 session scorer promoted to committed code.

Pure, I/O-free metric functions for the W1 operator scoreboard (EXP-OP-01
pre-registration). Every definition here matches the pre-registered one exactly:

- ``capture@R`` / ``|Δcx|``: fraction of labeled views with |campath_cx − fx| ≤ R
  (x-axis), plus median / p90 of the same deltas.
- events: gap=64 frame clustering (``f - prev <= gap`` joins — the EXP-72 rule).
- framing metrics (pan-velocity profile, reversal rate, hold fidelity): the event
  unit is the CONTIGUOUS labeled segment (same gap-64 clustering).
- range bands by expected ball diameter: far < 8 px, mid 8–15 px, near > 15 px.
- split-half null calibration: event-level random half-splits of ONE arm's data;
  the null band is the central 95% of half-vs-half deltas.

No file I/O, no torch, numpy only — callers (``training.cli.operator_scoreboard``)
own loading and report assembly.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence

import numpy as np

# Event / segment clustering gap (frames) — the EXP-72 noise protocol.
DEFAULT_GAP = 64

# Range-band edges (px expected ball diameter) per the EXP-OP-01 pre-registration:
# far < 8 (replay_fullgame --far-px convention); near > 15 (log-midpoint of the
# ≈3.5x near/far span, EXP-DIST-66); mid is everything between, edges inclusive.
BAND_FAR_MAX_PX = 8.0
BAND_NEAR_MIN_PX = 15.0

# Hold-fidelity stoppage definition ((h)): GT focal swing ≤ 400 px.
DEFAULT_GT_SWING_MAX_PX = 400.0


# ---------------------------------------------------------------------------
# Event clustering
# ---------------------------------------------------------------------------


def cluster_events(frames: Iterable[int], gap: int = DEFAULT_GAP) -> list[list[int]]:
    """Cluster frame indices into events: a frame joins the previous event iff
    ``f - event[-1] <= gap`` (matching the EXP-72 scorer exactly: exactly ``gap``
    apart = same event, ``gap + 1`` = new event)."""
    ev: list[list[int]] = []
    for f in sorted(frames):
        if ev and f - ev[-1][-1] <= gap:
            ev[-1].append(f)
        else:
            ev.append([f])
    return ev


def segment_series(frames: Iterable[int], gap: int = DEFAULT_GAP) -> list[list[int]]:
    """Split labeled frames into contiguous segments — the framing metrics' event
    unit. Same clustering rule as :func:`cluster_events`; named separately because
    framing-events ≠ flip-events in the pre-registration."""
    return cluster_events(frames, gap=gap)


# ---------------------------------------------------------------------------
# Capture / delta statistics (the EXP-72 core read)
# ---------------------------------------------------------------------------


def capture_stats(
    deltas: Sequence[tuple[int, float]],
    radii: Sequence[float] = (300, 600),
) -> dict:
    """capture@R fractions + |Δcx| median / p90 over ``[(frame, abs_dx), ...]``.

    Returns ``{"n", "capture": {R: fraction}, "median", "p90"}``; the stats are
    ``None`` when there are no deltas (n = 0). Fractions count ``d <= R``.
    """
    ds = np.asarray([d for _f, d in deltas], dtype=np.float64)
    n = int(ds.size)
    if n == 0:
        return {
            "n": 0,
            "capture": {int(r): None for r in radii},
            "median": None,
            "p90": None,
        }
    return {
        "n": n,
        "capture": {int(r): float(np.mean(ds <= r)) for r in radii},
        "median": float(np.median(ds)),
        "p90": float(np.percentile(ds, 90)),
    }


def pair_flip_read(
    cap_a: dict[int, bool],
    cap_b: dict[int, bool],
    gap: int = DEFAULT_GAP,
) -> dict:
    """Paired flip read on the COMMON frames of two arms.

    ``cap_x`` maps frame -> captured@600 (bool). ``a_only`` = frames where a
    captured and b did not; ``b_only`` the reverse. Events via gap clustering.
    Returns ``{"n_common", "a_only_frames", "b_only_frames", "a_only_events",
    "b_only_events"}``.
    """
    common = sorted(set(cap_a) & set(cap_b))
    ao = [f for f in common if cap_a[f] and not cap_b[f]]
    bo = [f for f in common if cap_b[f] and not cap_a[f]]
    return {
        "n_common": len(common),
        "a_only_frames": len(ao),
        "b_only_frames": len(bo),
        "a_only_events": len(cluster_events(ao, gap=gap)),
        "b_only_events": len(cluster_events(bo, gap=gap)),
    }


# ---------------------------------------------------------------------------
# Framing metrics (per contiguous labeled segment)
# ---------------------------------------------------------------------------


def _steps(
    cx_by_frame: dict[int, float], segment: Sequence[int], fps: float
) -> list[float]:
    """Signed per-step velocities (px/s) between CONSECUTIVE available frames of
    one segment. Δt comes from the actual frame gap, so sparse (strided) series
    are stride-normalized by construction."""
    fs = sorted(f for f in segment if f in cx_by_frame)
    return [
        (cx_by_frame[b] - cx_by_frame[a]) / ((b - a) / fps)
        for a, b in zip(fs, fs[1:], strict=False)
    ]


def pan_velocity(
    cx_by_frame: dict[int, float], segment: Sequence[int], fps: float
) -> list[float]:
    """Per-step |Δcx|/Δt (px/s) between consecutive available frames within one
    segment. Works for a dense campath sampled at label frames and for sparse GT
    labels alike (Δt uses the actual frame gap)."""
    return [abs(v) for v in _steps(cx_by_frame, segment, fps)]


def velocity_summary(vels: Sequence[float]) -> dict:
    """``{"n", "median", "p90"}`` of a pooled velocity list (None when empty)."""
    arr = np.asarray(list(vels), dtype=np.float64)
    if arr.size == 0:
        return {"n": 0, "median": None, "p90": None}
    return {
        "n": int(arr.size),
        "median": float(np.median(arr)),
        "p90": float(np.percentile(arr, 90)),
    }


def reversal_rate(
    cx_by_frame: dict[int, float],
    segment: Sequence[int],
    fps: float,
    v_thresh: float,
) -> dict:
    """Reversals per labeled minute in one segment.

    A reversal is a sign flip between consecutive step velocities where BOTH
    steps exceed ``v_thresh`` in magnitude (threshold from GT only, fixed before
    any arm read). The denominator is the segment's labeled duration in minutes
    (span of the arm-available frames). Returns ``{"flips", "minutes", "rate"}``
    with ``rate`` None when the duration is zero (fewer than two available
    frames) — callers pool ``flips`` / ``minutes`` across segments.
    """
    vs = _steps(cx_by_frame, segment, fps)
    flips = sum(
        1
        for a, b in zip(vs, vs[1:], strict=False)
        if abs(a) > v_thresh and abs(b) > v_thresh and a * b < 0
    )
    fs = sorted(f for f in segment if f in cx_by_frame)
    minutes = (fs[-1] - fs[0]) / fps / 60.0 if len(fs) >= 2 else 0.0
    return {
        "flips": flips,
        "minutes": minutes,
        "rate": (flips / minutes) if minutes > 0 else None,
    }


def gt_velocity_threshold(gt_vels: Sequence[float], pct: float = 95) -> float:
    """The pre-registered GT-derived reversal threshold: the ``pct``-th percentile
    of the GT-label velocity distribution. Raises on an empty distribution — the
    threshold must come from GT, never a fallback."""
    arr = np.asarray(list(gt_vels), dtype=np.float64)
    if arr.size == 0:
        raise ValueError("gt_velocity_threshold: empty GT velocity distribution")
    return float(np.percentile(arr, pct))


def hold_fidelity(
    arm_cx_by_frame: dict[int, float],
    gt_fx_by_frame: dict[int, float],
    segments: Sequence[Sequence[int]],
    gt_swing_max: float = DEFAULT_GT_SWING_MAX_PX,
) -> tuple[list[tuple[list[int], float, float, float]], float | None]:
    """Hold fidelity on stoppage-like segments (GT focal swing ≤ ``gt_swing_max``).

    For each segment with ≥ 2 GT-labeled frames whose GT swing (max fx − min fx)
    is ≤ ``gt_swing_max``: the arm's swing over the SAME frames (those with arm
    data; ≥ 2 required, else the segment is skipped as uncovered). Ratio =
    arm_swing / gt_swing; a zero GT swing gives ratio 0.0 when the arm also held
    perfectly still, else ``inf``. Returns ``(rows, median_ratio)`` with rows
    ``(segment_frames, gt_swing, arm_swing, ratio)``.
    """
    rows: list[tuple[list[int], float, float, float]] = []
    for seg in segments:
        gt_fs = [f for f in seg if f in gt_fx_by_frame]
        if len(gt_fs) < 2:
            continue
        gvals = [gt_fx_by_frame[f] for f in gt_fs]
        gt_swing = max(gvals) - min(gvals)
        if gt_swing > gt_swing_max:
            continue
        arm_fs = [f for f in gt_fs if f in arm_cx_by_frame]
        if len(arm_fs) < 2:
            continue
        avals = [arm_cx_by_frame[f] for f in arm_fs]
        arm_swing = max(avals) - min(avals)
        if gt_swing > 0:
            ratio = arm_swing / gt_swing
        else:
            ratio = 0.0 if arm_swing == 0 else math.inf
        rows.append((list(gt_fs), float(gt_swing), float(arm_swing), float(ratio)))
    med = float(np.median([r[3] for r in rows])) if rows else None
    return rows, med


# ---------------------------------------------------------------------------
# Split-half null calibration (instrument admission, DECISIONS (g))
# ---------------------------------------------------------------------------


def split_half_null(
    frames_by_event: Sequence[Sequence[int]],
    values_by_frame: dict[int, float],
    metric_fn: Callable[[list[int]], float | None],
    *,
    reps: int = 300,
    seed: int,
) -> dict:
    """Event-level random half-splits of ONE arm's data.

    Events are filtered to frames present in ``values_by_frame`` (the arm's
    coverage); empty events drop out. Each rep randomly permutes the events,
    splits them into two halves, and records ``metric_fn(half_a_frames) -
    metric_fn(half_b_frames)``; reps where either half's metric is None or
    non-finite are discarded. Returns ``{"deltas", "band", "n_events",
    "reps_valid", "reason"}`` — ``band`` is the central 95% of the deltas
    ``(p2.5, p97.5)``, or None (with ``reason``) when it cannot be computed
    (fewer than 2 covered events, or no valid rep).
    """
    events = [[f for f in ev if f in values_by_frame] for ev in frames_by_event]
    events = [ev for ev in events if ev]
    if len(events) < 2:
        return {
            "deltas": [],
            "band": None,
            "n_events": len(events),
            "reps_valid": 0,
            "reason": f"fewer than 2 covered events ({len(events)})",
        }
    rng = np.random.default_rng(seed)
    deltas: list[float] = []
    half = len(events) // 2
    for _ in range(reps):
        perm = rng.permutation(len(events))
        fa = sorted(f for i in perm[:half] for f in events[i])
        fb = sorted(f for i in perm[half:] for f in events[i])
        ma, mb = metric_fn(fa), metric_fn(fb)
        if ma is None or mb is None:
            continue
        d = float(ma) - float(mb)
        if math.isfinite(d):
            deltas.append(d)
    if not deltas:
        return {
            "deltas": [],
            "band": None,
            "n_events": len(events),
            "reps_valid": 0,
            "reason": "no valid rep (metric undefined on every half-split)",
        }
    return {
        "deltas": deltas,
        "band": (
            float(np.percentile(deltas, 2.5)),
            float(np.percentile(deltas, 97.5)),
        ),
        "n_events": len(events),
        "reps_valid": len(deltas),
        "reason": None,
    }


# ---------------------------------------------------------------------------
# Range bands
# ---------------------------------------------------------------------------


def band_of(expected_diameter_px: float) -> str:
    """Range band from the expected ball diameter at the GT focal point.

    Pre-registered edges (EXP-OP-01): **far < 8 px**, **mid 8–15 px**,
    **near > 15 px**. Edge inclusivity as implemented (and tested): the strict
    inequalities are far's and near's, so both edges belong to mid —
    ``band_of(8.0) == "mid"`` and ``band_of(15.0) == "mid"``.
    """
    d = float(expected_diameter_px)
    if d < BAND_FAR_MAX_PX:
        return "far"
    if d > BAND_NEAR_MIN_PX:
        return "near"
    return "mid"
