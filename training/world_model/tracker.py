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

from dataclasses import dataclass

import numpy as np

from training.world_model.geometry import FieldGeometry
from training.world_model.tbd import Candidate, TBDResult, TrackPoint


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
    action_radius: float = 300.0  # (fused) radius for the local action centroid
    action_pull: float = 0.6  # (fused) blend toward action centroid during a gap


def _clamp(p: np.ndarray, cfg: TrackerConfig) -> np.ndarray:
    if cfg.frame_w > 0.0:
        return np.array(
            [min(max(p[0], 0.0), cfg.frame_w), min(max(p[1], 0.0), cfg.frame_h)]
        )
    return p


def _acquire(
    cands: list[Candidate], geom: FieldGeometry | None, cfg: TrackerConfig
) -> Candidate | None:
    """Highest-score candidate clearing ``acq_score`` (optionally on-field)."""
    pool = [c for c in cands if c.score >= cfg.acq_score]
    if cfg.require_support_on_acquire and geom is not None:
        pool = [c for c in pool if bool(geom.is_in_support(np.array([[c.x, c.y]]))[0])]
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
    pos: np.ndarray | None = None
    vel = np.zeros(2)
    lost = 0
    points: list[TrackPoint] = []

    for t, cands in enumerate(frames):
        if pos is None:
            c = _acquire(cands, geom, cfg)
            if c is not None:
                pos = np.array([c.x, c.y])
                vel = np.zeros(2)
                lost = 0
                points.append(TrackPoint(t, float(pos[0]), float(pos[1]), True))
            continue

        pred = _clamp(pos + vel, cfg)
        gate = cfg.gate0 + cfg.gate_grow * lost
        gated = [c for c in cands if np.hypot(c.x - pred[0], c.y - pred[1]) <= gate]
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
                c = _acquire(cands, geom, cfg)
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
    pos: np.ndarray | None = None
    vel = np.zeros(2)
    lost = 0
    points: list[TrackPoint] = []

    for t, acands in enumerate(appearance):
        mpts = action[t] if t < len(action) else []
        if pos is None:
            c = _acquire(acands, geom, cfg)
            if c is not None:
                pos = np.array([c.x, c.y])
                vel = np.zeros(2)
                lost = 0
                points.append(TrackPoint(t, float(pos[0]), float(pos[1]), True))
            continue

        pred = _clamp(pos + vel, cfg)
        gate = cfg.gate0 + cfg.gate_grow * lost
        gated = [c for c in acands if np.hypot(c.x - pred[0], c.y - pred[1]) <= gate]
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
            # Action-area prior: pull toward the local action centroid in the gap.
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
                c = _acquire(acands, geom, cfg)
                if c is not None:
                    pos = np.array([c.x, c.y])
                    vel = np.zeros(2)
                    lost = 0
                    detected = True
        points.append(TrackPoint(t, float(pos[0]), float(pos[1]), detected))

    return TBDResult(points=points, total_logprob=0.0)
