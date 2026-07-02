"""Parent event-tap phase anchors.

Parents tagging in the moment-tagger app tap a button at each phase transition
(Kickoff / Halftime / 2nd Half / Final Whistle -- the moment-tagger
``EventSyncPrompt``). Those taps are stored as ``sync_anchors`` rows with a
device wall-clock timestamp. Given the recording's true start wall-clock (from
the time-sync reconcile), each tap maps to a video-time estimate of its phase
boundary.

Parent taps are a *noisy* human signal (reaction lag, wrong button, only some
games have a tagging parent). Trust model (Mark, 2026-07-02):

  * a lone tap -- or taps that don't agree -- is the **lowest-quality** signal:
    used only as a weak, wide prior to break ties when the detector is unsure;
    it never overrides a confident whistle/ball anchor.
  * **multiple parents that basically agree** (>= 2 taps clustered within
    ``CLUSTER_WINDOW_S`` seconds) are a **trusted** consensus: a strong anchor
    that can compete with / override the detector.

This module is pure logic (no I/O): it turns raw taps + the recording start into
a per-boundary :class:`Anchor` with a confidence tier. ``fuse_phases`` consumes
the anchors; the thin fetch/adapter that pulls ``sync_anchors`` from TTT and the
reconciled start lives with the phase game-start resolver.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime

# moment-tagger EventSyncPrompt label -> detector boundary key.
BOUNDARY_FOR_LABEL: dict[str, str] = {
    "kickoff": "kickoff",
    "halftime_start": "halftime",
    "halftime_end": "second_half",
    "game_end": "end",
}

# Taps for one boundary within this spread (seconds) count as an agreeing cluster.
CLUSTER_WINDOW_S = 10.0
# Parents tap *after* they see/hear the event; shift estimates earlier by this.
REACTION_LAG_S = 1.0


@dataclass(frozen=True)
class Anchor:
    """A parent-derived prior for one phase boundary, in video time (seconds
    into the same video ``fuse_phases`` runs on)."""

    boundary: str  # kickoff | halftime | second_half | end
    video_time: float  # seconds into the video
    confidence: str  # "high" (agreeing cluster) | "low" (lone / scattered)
    n_taps: int  # taps backing this anchor
    spread_s: float  # spread of the backing cluster (0.0 for a lone tap)

    @property
    def is_high(self) -> bool:
        return self.confidence == "high"


def _largest_cluster(times: list[float], window: float) -> list[float]:
    """The largest subset of ``times`` spanning <= ``window`` seconds.

    Sweep each sorted value as a window start and take the longest run within
    ``window``; ties go to the tighter (earlier-ending) run. Returns the run's
    values (>= 1 element)."""
    if not times:
        return []
    xs = sorted(times)
    best: list[float] = [xs[0]]
    n = len(xs)
    for i in range(n):
        j = i
        while j + 1 < n and xs[j + 1] - xs[i] <= window:
            j += 1
        run = xs[i : j + 1]
        if len(run) > len(best) or (
            len(run) == len(best) and (run[-1] - run[0]) < (best[-1] - best[0])
        ):
            best = run
    return best


def _tap_video_time(tap: dict, recording_start: datetime | None) -> float | None:
    """Video-time (seconds) for one tap, or None if it can't be placed.

    Prefers a TTT-computed ``video_time_seconds`` (the time-sync system already
    mapped the tap into video time -- no wall-clock math, no timezone risk).
    Falls back to ``device_timestamp - recording_start`` (both must be tz-aware),
    minus the reaction lag. Returns None (drop the tap) when neither is usable."""
    vts = tap.get("video_time_seconds")
    if isinstance(vts, int | float) and not isinstance(vts, bool):
        return float(vts)
    if recording_start is None:
        return None
    ts = tap.get("device_timestamp")
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            return None
    if not isinstance(ts, datetime):
        return None
    try:
        return (ts - recording_start).total_seconds() - REACTION_LAG_S
    except TypeError:
        # naive vs aware mismatch -> can't place this tap reliably.
        return None


def build_anchors(
    taps: list[dict], recording_start: datetime | None
) -> dict[str, Anchor]:
    """Turn raw event taps into per-boundary anchors in video time.

    ``taps``: dicts with a moment-tagger ``label`` (kickoff | halftime_start |
    halftime_end | game_end) and either a TTT-computed ``video_time_seconds`` or a
    wall-clock ``device_timestamp`` (aware ``datetime`` or ISO string).
    ``recording_start``: wall-clock instant of video time 0 (the reconciled true
    recording start); only needed for taps lacking ``video_time_seconds``.

    For each boundary: place each tap in video time (see ``_tap_video_time``),
    find the largest cluster within ``CLUSTER_WINDOW_S``; if it has >= 2 taps the
    anchor is **high** confidence at the cluster median (outliers excluded),
    otherwise **low** confidence at the median of all the boundary's taps. Only
    boundaries with >= 1 usable tap appear in the result.
    """
    by_boundary: dict[str, list[float]] = {}
    for tap in taps:
        label = tap.get("label")
        if not isinstance(label, str):
            continue
        boundary = BOUNDARY_FOR_LABEL.get(label)
        if boundary is None:
            continue
        vt = _tap_video_time(tap, recording_start)
        if vt is None or vt < 0:
            # unplaceable, or before the recording started (implausible) -> drop.
            continue
        by_boundary.setdefault(boundary, []).append(vt)

    anchors: dict[str, Anchor] = {}
    for boundary, times in by_boundary.items():
        cluster = _largest_cluster(times, CLUSTER_WINDOW_S)
        if len(cluster) >= 2:
            anchors[boundary] = Anchor(
                boundary=boundary,
                video_time=float(statistics.median(cluster)),
                confidence="high",
                n_taps=len(cluster),
                spread_s=float(cluster[-1] - cluster[0]),
            )
        else:
            anchors[boundary] = Anchor(
                boundary=boundary,
                video_time=float(statistics.median(times)),
                confidence="low",
                n_taps=len(times),
                spread_s=0.0,
            )
    return anchors
