"""Track-before-detect over per-frame ball candidates (the world-model spine).

The per-frame detector produces a noisy *likelihood field*; on a fixed 180deg
camera with 3-8 px balls it false-fires on players/lines/bright-spots more
strongly than on the ball, so per-frame argmax fails (the heatmap session
measured 0.29 far-recall, 76% false-fire). Instead of deciding per frame, we
keep many candidate peaks per frame and find the **single trajectory that
maximizes integrated likelihood subject to physics** — the classic
track-before-detect formulation (Viterbi over a frame lattice: columns = frames,
nodes = candidate peaks, a track = a path, score = summed emission + transition
log-likelihood; O(T * J^2)).

Physics encoded here (from the ball's hard constraints):

- **No teleport / max speed** — per-frame displacement is gated (a hard
  ``teleport_px`` ceiling, a soft ``max_speed_px`` above which re-acquisition is
  penalized). Distractors that would require an impossible jump can't capture the
  track.
- **Smooth / near-ballistic motion** — an acceleration penalty (Gaussian process
  noise) favours constant-velocity and gentle arcs; an erratic distractor path
  scores worse than the ball's smooth one.
- **Single object** — exactly one path; the estimator tolerates the ball being a
  *non-dominant* peak (where the greedy gated tracker failed).
- **Occlusion persistence** — every frame has a ``miss`` node (invisibility
  penalty); a run of misses propagates the belief by constant-velocity
  prediction and re-acquires on the physically-consistent side, instead of
  jumping to whatever distractor is brightest.
- **Context over appearance** — the emission score folds in the geometry priors
  (``FieldGeometry``): a candidate of the wrong apparent size for its field
  location, or outside the field+dome support, is down-weighted. This is how the
  ball is told apart from an *identical-looking* sideline/bench/adjacent-field
  ball — on context, not pixels.

This is the offline MAP decoder (deterministic, testable). The online switching
particle filter (for deployment, with explicit gravity/bounce/restart modes) is
a follow-on that reuses the same emission model.

Pure numpy (+ the geometry module). No torch.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from training.world_model.geometry import FieldGeometry


@dataclass(frozen=True)
class Candidate:
    """One per-frame ball candidate (e.g. a heatmap peak or motion blob).

    Attributes:
        x: Source-pixel x.
        y: Source-pixel y.
        score: Detector confidence in ``(0, 1]`` (or any positive saliency).
        size_px: Observed blob diameter in px, or ``None`` if unknown. When
            present it is checked against the geometric size prior.
    """

    x: float
    y: float
    score: float
    size_px: float | None = None


@dataclass
class TBDConfig:
    """Weights and physics limits for the track-before-detect decoder."""

    # Emission (observation) weighting.
    score_weight: float = 1.0  # weight on log(detector score)
    size_weight: float = 1.0  # weight on the geometric size-consistency logprob
    size_rel_sigma: float = 0.5  # std of log(observed/expected) ball size
    support_penalty: float = -6.0  # added when a candidate is off field+dome
    support_margin_px: float = 60.0
    support_dome_px: float = 250.0  # upward (far-side) tolerance for airborne balls

    # Transition (dynamics) limits, in source pixels per frame.
    max_speed_px: float = 130.0  # soft cap: normal ball motion between frames
    teleport_px: float = 500.0  # hard cap: above this, no transition at all
    reacquire_penalty: float = -8.0  # penalty for a max_speed..teleport jump
    accel_sigma_px: float = 18.0  # process noise on acceleration (smoothness)

    # Occlusion.
    miss_logprob: float = -3.5  # invisibility penalty for a frame with no detection

    # Search width.
    max_candidates_per_frame: int = 24  # top-K peaks kept per frame


@dataclass
class TrackPoint:
    """One frame of the recovered single-ball trajectory."""

    frame_idx: int
    x: float
    y: float
    detected: bool  # False => position is a physics prediction through occlusion


@dataclass
class _State:
    pos: np.ndarray
    vel: np.ndarray
    score: float  # cumulative path log-likelihood reaching this node
    back: int | None
    detected: bool


@dataclass
class TBDResult:
    """The MAP single-ball trajectory and its total log-likelihood."""

    points: list[TrackPoint] = field(default_factory=list)
    total_logprob: float = float("-inf")


def _emission_logprob(cand: Candidate, geom: FieldGeometry, cfg: TBDConfig) -> float:
    """Observation log-likelihood of a candidate, folding in geometry priors."""
    lp = cfg.score_weight * math.log(max(cand.score, 1e-6))
    xy = np.array([[cand.x, cand.y]], dtype=np.float64)
    if cand.size_px is not None:
        lp += cfg.size_weight * float(
            geom.size_consistency_logprob(
                xy, np.array([cand.size_px]), cfg.size_rel_sigma
            )[0]
        )
    if not bool(geom.is_in_support(xy, cfg.support_margin_px, cfg.support_dome_px)[0]):
        lp += cfg.support_penalty
    return lp


def _frame_nodes(
    cands: list[Candidate], geom: FieldGeometry, cfg: TBDConfig
) -> list[tuple[np.ndarray | None, float, bool]]:
    """Build (pos, emission, detected) nodes for a frame: top-K dets + a miss."""
    top = sorted(cands, key=lambda c: c.score, reverse=True)[
        : cfg.max_candidates_per_frame
    ]
    nodes: list[tuple[np.ndarray | None, float, bool]] = [
        (np.array([c.x, c.y], dtype=np.float64), _emission_logprob(c, geom, cfg), True)
        for c in top
    ]
    nodes.append((None, cfg.miss_logprob, False))  # occlusion node
    return nodes


def run_tbd(
    frames: list[list[Candidate]],
    geom: FieldGeometry,
    cfg: TBDConfig | None = None,
) -> TBDResult:
    """Decode the MAP single-ball trajectory from per-frame candidates.

    Args:
        frames: ``frames[t]`` is the list of candidates in frame ``t`` (may be
            empty = total occlusion that frame). Frames must be consecutive.
        geom: Field geometry for the size/support priors (may be neutral).
        cfg: Decoder config; defaults used if ``None``.

    Returns:
        A :class:`TBDResult` with one :class:`TrackPoint` per frame from the
        first frame that has a detection onward (leading all-occlusion frames are
        skipped). Empty if no frame has any candidate.

    The decoder is a Viterbi pass that carries, for each (frame, node), the best
    incoming path and its implied velocity, then scores transitions by an
    acceleration penalty + a max-speed gate. Occlusion ``miss`` nodes inherit the
    constant-velocity prediction so the track bridges gaps smoothly.
    """
    cfg = cfg or TBDConfig()
    t_count = len(frames)
    table: list[list[_State | None] | None] = [None] * t_count

    prev: list[_State | None] | None = None
    seed_done = False
    inv_accel_var = 1.0 / (cfg.accel_sigma_px**2)

    for t in range(t_count):
        nodes = _frame_nodes(frames[t], geom, cfg)

        if not seed_done:
            # Track must start on a real detection (a miss has no position).
            states: list[_State | None] = []
            any_det = False
            for pos, emis, det in nodes:
                if det and pos is not None:
                    states.append(
                        _State(
                            pos=pos,
                            vel=np.zeros(2),
                            score=emis,
                            back=None,
                            detected=True,
                        )
                    )
                    any_det = True
                else:
                    states.append(None)
            if any_det:
                table[t] = states
                prev = states
                seed_done = True
            continue

        assert prev is not None
        states = []
        for pos, emis, det in nodes:
            best_score = float("-inf")
            best_back: int | None = None
            best_pos: np.ndarray | None = None
            best_vel: np.ndarray | None = None
            for pi, ps in enumerate(prev):
                if ps is None:
                    continue
                pred = ps.pos + ps.vel
                npos = pos if (det and pos is not None) else pred
                disp = npos - ps.pos
                speed = float(np.hypot(disp[0], disp[1]))
                if speed > cfg.teleport_px:
                    continue
                pen = cfg.reacquire_penalty if speed > cfg.max_speed_px else 0.0
                accel = disp - ps.vel
                tcost = -0.5 * float(accel @ accel) * inv_accel_var
                total = ps.score + tcost + pen + emis
                if total > best_score:
                    best_score = total
                    best_back = pi
                    best_pos = npos
                    best_vel = disp
            if best_back is None:
                states.append(None)
            else:
                states.append(
                    _State(
                        pos=best_pos,  # type: ignore[arg-type]
                        vel=best_vel,  # type: ignore[arg-type]
                        score=best_score,
                        back=best_back,
                        detected=det,
                    )
                )
        table[t] = states
        prev = states

    return _backtrack(table)


def _backtrack(table: list[list[_State | None] | None]) -> TBDResult:
    """Walk back the best final node to recover the trajectory."""
    seed_t = next((t for t, s in enumerate(table) if s is not None), None)
    if seed_t is None:
        return TBDResult()
    last_t = max(t for t, s in enumerate(table) if s is not None)

    final = table[last_t]
    assert final is not None
    best_idx = max(
        (i for i, st in enumerate(final) if st is not None),
        key=lambda i: final[i].score,  # type: ignore[union-attr]
        default=None,
    )
    if best_idx is None:
        return TBDResult()

    total = final[best_idx].score  # type: ignore[union-attr]
    chain: dict[int, _State] = {}
    t = last_t
    idx: int | None = best_idx
    while t >= seed_t and idx is not None:
        st = table[t][idx]  # type: ignore[index]
        if st is None:
            break
        chain[t] = st
        idx = st.back
        t -= 1

    points = [
        TrackPoint(
            frame_idx=ti,
            x=float(chain[ti].pos[0]),
            y=float(chain[ti].pos[1]),
            detected=chain[ti].detected,
        )
        for ti in sorted(chain)
    ]
    return TBDResult(points=points, total_logprob=float(total))
