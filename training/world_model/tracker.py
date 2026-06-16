"""Causal continuity tracker over per-frame candidates — the deployment inference.

The global-MAP Viterbi (``tbd.py``) optimises the whole trajectory at once. That
is right when the target is *consistently* detectable, but it FAILS on an
intermittent target: EXP-1/3 showed champion-J's far ball is a strong peak only
~56% of frames, so the global MAP prefers a smooth distractor/coast path and the
recall collapses to ~0.

A **causal tracker** is the right inference here. Each frame: predict the ball
position (constant velocity), gate candidates to a window around the prediction,
and pick the candidate **closest to the prediction** — *continuity*, not
brightness. Coast through gaps on a decaying velocity, and re-acquire the global
best candidate when lost too long.

On champion-J's existing heatmap (no retraining), this lifts far-ball **viewport
area-recall (R=400) from 0.39 (argmax) to ~0.84** — recovering nearly every frame
where the ball is detectable at all. The decisive empirical lesson: **"continuity
beats appearance"** — closest-in-gate beats highest-score-in-gate, because when a
distractor outscores the ball the ball is still the candidate nearest the
predicted trajectory.

Pure numpy. Consumes ``Candidate`` and returns a ``TBDResult`` (so the eval's
``track_to_predictions`` works unchanged). Pair with
``measurements.suppress_static_candidates`` upstream to drop static background.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from training.world_model.geometry import FieldGeometry
from training.world_model.tbd import Candidate, TBDResult, TrackPoint


def _static_cells(
    frames: list[list[Candidate]], cell_px: float, thresh: float
) -> set[tuple[int, int]]:
    """Grid cells holding a candidate in >= ``thresh`` fraction of frames.

    These are *persistent-static* detector responses — on a fixed camera, a high-
    score peak that fires at the same place in most frames is a background scene
    feature (a line corner, post, far-side clutter), NOT the ball: a real ball
    moves continuously and never sits in one ``cell_px`` cell for most of a
    multi-second clip (even a restart ball moves on within a second or two). The
    tracker uses this to avoid *acquiring/parking on* such cells while still
    keeping them as a fallback (the ball may legitimately pass through one).
    """
    n = len(frames)
    if n == 0 or thresh <= 0.0:
        return set()
    occ: dict[tuple[int, int], int] = defaultdict(int)
    for cands in frames:
        for cell in {(int(c.x // cell_px), int(c.y // cell_px)) for c in cands}:
            occ[cell] += 1
    return {cell for cell, k in occ.items() if k >= thresh * n}


def _support(geom: FieldGeometry | None, p: np.ndarray) -> float:
    """1.0 if ``p`` is inside the field support region, else 0.0 (0 when no geom)."""
    if geom is None:
        return 0.0
    try:
        return 1.0 if bool(geom.is_in_support(np.array([[p[0], p[1]]]))[0]) else 0.0
    except Exception:
        return 0.0


def _action_near(p: np.ndarray, mpts: list[Candidate], radius: float) -> float:
    """1.0 if any action/motion blob is within ``radius`` of ``p`` (the area prior)."""
    return 1.0 if any(np.hypot(m.x - p[0], m.y - p[1]) <= radius for m in mpts) else 0.0


@dataclass
class TrackerConfig:
    """Parameters for the causal continuity tracker."""

    gate0: float = 90.0  # base gate radius (px) around the predicted position
    gate_grow: float = 50.0  # gate radius grows this much per consecutive lost frame
    max_lost: int = 8  # after this many lost frames, re-acquire the global best
    vel_alpha: float = 0.5  # velocity EMA on a detection (0=hold, 1=snap)
    vel_decay: float = 0.8  # velocity *= this while coasting (coast to a stop)
    acq_score: float = 0.5  # min candidate score to (re)acquire
    frame_w: float = 0.0  # if >0, clamp coasting predictions to [0, frame_w]
    frame_h: float = 0.0  # if >0, clamp coasting predictions to [0, frame_h]
    require_support_on_acquire: bool = False  # only acquire on-field (needs geom)
    # Persistent-static-feature avoidance. If >0, the tracker will not acquire or
    # follow a candidate whose cell holds a peak in >= this fraction of the clip
    # (a fixed scene feature / detector false-positive) unless it is the only
    # option. Validated cross-game (no regression on Irondequoit, +0.05/0.06 on
    # Fairport, +0.19 @R200 on Spencerport clip-1): the dim/intermittent ball
    # otherwise loses acquisition + parking to bright static FPs. Offline-tier
    # (uses the whole clip's occupancy); for streaming, feed a rolling window.
    static_thresh: float = 0.0
    static_cell_px: float = 40.0
    action_radius: float = 300.0  # (fused) radius for the local action centroid
    # Action pull during an appearance gap. 0 = pure causal (the robust default that
    # generalises across games). Set >0 (e.g. 0.6) only for hard far-ball RECOVERY —
    # it recovers undetected far balls (+0.13 on Irondequoit) but costs precision on
    # well-detected balls (-0.10 on Fairport). A clean adaptive switch is open work
    # (min-lost / reliability-scale / reliability-threshold all failed; see DESIGN).
    action_pull: float = 0.0


def _clamp(p: np.ndarray, cfg: TrackerConfig) -> np.ndarray:
    if cfg.frame_w > 0.0:
        return np.array(
            [min(max(p[0], 0.0), cfg.frame_w), min(max(p[1], 0.0), cfg.frame_h)]
        )
    return p


def _in_avoid(c: Candidate, avoid: set[tuple[int, int]], cell_px: float) -> bool:
    return (int(c.x // cell_px), int(c.y // cell_px)) in avoid


def _acquire(
    cands: list[Candidate],
    geom: FieldGeometry | None,
    cfg: TrackerConfig,
    avoid: set[tuple[int, int]] | None = None,
) -> Candidate | None:
    """Highest-score candidate clearing ``acq_score`` (optionally on-field).

    Persistent-static candidates (``avoid``) are excluded unless nothing else
    qualifies — so acquisition never latches onto a bright background false-
    positive while the dim ball is also present.
    """
    pool = [c for c in cands if c.score >= cfg.acq_score]
    if cfg.require_support_on_acquire and geom is not None:
        pool = [c for c in pool if bool(geom.is_in_support(np.array([[c.x, c.y]]))[0])]
    if avoid:
        non_static = [c for c in pool if not _in_avoid(c, avoid, cfg.static_cell_px)]
        if non_static:  # fall back to static only when it is the sole option
            pool = non_static
    return max(pool, key=lambda c: c.score) if pool else None


def causal_track(
    frames: list[list[Candidate]],
    geom: FieldGeometry | None = None,
    cfg: TrackerConfig | None = None,
) -> TBDResult:
    """Track the ball causally: gate to the prediction, pick the nearest candidate.

    Args:
        frames: per-frame candidate lists (consecutive frames). Pre-filter with
            ``suppress_static_candidates`` to drop static background.
        geom: optional field geometry; only used to keep (re)acquisition on-field
            when ``cfg.require_support_on_acquire``.
        cfg: tracker config; defaults used if ``None``.

    Returns:
        A :class:`TBDResult` with one :class:`TrackPoint` per frame from the first
        acquired frame onward (``detected=False`` while coasting through a gap).
    """
    cfg = cfg or TrackerConfig()
    avoid = _static_cells(frames, cfg.static_cell_px, cfg.static_thresh)
    pos: np.ndarray | None = None
    vel = np.zeros(2)
    lost = 0
    points: list[TrackPoint] = []

    for t, cands in enumerate(frames):
        if pos is None:
            c = _acquire(cands, geom, cfg, avoid)
            if c is not None:
                pos = np.array([c.x, c.y])
                vel = np.zeros(2)
                lost = 0
                points.append(TrackPoint(t, float(pos[0]), float(pos[1]), True))
            continue

        pred = _clamp(pos + vel, cfg)
        gate = cfg.gate0 + cfg.gate_grow * lost
        gated = [c for c in cands if np.hypot(c.x - pred[0], c.y - pred[1]) <= gate]
        if (
            avoid
        ):  # never follow a persistent-static FP — coast past it (re-acquire later)
            gated = [c for c in gated if not _in_avoid(c, avoid, cfg.static_cell_px)]
        if gated:
            c = min(gated, key=lambda c: np.hypot(c.x - pred[0], c.y - pred[1]))
            new = np.array([c.x, c.y])
            vel = (1.0 - cfg.vel_alpha) * vel + cfg.vel_alpha * (new - pos)
            pos = new
            lost = 0
            detected = True
        else:
            pos = pred
            vel = vel * cfg.vel_decay
            lost += 1
            detected = False
            if lost > cfg.max_lost:
                c = _acquire(cands, geom, cfg, avoid)
                if c is not None:
                    pos = np.array([c.x, c.y])
                    vel = np.zeros(2)
                    lost = 0
                    detected = True
        points.append(TrackPoint(t, float(pos[0]), float(pos[1]), detected))

    return TBDResult(points=points, total_logprob=0.0)


def causal_track_fused(
    appearance: list[list[Candidate]],
    action: list[list[Candidate]],
    geom: FieldGeometry | None = None,
    cfg: TrackerConfig | None = None,
) -> TBDResult:
    """Causal tracker fusing appearance (precise position) + action (area prior).

    Appearance candidates (heatmap peaks) drive precise localization. During
    appearance **gaps**, the prediction is pulled toward the **local player-action
    centroid** — the mean of motion/action points within ``cfg.action_radius`` of
    the prediction — the *"action clusters around the ball"* prior (R4), instead of
    coasting blind into a drift. EXP-5: this lifts far-ball viewport area-recall
    (R=400) from 0.74 (appearance only) to 0.87 on the hardest clip.

    Args:
        appearance: per-frame appearance candidates (e.g. heatmap peaks).
        action: per-frame action points (motion blobs); same length as
            ``appearance``. Only positions are used (the centroid is unweighted).
        geom: optional geometry (acquisition support gate).
        cfg: tracker config; ``action_radius`` / ``action_pull`` control the prior.

    Returns:
        A :class:`TBDResult`, one :class:`TrackPoint` per frame from acquisition on.
    """
    cfg = cfg or TrackerConfig()
    avoid = _static_cells(appearance, cfg.static_cell_px, cfg.static_thresh)
    pos: np.ndarray | None = None
    vel = np.zeros(2)
    lost = 0
    points: list[TrackPoint] = []

    for t, acands in enumerate(appearance):
        mpts = action[t] if t < len(action) else []
        if pos is None:
            c = _acquire(acands, geom, cfg, avoid)
            if c is not None:
                pos = np.array([c.x, c.y])
                vel = np.zeros(2)
                lost = 0
                points.append(TrackPoint(t, float(pos[0]), float(pos[1]), True))
            continue

        pred = _clamp(pos + vel, cfg)
        gate = cfg.gate0 + cfg.gate_grow * lost
        gated = [c for c in acands if np.hypot(c.x - pred[0], c.y - pred[1]) <= gate]
        if avoid:  # never follow a persistent-static FP — coast (action prior) instead
            gated = [c for c in gated if not _in_avoid(c, avoid, cfg.static_cell_px)]
        if gated:
            c = min(gated, key=lambda c: np.hypot(c.x - pred[0], c.y - pred[1]))
            new = np.array([c.x, c.y])
            vel = (1.0 - cfg.vel_alpha) * vel + cfg.vel_alpha * (new - pos)
            pos = new
            lost = 0
            detected = True
        else:
            pos = pred
            vel = vel * cfg.vel_decay
            lost += 1
            detected = False
            # Action-area prior (opt-in, action_pull>0): pull toward the local
            # player-action centroid during the gap. Recovers undetected far balls
            # at a small precision cost — situational, see the action_pull docstring.
            if cfg.action_pull > 0.0 and mpts:
                near = np.array(
                    [
                        (m.x, m.y)
                        for m in mpts
                        if np.hypot(m.x - pred[0], m.y - pred[1]) <= cfg.action_radius
                    ]
                )
                if len(near):
                    centroid = near.mean(axis=0)
                    pos = _clamp(
                        pos * (1.0 - cfg.action_pull) + centroid * cfg.action_pull, cfg
                    )
            if lost > cfg.max_lost:
                c = _acquire(acands, geom, cfg, avoid)
                if c is not None:
                    pos = np.array([c.x, c.y])
                    vel = np.zeros(2)
                    lost = 0
                    detected = True
        points.append(TrackPoint(t, float(pos[0]), float(pos[1]), detected))

    return TBDResult(points=points, total_logprob=0.0)


# ---------------------------------------------------------------------------
# Multi-hypothesis (beam) tracker — Phase-2.
#
# The causal trackers above are SINGLE-hypothesis: one committed position per
# frame. On the hard far clip (EXP-8) the fused candidate set contains the ball
# 0.97 @R200 / 1.00 @R400, yet the greedy tracker realises only 0.52 — because
# greedy acquisition/closest-gate locks onto a brighter player *distractor* the
# moment one outscores the ball, and can't recover until a re-acquire grabs
# another bright distractor. The selection, not the detection, is the wall.
#
# The fix is to STOP committing per frame. Keep a beam of K **full-window
# trajectory hypotheses**; each frame, extend every hypothesis by all its gated
# candidate continuations (+ a coast option, + re-acquire jumps when lost), score
# each by accumulated **trajectory** quality, merge near-duplicates to preserve
# diversity, and prune to K. At the end pick the single best-scoring path. The
# integrated score rewards the *one physically-continuous, action-consistent,
# on-field* path (the ball) over a distractor-hop that pays a continuity/
# acceleration penalty at every jump and drifts off-field — "continuity beats
# appearance" lifted from per-frame to per-trajectory. Pure numpy, ~K*cands per
# frame, trivially real-time on CPU (so it also fits the base-hardware gate).
# ---------------------------------------------------------------------------


@dataclass
class MHTConfig:
    """Parameters for the multi-hypothesis (beam) tracker."""

    beam: int = 16  # K trajectory hypotheses kept per frame
    gate0: float = 90.0  # base gate radius (px) around a hypothesis' prediction
    gate_grow: float = 35.0  # gate grows this much per consecutive coasted frame
    max_coast: int = 14  # a hypothesis may coast at most this many frames in a row
    reacq_after: int = 4  # after this many coasts, allow jumps to global-best peaks
    reacq_k: int = 4  # number of global-best peaks a lost hypothesis may jump to
    vel_alpha: float = 0.5  # velocity EMA on a detection (0=hold, 1=snap)
    vel_decay: float = 0.85  # velocity *= this while coasting
    w_app: float = 1.0  # appearance-score weight (evidence the candidate is a ball)
    w_cont: float = 2.5  # continuity weight (penalty for distance from prediction)
    w_acc: float = 1.0  # acceleration weight (penalty for velocity change — smoothness)
    coast_pen: float = 0.5  # per-frame penalty for coasting (prefer detecting)
    w_action: float = 0.7  # reward for sitting where the player action is (area prior)
    action_radius: float = 300.0  # radius (px) for the action-near test
    w_support: float = 0.3  # reward for being inside the field support region
    merge_dist: float = (
        35.0  # merge hypotheses whose current pos is within this (keep best)
    )
    frame_w: float = 0.0  # if >0, clamp predictions to [0, frame_w]
    frame_h: float = 0.0  # if >0, clamp predictions to [0, frame_h]


class _Node:
    """One leaf of the hypothesis tree (a trajectory tip); backpointers to parent."""

    __slots__ = ("pos", "vel", "coast", "score", "parent", "pt")

    def __init__(self, pos, vel, coast, score, parent, pt):
        self.pos = pos
        self.vel = vel
        self.coast = coast
        self.score = score
        self.parent = parent
        self.pt = pt


def _prune(nodes: list[_Node], cfg: MHTConfig) -> list[_Node]:
    """Keep the top-``beam`` nodes, merging any within ``merge_dist`` (highest wins)."""
    nodes.sort(key=lambda n: -n.score)
    kept: list[_Node] = []
    for n in nodes:
        if any(
            np.hypot(n.pos[0] - k.pos[0], n.pos[1] - k.pos[1]) < cfg.merge_dist
            for k in kept
        ):
            continue
        kept.append(n)
        if len(kept) >= cfg.beam:
            break
    return kept


def _node_to_result(node: _Node) -> TBDResult:
    pts: list[TrackPoint] = []
    nd: _Node | None = node
    while nd is not None:
        pts.append(nd.pt)
        nd = nd.parent
    pts.reverse()
    return TBDResult(points=pts, total_logprob=node.score)


def multi_hypothesis_track(
    frames: list[list[Candidate]],
    action: list[list[Candidate]] | None = None,
    geom: FieldGeometry | None = None,
    cfg: MHTConfig | None = None,
    return_all_paths: bool = False,
) -> TBDResult | list[TBDResult]:
    """Beam search over the candidate lattice for the best ball trajectory.

    Args:
        frames: per-frame fused candidate lists (appearance ∪ motion), consecutive
            frames. Pre-filter with ``suppress_static_candidates`` upstream.
        action: per-frame action/motion blobs (same length) for the area prior;
            if ``None`` the area term is off (equivalent to ``w_action`` unused).
        geom: optional field geometry for the on-field support reward.
        cfg: :class:`MHTConfig`; defaults used if ``None``.

    Returns:
        A :class:`TBDResult` whose ``points`` are the best path's per-frame
        positions (``detected=False`` on coasted frames), ``total_logprob`` the
        winning path's accumulated score. ``frame_idx`` is the list index (the
        caller maps source frame = ``lo + frame_idx``), matching ``causal_track``.
    """
    cfg = cfg or MHTConfig()
    action = action or [[] for _ in frames]

    def clamp(p):
        if cfg.frame_w > 0.0:
            return np.array(
                [min(max(p[0], 0.0), cfg.frame_w), min(max(p[1], 0.0), cfg.frame_h)]
            )
        return p

    beam: list[_Node] = []
    for t, cands in enumerate(frames):
        mpts = action[t] if t < len(action) else []
        if not beam:
            # Seed a fresh hypothesis from every candidate this frame.
            for c in cands:
                p = np.array([c.x, c.y])
                sc = (
                    cfg.w_app * c.score
                    + cfg.w_action * _action_near(p, mpts, cfg.action_radius)
                    + cfg.w_support * _support(geom, p)
                )
                beam.append(
                    _Node(p, np.zeros(2), 0, sc, None, TrackPoint(t, c.x, c.y, True))
                )
            beam = _prune(beam, cfg)
            continue

        best_peaks = sorted(cands, key=lambda c: -c.score)[: cfg.reacq_k]
        children: list[_Node] = []
        for nd in beam:
            pred = clamp(nd.pos + nd.vel)
            gate = cfg.gate0 + cfg.gate_grow * nd.coast
            gated = [c for c in cands if np.hypot(c.x - pred[0], c.y - pred[1]) <= gate]
            for c in gated:
                cp = np.array([c.x, c.y])
                d = np.hypot(cp[0] - pred[0], cp[1] - pred[1])
                newvel = (1.0 - cfg.vel_alpha) * nd.vel + cfg.vel_alpha * (cp - nd.pos)
                step = (
                    cfg.w_app * c.score
                    - cfg.w_cont * (d / gate)
                    - cfg.w_acc * (np.hypot(*(newvel - nd.vel)) / gate)
                    + cfg.w_action * _action_near(cp, mpts, cfg.action_radius)
                    + cfg.w_support * _support(geom, cp)
                )
                children.append(
                    _Node(
                        cp,
                        newvel,
                        0,
                        nd.score + step,
                        nd,
                        TrackPoint(t, c.x, c.y, True),
                    )
                )
            # Re-acquire jumps: a lost hypothesis may leap to a global-best peak
            # outside its gate (pays a fixed continuity penalty for the jump).
            if nd.coast >= cfg.reacq_after:
                for c in best_peaks:
                    cp = np.array([c.x, c.y])
                    if np.hypot(cp[0] - pred[0], cp[1] - pred[1]) <= gate:
                        continue  # already added above as a gated continuation
                    step = (
                        cfg.w_app * c.score
                        - cfg.w_cont * 1.2
                        + cfg.w_action * _action_near(cp, mpts, cfg.action_radius)
                        + cfg.w_support * _support(geom, cp)
                    )
                    children.append(
                        _Node(
                            cp,
                            np.zeros(2),
                            0,
                            nd.score + step,
                            nd,
                            TrackPoint(t, c.x, c.y, True),
                        )
                    )
            # Coast continuation: hold the physics prediction through a gap.
            if nd.coast + 1 <= cfg.max_coast:
                step = -cfg.coast_pen + 0.5 * cfg.w_action * _action_near(
                    pred, mpts, cfg.action_radius
                )
                children.append(
                    _Node(
                        pred.copy(),
                        nd.vel * cfg.vel_decay,
                        nd.coast + 1,
                        nd.score + step,
                        nd,
                        TrackPoint(t, float(pred[0]), float(pred[1]), False),
                    )
                )
        beam = _prune(children, cfg) if children else beam

    if not beam:
        return [] if return_all_paths else TBDResult(points=[], total_logprob=0.0)
    if return_all_paths:
        return [_node_to_result(n) for n in beam]
    return _node_to_result(max(beam, key=lambda n: n.score))
