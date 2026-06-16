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


def _clamp(p: np.ndarray, cfg: TrackerConfig) -> np.ndarray:
    if cfg.frame_w > 0.0:
        return np.array(
            [min(max(p[0], 0.0), cfg.frame_w), min(max(p[1], 0.0), cfg.frame_h)]
        )
    return p


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

    def _acquire(cands: list[Candidate]) -> Candidate | None:
        pool = [c for c in cands if c.score >= cfg.acq_score]
        if cfg.require_support_on_acquire and geom is not None:
            pool = [
                c for c in pool if bool(geom.is_in_support(np.array([[c.x, c.y]]))[0])
            ]
        return max(pool, key=lambda c: c.score) if pool else None

    for t, cands in enumerate(frames):
        if pos is None:
            c = _acquire(cands)
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
                c = _acquire(cands)
                if c is not None:
                    pos = np.array([c.x, c.y])
                    vel = np.zeros(2)
                    lost = 0
                    detected = True
        points.append(TrackPoint(t, float(pos[0]), float(pos[1]), detected))

    return TBDResult(points=points, total_logprob=0.0)
