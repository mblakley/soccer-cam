"""Game-ball ROLE assignment with handoff (Phase-2).

The "game ball" is a **role**, not a fixed physical object (Mark): when the ball
leaves the field of play and a *different* ball is sustainedly played in, the role
hands off to the new ball while the old one is abandoned out of play. This wraps
the single-ball causal tracker with a role state machine that encodes the exact
rules captured in DESIGN.md:

- **NORMAL:** the game ball is the *primary* track. It may be briefly out of
  bounds for a throw-in / goal-kick excursion — that's still the same ball.
- **Handoff gate:** a swap is only *possible* once the primary has been **out of
  play** (outside the field+margin) for ``out_of_play_frames`` — a real exit, not
  a brief excursion. In-field restarts (free-kick/PK/goal-kick) keep the same ball.
- **Handoff:** fires only if a *different* in-bounds ball (far from the abandoned
  primary) is **sustainedly** in play for ``sustain_frames`` while the primary
  stays out → the role transfers to it.
- **Intruder balls** (brief in-field non-game balls) never win: while the primary
  is in play the gate stays shut, and a brief intrusion fails the sustain test.

This is the single-primary + one-watched-candidate formulation (enough for the
real scenarios); full N-hypothesis tracking is a later generalization. Pure numpy.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from training.world_model.geometry import FieldGeometry
from training.world_model.tbd import Candidate, TrackPoint
from training.world_model.tracker import TrackerConfig, _acquire, _clamp


@dataclass
class RoleConfig:
    """Thresholds for the game-ball role state machine."""

    out_of_play_frames: int = 15  # primary out-of-bounds this long => handoff possible
    sustain_frames: int = 20  # a new in-play ball sustained this long => handoff
    support_margin: float = 60.0  # in/out-of-bounds margin (px)
    new_ball_min_dist: float = (
        400.0  # a "different" ball must be this far from the primary
    )


@dataclass
class GameBallResult:
    """The game-ball track plus the frames where the role handed off."""

    points: list[TrackPoint] = field(default_factory=list)
    handoffs: list[int] = field(default_factory=list)


def _in_play(pos: np.ndarray, geom: FieldGeometry | None, margin: float) -> bool:
    """True if the position is inside the field+margin (or no polygon → always)."""
    if geom is None or geom.polygon is None:
        return True
    return bool(geom.is_in_support(np.array([[pos[0], pos[1]]]), margin)[0])


def _advance(
    pos: np.ndarray,
    vel: np.ndarray,
    lost: int,
    acands: list[Candidate],
    mpts: list[Candidate],
    geom: FieldGeometry | None,
    cfg: TrackerConfig,
) -> tuple[np.ndarray, np.ndarray, int, bool]:
    """One causal + action-prior step (same rule as ``tracker.causal_track_fused``)."""
    pred = _clamp(pos + vel, cfg)
    gate = cfg.gate0 + cfg.gate_grow * lost
    gated = [c for c in acands if np.hypot(c.x - pred[0], c.y - pred[1]) <= gate]
    if gated:
        c = min(gated, key=lambda c: np.hypot(c.x - pred[0], c.y - pred[1]))
        new = np.array([c.x, c.y])
        vel = (1.0 - cfg.vel_alpha) * vel + cfg.vel_alpha * (new - pos)
        return new, vel, 0, True
    pos = pred
    vel = vel * cfg.vel_decay
    lost += 1
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
    return pos, vel, lost, False


def _acquire_new_ball(
    acands: list[Candidate],
    geom: FieldGeometry | None,
    cfg: TrackerConfig,
    role_cfg: RoleConfig,
    primary_pos: np.ndarray,
) -> Candidate | None:
    """Acquire an in-bounds candidate far from the (out-of-play) primary ball."""
    pool = [
        c
        for c in acands
        if c.score >= cfg.acq_score
        and _in_play(np.array([c.x, c.y]), geom, role_cfg.support_margin)
        and np.hypot(c.x - primary_pos[0], c.y - primary_pos[1])
        >= role_cfg.new_ball_min_dist
    ]
    return max(pool, key=lambda c: c.score) if pool else None


def track_game_ball(
    appearance: list[list[Candidate]],
    action: list[list[Candidate]] | None = None,
    geom: FieldGeometry | None = None,
    cfg: TrackerConfig | None = None,
    role_cfg: RoleConfig | None = None,
) -> GameBallResult:
    """Track the game ball with role-handoff between physical balls.

    Args:
        appearance: per-frame appearance candidates (heatmap peaks).
        action: per-frame action points (motion); ``None`` = no action prior.
        geom: field geometry (its polygon defines in/out of play). With no polygon
            the ball is always "in play" and no handoff can occur.
        cfg: single-ball tracker config.
        role_cfg: role state-machine thresholds.

    Returns:
        A :class:`GameBallResult` — the game-ball track and the handoff frames.
    """
    cfg = cfg or TrackerConfig()
    role_cfg = role_cfg or RoleConfig()
    action = action or [[] for _ in appearance]

    ppos: np.ndarray | None = None
    pvel = np.zeros(2)
    plost = 0
    pdet = False
    out_count = 0
    watching = False
    cpos: np.ndarray | None = None
    cvel = np.zeros(2)
    clost = 0
    c_inplay = 0
    handoffs: list[int] = []
    points: list[TrackPoint] = []

    for t in range(len(appearance)):
        acands = appearance[t]
        mpts = action[t] if t < len(action) else []

        if ppos is None:
            c = _acquire(acands, geom, cfg)
            if c is not None:
                ppos = np.array([c.x, c.y])
                pvel = np.zeros(2)
                plost = 0
                points.append(TrackPoint(t, float(ppos[0]), float(ppos[1]), True))
            continue

        ppos, pvel, plost, pdet = _advance(ppos, pvel, plost, acands, mpts, geom, cfg)
        if plost > cfg.max_lost:
            c = _acquire(acands, geom, cfg)
            if c is not None:
                ppos = np.array([c.x, c.y])
                pvel = np.zeros(2)
                plost = 0
                pdet = True

        # --- role state machine ---
        if _in_play(ppos, geom, role_cfg.support_margin):
            out_count = 0
            watching = False
            cpos = None  # ball returned to play → cancel any pending handoff
        else:
            out_count += 1
            if out_count >= role_cfg.out_of_play_frames:
                watching = True

        if watching:
            if cpos is None:
                cand = _acquire_new_ball(acands, geom, cfg, role_cfg, ppos)
                if cand is not None:
                    cpos = np.array([cand.x, cand.y])
                    cvel = np.zeros(2)
                    clost = 0
                    c_inplay = 0
            else:
                cpos, cvel, clost, _ = _advance(
                    cpos, cvel, clost, acands, mpts, geom, cfg
                )
                if clost > cfg.max_lost:
                    cpos = None
                    c_inplay = 0
                elif _in_play(cpos, geom, role_cfg.support_margin):
                    c_inplay += 1
                else:
                    c_inplay = 0
                if cpos is not None and c_inplay >= role_cfg.sustain_frames:
                    # HANDOFF: the role transfers to the sustained new ball.
                    handoffs.append(t)
                    ppos, pvel, plost = cpos, cvel, clost
                    pdet = True
                    watching = False
                    cpos = None
                    out_count = 0

        points.append(TrackPoint(t, float(ppos[0]), float(ppos[1]), pdet))

    return GameBallResult(points=points, handoffs=handoffs)
