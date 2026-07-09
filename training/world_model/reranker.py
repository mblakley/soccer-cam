"""Motion-consistency re-ranker: pick the ball from per-frame candidates by CONTEXT.

The detector finds the ball (candidate ceiling ~0.72 in meters on held-out Spencerport)
but ranks it #1 only ~16% of the time — it is genuinely dim, so per-frame brightness-argmax
picks a brighter distractor (EXP-27). This re-ranks the *same* candidates by physics/context
instead of brightness, nearly doubling top-1 (0.163 -> 0.307 @ R=5m, EXP-28) with no extra
model — pure CPU post-processing on the candidate dumps.

Three context terms, all from the ball's hard constraints:

- **Static-persistence penalty (the dominant lever).** A bright *static* distractor (a
  painted line, a tent, a bench, a coach's ball) sits in the same field cell every frame;
  the game ball passes through a cell only briefly. So a candidate is penalised by how
  often *some* candidate occupies its ~2 m world cell across the clip. **This is the key
  fix smoothness alone gets backwards** — a zero-motion track is maximally "smooth", so a
  pure smoothness/acceleration prior (e.g. plain track-before-detect) *rewards* static
  distractors and locks onto them; the persistence penalty is what repels them.
- **Motion-blob support.** The ball is a *moving* object, so it tends to coincide with a
  background-subtraction motion blob; static clutter does not. A small bonus for candidates
  near a motion blob.
- **Meters-smooth, physics-bounded trajectory.** A Viterbi/DP over the frame lattice favours
  a path that is smooth *in meters on the field plane* (perspective-fair) and forbids
  teleports (> max ball speed). A ``miss`` state lets the track coast an occlusion.

Operates in **world coordinates** (via the homography) so distances are perspective-fair —
a few px of error at a far corner is ~10 m and must be penalised like 10 m, not like a few
px. Returns predictions in source pixels for the renderer / scoring. Pure numpy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from training.world_model.geometry import FieldGeometry
from training.world_model.tbd import Candidate


@dataclass(frozen=True)
class RerankConfig:
    """Re-ranker weights.

    Teleport defaults RAISED 2026-07-01/02 (EXP-DIST-17/20, cli/sweep_tracker on the distilled detector).
    The old (vmax 2.5, max_jump 6.0) gate hard-EXCLUDED the true far candidate (meters-space is
    ill-conditioned near the far touchline, so the real ball's world position jitters past a tight gate —
    EXP-DIST-11's 82.5% far-exclusion); loosening it lifted far R15m 0.288 -> ~0.65 and cut median error
    51 m -> 12 m. **Cross-validated on TWO held-out games** (Spencerport + Irondequoit): the first
    single-game optimum (a0.3, mj40, v20) was Spencerport-overfit (Iron far 0.455 vs 0.727 at the tighter
    a1.0/mj25); the robust choice across both is **alpha=1.0, max_jump=25, vmax=12** (Spc far 0.648/near
    0.189; Iron far 0.727-0.545/near 0.312). alpha RAISED 0.3->1.0 because the hard-neg detector's scores
    are now worth trusting (a0.3 underperforms on held-out Iron); a3.0 overshoots. static_w=2.0 unchanged
    (cutting it hurt on both). Far target (AutoCam-dets->tracker 0.845) not yet reached; near (target
    0.978) is the open problem — see EXP-DIST-19/20."""

    vmax_m_per_frame: float = 12.0  # smoothness budget (scaled by the frame gap)
    max_jump_m_per_frame: float = 25.0  # hard teleport ceiling
    alpha: float = 1.0  # detector-score weight (raised 0.3->1.0 for the distilled detector, EXP-DIST-20)
    static_w: float = 2.0  # static-persistence penalty weight (the dominant lever)
    motion_w: float = 0.5  # motion-blob support bonus weight
    miss_cost: float = 0.9  # cost of the occlusion/miss state
    cell_m: float = 2.0  # world-cell size for the persistence map
    motion_radius_m: float = 3.0  # how near a motion blob counts as support
    # AERIAL BRIDGE (EXP-DIST-30/31, v0-v1 of the ball-state machine): the miss state
    # remembers where the track left the ground AND its exit velocity (from the last
    # detections before it left frame — the early-flight ones on a launch). Re-entry
    # near the BALLISTIC PREDICTION ``exit + v * airtime`` (uncertainty growing with
    # airtime) gets a strong bonus; re-entry merely at a flight-consistent RATE
    # (<= air_vmax_mpf) a small one; faster-than-flight re-entries — true teleports —
    # are penalised quadratically. bridge_w=0 = legacy flat re-entry (0.6, distance-blind).
    bridge_w: float = 0.0  # aerial-bridge shaping weight (0 = off)
    air_vmax_mpf: float = 2.0  # max plausible flight speed, world m per SOURCE frame
    # PHYSICAL TRANSITIONS (EXP-DIST-31b): the legacy vmax/max_jump were raised to 12/25
    # m-per-frame to survive far-band world jitter (EXP-DIST-11) — but at stride 8 that
    # makes candidate hops nearly free (budget 96-200 m), so the path NEVER misses and
    # the aerial machinery never engages (Chili full game: 0 miss entries, 1328
    # teleports). Physical mode replaces the loose global budget with real ball physics
    # plus DEPTH-DEPENDENT measurement noise: allowance = ball_vmax_mpf * gap +
    # phys_sigma_px * (local m-per-px Jacobian at both endpoints). Far positions may
    # jitter tens of meters (the homography is ill-conditioned there) without unlocking
    # free teleports near the camera. phys_sigma_px = 0 keeps legacy transitions.
    phys_sigma_px: float = (
        0.0  # px jitter mapped through the local Jacobian (0 = legacy)
    )
    ball_vmax_mpf: float = 2.5  # real ball speed ceiling, world m per SOURCE frame
    oob_w: float = 0.0  # out-of-bounds pin weight (0 = off; see _OOB_* constants)


# aerial-bridge launch model (EXP-DIST-31): horizontal momentum is roughly conserved
# in flight, so the landing zone is exit + v_exit * airtime. The exit velocity comes
# from the last two on-path detections; slower motion than _LAUNCH_MIN_MPF is rolling
# noise, not a launch, so no direction is inferred. The landing cone widens with
# airtime (unknown launch angle + apparent-velocity bias while the ball rises).
_LAUNCH_MIN_MPF = 0.8  # min exit speed (m per source frame) to trust a direction
_CONE_BASE_M = 6.0  # landing-zone radius at airtime 0
_CONE_SPREAD_MPF = 0.4  # landing-zone growth per source frame in the air
_CONE_BONUS = 0.75  # re-entry bonus (x bridge_w) inside the predicted landing zone
_BAND_BONUS = 0.5  # re-entry bonus (x bridge_w) for rate-consistent, direction-blind

# OUT-OF-BOUNDS state (EXP-DIST-35, Mark's physics): a ball crossing the mask boundary
# does not disappear — the rules bring it back NEAR WHERE IT LEFT (throw-in at the
# crossing point; measured on held-out GT: re-entry within 7-15 m of the extrapolated
# crossing, after 7-20 s). When the track leaves toward/over the boundary, the miss
# state pins its expectation at the BOUNDARY CROSSING POINT with a slow-growing cone —
# instead of the ballistic continuation (which predicts downfield) or a free wander.
_OOB_NEAR_BOUNDARY_M = 8.0  # exit position within this of the line counts as OOB
_OOB_BASE_M = 8.0  # re-entry cone radius at exit time (median measured 7.4 m)
_OOB_SPREAD_MPF = 0.03  # cone growth per source frame out of play
_OOB_CAP_M = 20.0  # cone ceiling (p90 measured 15 m)


def _world_polygon(geom: FieldGeometry) -> np.ndarray | None:
    poly = getattr(geom, "polygon", None)
    if poly is None:
        return None
    return geom.image_to_world(np.asarray(poly, float).reshape(-1, 2))


def _nearest_on_polygon(pw: np.ndarray, wpoly: np.ndarray) -> tuple[np.ndarray, int]:
    """Nearest point to ``pw`` on the (world-space) polygon boundary + its edge index."""
    best, bd, be = wpoly[0], np.inf, 0
    n = len(wpoly)
    for i in range(n):
        a, bseg = wpoly[i], wpoly[(i + 1) % n]
        ab = bseg - a
        denom = float(ab @ ab)
        t = 0.0 if denom == 0 else float(np.clip((pw - a) @ ab / denom, 0.0, 1.0))
        q = a + t * ab
        d = float(np.linalg.norm(pw - q))
        if d < bd:
            best, bd, be = q, d, i
    return best, be


def _restart_spots(cross_w: np.ndarray, edge_idx: int, wpoly: np.ndarray) -> np.ndarray:
    """Rule-based re-entry spots for a boundary crossing (EXP-DIST-35b).

    Touchline exit -> throw-in AT the crossing (the crossing alone). END-LINE exit
    (edges 4->5 and 9->0 of the 10-point field outline, the same convention as
    ``_far_margin_polygon``) -> the rules move the restart: GOAL KICK from the goal
    area (end-line midpoint pushed ~6 m infield) or CORNER at either end of that
    line — plus the crossing itself (quick keeper restarts). Returns ``(m, 2)``
    world points."""
    spots = [cross_w]
    n = len(wpoly)
    if n >= 10 and edge_idx in (4, n - 1):
        a, bseg = wpoly[edge_idx], wpoly[(edge_idx + 1) % n]
        mid = (a + bseg) / 2.0
        inward = wpoly.mean(axis=0) - mid
        nrm = float(np.linalg.norm(inward))
        if nrm > 0:
            spots.append(mid + inward / nrm * 6.0)  # goal-kick spot
        spots.append(a)  # corner arcs
        spots.append(bseg)
    return np.asarray(spots, float)


def static_persistence(
    frames_world: list[np.ndarray], cell_m: float
) -> list[np.ndarray]:
    """Per-candidate persistence in ``[0, 1]``: fraction of frames with a candidate in the
    same ~``cell_m`` world cell. ~1.0 for a fixed distractor, small for the moving ball."""
    n = len(frames_world)
    occ: dict[tuple[int, int], int] = {}
    for w in frames_world:
        for c in {(round(p[0] / cell_m), round(p[1] / cell_m)) for p in w}:
            occ[c] = occ.get(c, 0) + 1
    out = []
    for w in frames_world:
        out.append(
            np.array([occ[(round(p[0] / cell_m), round(p[1] / cell_m))] / n for p in w])
        )
    return out


def _motion_support(
    frames_world: list[np.ndarray],
    motion_world: list[np.ndarray],
    radius_m: float,
) -> list[np.ndarray]:
    out = []
    for w, m in zip(frames_world, motion_world, strict=True):
        if len(w) == 0 or len(m) == 0:
            out.append(np.zeros(len(w)))
            continue
        out.append(
            np.array(
                [1.0 if np.min(np.hypot(*(m - wi).T)) <= radius_m else 0.0 for wi in w]
            )
        )
    return out


def action_density_prior(
    frames: list[list[Candidate]],
    player_boxes: list[list[tuple[float, float]]],
    geom: FieldGeometry,
    sigma_m: float = 10.0,
    weight: float = 0.5,
) -> list[np.ndarray]:
    """Player-density (action-region) prior for :func:`rerank` ``priors``.

    The game ball sits where players cluster; a *far lone-player* distractor track is in low
    player density. So this favours candidates in dense action and discounts the far players
    that the re-ranker would otherwise follow as a smooth track — while KEEPING ball-on-player
    picks (the ball is in the dense action ~35% of the time, so blanket player-masking is
    wrong). EXP-31: +5 pts viewport recall (R15 0.491 -> 0.543) at ``weight=0.5``; over-
    weighting hurts (the ball isn't *always* in the densest cluster — clearances).

    Args:
        frames: same per-frame candidates passed to :func:`rerank`.
        player_boxes: per-frame list of player ``(cx, cy)`` centres in SOURCE pixels (e.g.
            YOLO box centres). Empty when no detections that frame.
        geom: the field geometry (density is measured in meters).
        sigma_m: action-region scale (Gaussian kernel std, meters).
        weight: prior strength (the additive emission bonus = ``-weight * density_norm``).

    Returns:
        A ``priors`` list aligned with ``frames`` (negative cost = favoured).
    """
    out: list[np.ndarray] = []
    for cands, boxes in zip(frames, player_boxes, strict=True):
        if not cands:
            out.append(np.zeros(0))
            continue
        cw = geom.image_to_world(np.array([[c.x, c.y] for c in cands], float))
        if not boxes:
            out.append(np.zeros(len(cands)))
            continue
        pw = geom.image_to_world(np.array(boxes, float))
        d = np.array(
            [
                np.exp(-((cw[i] - pw) ** 2).sum(1) / (2 * sigma_m**2)).sum()
                for i in range(len(cw))
            ]
        )
        out.append(-weight * d / (d.max() + 1e-9))
    return out


def coast_occlusions(
    preds: dict[int, tuple[float, float]],
) -> dict[int, tuple[float, float]]:
    """Fill the re-ranker's miss-gaps by constant-velocity coast (occlusion persistence).

    When the ball goes behind a player it is INVISIBLE — the detector cannot and should not
    "see" it (training detection through occlusion just teaches hallucination). It is the
    *tracker's* job to carry the ball through the gap by physics. This interpolates the ball's
    path across each bracketed miss-run (the ball moved roughly straight from where it entered
    the occlusion to where it re-emerged), so the viewport stays on its predicted path instead
    of dropping it. Offline (uses both endpoints); a real-time variant would extrapolate the
    last velocity. EXP-32: +2 pts viewport recall. Leading/trailing misses are left empty
    (no bracket to coast between).
    """
    if not preds:
        return preds
    keys = sorted(preds)
    out = dict(preds)
    for a, b in zip(keys, keys[1:], strict=False):
        if b - a <= 1:
            continue
        (xa, ya), (xb, yb) = preds[a], preds[b]
        for t in range(a + 1, b):
            w = (t - a) / (b - a)
            out[t] = (xa + w * (xb - xa), ya + w * (yb - ya))
    return out


def kalman_smooth(
    preds: dict[int, tuple[float, float]],
    geom: FieldGeometry,
    *,
    q_accel: float = 1.5,
    r_meas_m: float = 2.5,
) -> dict[int, tuple[float, float]]:
    """Constant-velocity Kalman RTS smoother over the selected ball track (world meters).

    The principled replacement for the linear :func:`coast_occlusions`. The re-ranker's
    per-frame selections hop between candidates near the ball (measurement noise); a kicked
    ball also accelerates (process noise). A constant-velocity Kalman forward filter +
    backward RTS pass, run in **world meters** (perspective-fair physics), both **de-jitters**
    the track (steadier viewport) and **coasts occlusions** by the motion model — predict-only
    on the missing frames, uncertainty growing through the gap, then optimally interpolated by
    the smoother. Returns a smoothed position at EVERY frame index in the track span (occluded
    frames included), in source pixels.

    Args:
        preds: ``{frame_idx: (x, y)}`` selected positions (source px) from :func:`rerank`.
        geom: field geometry with a valid homography (the filter runs in meters).
        q_accel: process-noise acceleration std (m per step^2) — how hard the ball can change
            velocity. Larger = trust the measurements more (less smoothing).
        r_meas_m: measurement-noise std (m) — how far a selected candidate sits from the true
            ball. Larger = smooth harder.
    """
    if len(preds) < 2 or not getattr(geom, "valid", False):
        return dict(preds)
    keys = sorted(preds)
    t0, t1 = keys[0], keys[-1]
    zs = {t: geom.image_to_world(np.array([[x, y]]))[0] for t, (x, y) in preds.items()}
    f = np.array([[1, 0, 1, 0], [0, 1, 0, 1], [0, 0, 1, 0], [0, 0, 0, 1]], float)
    h = np.array([[1, 0, 0, 0], [0, 1, 0, 0]], float)
    q = q_accel**2
    qm = q * np.array(
        [[0.25, 0, 0.5, 0], [0, 0.25, 0, 0.5], [0.5, 0, 1, 0], [0, 0.5, 0, 1]], float
    )
    rm = (r_meas_m**2) * np.eye(2)
    n = t1 - t0 + 1
    z0 = zs[t0]
    x = np.array([z0[0], z0[1], 0.0, 0.0])
    p = np.diag([r_meas_m**2, r_meas_m**2, 25.0, 25.0])
    xf = np.zeros((n, 4))
    pf = np.zeros((n, 4, 4))
    xp = np.zeros((n, 4))
    pp = np.zeros((n, 4, 4))
    xf[0] = xp[0] = x
    pf[0] = pp[0] = p
    for i in range(1, n):
        x = f @ x
        p = f @ p @ f.T + qm
        xp[i], pp[i] = x, p
        if (t0 + i) in zs:
            z = zs[t0 + i]
            k = p @ h.T @ np.linalg.inv(h @ p @ h.T + rm)
            x = x + k @ (z - h @ x)
            p = (np.eye(4) - k @ h) @ p
        xf[i], pf[i] = x, p
    xs = xf.copy()
    for i in range(n - 2, -1, -1):
        c = pf[i] @ f.T @ np.linalg.inv(pp[i + 1])
        xs[i] = xf[i] + c @ (xs[i + 1] - xp[i + 1])
    img = geom.world_to_image(xs[:, :2])
    return {t0 + i: (float(img[i, 0]), float(img[i, 1])) for i in range(n)}


def rerank(
    frames: list[list[Candidate]],
    geom: FieldGeometry,
    *,
    motion: list[list[Candidate]] | None = None,
    frame_gaps: list[int] | None = None,
    priors: list[np.ndarray] | None = None,
    miss_costs: list[float] | None = None,
    anchors: dict[int, tuple[float, float]] | None = None,
    anchor_radius_m: float = 8.0,
    config: RerankConfig | None = None,
) -> dict[int, tuple[float, float]]:
    """Re-rank per-frame ball candidates by physics/context (see module docstring).

    Args:
        frames: detector candidates per frame (``frames[t]`` = list of :class:`Candidate`,
            source-pixel ``x, y`` + ``score``). Empty frames are allowed (-> a miss).
        geom: a :class:`FieldGeometry` with a **valid** homography (meters are required for
            the perspective-fair smoothness and the static-cell map).
        motion: optional per-frame motion blobs (e.g. MOG2), same shape as ``frames``; gives
            the motion-support bonus.
        frame_gaps: optional per-frame source-frame gap to the previous frame (for stride-N
            dumps the smoothness budget scales by the gap). Defaults to all 1s.
        priors: optional additive per-candidate emission COST (``priors[t]`` aligns with
            ``frames[t]``): negative favours a candidate, positive penalises it. Used to fold
            in extra context the re-ranker doesn't model itself — e.g. an action/player-density
            prior (the ball sits where players cluster; a far lone-player track is in low
            density) or a size prior. ``None`` for no prior.
        miss_costs: optional PER-FRAME miss-state emission cost overriding the flat
            ``config.miss_cost`` (one float per frame). The learned selector supplies
            ``-log P(no visible ball)`` here: missing becomes expensive exactly when a
            confident candidate exists (the near-excursion fix) and cheap when the ball is
            genuinely invisible. ``None`` keeps the flat cost.
        anchors: optional IDENTITY anchors ``{frame_idx: (x, y) source px}`` — moments the
            game ball's position is known with certainty (kickoff/2H center spot from the
            game phases; restart spots later). At an anchored frame the path may only take a
            candidate within ``anchor_radius_m`` of the anchor (miss forbidden), so identity
            propagates bidirectionally from it. An anchor with NO candidate in radius is
            ignored (the detector missed the moment — never break the path over it).
        anchor_radius_m: world-meters gate around each anchor.
        config: :class:`RerankConfig`.

    Returns:
        ``{frame_idx: (x, y)}`` selected ball position in source pixels (frames the track
        coasts as a miss are omitted). ``frame_idx`` is the index into ``frames``.
    """
    if not getattr(geom, "valid", False):
        raise ValueError("rerank requires a valid (non-neutral) homography")
    cfg = config or RerankConfig()
    n = len(frames)
    if n == 0:
        return {}
    gaps = frame_gaps or [1] * n

    fw, fs, fsrc, fsig = [], [], [], []
    for cands in frames:
        xy = np.array([[c.x, c.y] for c in cands], float).reshape(-1, 2)
        fsrc.append(xy)
        fw.append(geom.image_to_world(xy) if len(xy) else np.zeros((0, 2)))
        sc = np.array([c.score for c in cands], float)
        fs.append(sc / (sc.max() + 1e-9) if len(sc) else sc)
        if cfg.phys_sigma_px > 0 and len(xy):
            # measurement noise in METERS: px jitter through the local homography
            # Jacobian (worst direction). Near candidates ~cm-m; far-line ones can
            # legitimately jitter tens of meters — that noise, not ball speed, is
            # what the loose legacy gate was absorbing.
            d_px = 3.0
            w0 = fw[-1]
            wx = geom.image_to_world(xy + [d_px, 0.0])
            wy = geom.image_to_world(xy + [0.0, d_px])
            jac = (
                np.maximum(
                    np.linalg.norm(wx - w0, axis=1), np.linalg.norm(wy - w0, axis=1)
                )
                / d_px
            )
            fsig.append(cfg.phys_sigma_px * jac)
        else:
            fsig.append(np.zeros(len(xy)))

    if motion is not None:
        mw = [
            geom.image_to_world(np.array([[c.x, c.y] for c in m], float).reshape(-1, 2))
            if m
            else np.zeros((0, 2))
            for m in motion
        ]
        fmot = _motion_support(fw, mw, cfg.motion_radius_m)
    else:
        fmot = [np.zeros(len(w)) for w in fw]

    pers = static_persistence(fw, cfg.cell_m)

    def emis(t: int, i: int) -> float:
        if t in allowed and i not in allowed[t]:
            return math.inf  # anchored frame: out-of-radius candidates forbidden
        e = (
            -cfg.alpha * fs[t][i]
            + cfg.static_w * pers[t][i]
            - cfg.motion_w * fmot[t][i]
        )
        if priors is not None and len(priors[t]):
            e += float(priors[t][i])
        return e

    # identity anchors -> per-frame allowed-candidate sets (None = unconstrained)
    allowed: dict[int, set[int]] = {}
    if anchors:
        for t, axy in anchors.items():
            if not (0 <= t < n) or not len(fw[t]):
                continue
            aw = geom.image_to_world(np.asarray([axy], float))[0]
            ok = {
                int(i)
                for i, w in enumerate(fw[t])
                if float(np.linalg.norm(w - aw)) <= anchor_radius_m
            }
            if ok:  # detector missed the anchor moment -> leave the frame unconstrained
                allowed[t] = ok

    def miss(t: int) -> float:
        if t in allowed:
            return math.inf  # anchored frame: the path must take an in-radius candidate
        return float(miss_costs[t]) if miss_costs is not None else cfg.miss_cost

    # Viterbi: state K = miss. cost[t][j], back[t][j]. The miss state additionally
    # carries the world position where its (best-predecessor) path left a candidate
    # plus the source-frames spent missing — a greedy approximation that lets re-entry
    # transitions be distance/time-aware (the aerial bridge) without a state blow-up.
    cost: list[np.ndarray] = []
    back: list[np.ndarray] = []
    k0 = len(frames[0])
    cost.append(np.array([emis(0, i) for i in range(k0)] + [miss(0)]))
    back.append(np.full(k0 + 1, -1, int))
    missw: list[np.ndarray | None] = [None]
    missv: list[np.ndarray | None] = [None]
    missoob: list[np.ndarray | None] = [None]  # pinned boundary-crossing expectation
    missdur: list[float] = [0.0]
    wpoly = _world_polygon(geom) if cfg.oob_w > 0 else None
    img_poly = (
        np.asarray(geom.polygon, np.float32).reshape(-1, 1, 2)
        if (cfg.oob_w > 0 and getattr(geom, "polygon", None) is not None)
        else None
    )

    def _oob_pin(exit_px, exit_w, v_px):
        """Re-entry expectation SPOTS for an exit (``(m, 2)`` world points): the
        boundary crossing (ray-marched when a usable velocity exists, else the
        nearest boundary point for line-hugging exits), expanded by the rule-based
        restart spots when the crossing is on an END line (goal kick / corners).
        None = not an out-of-bounds exit (the aerial cone handles it)."""
        if wpoly is None or img_poly is None:
            return None
        import cv2  # noqa: PLC0415

        inside = (
            cv2.pointPolygonTest(
                img_poly, (float(exit_px[0]), float(exit_px[1])), False
            )
            >= 0
        )
        if not inside:
            cross, edge = _nearest_on_polygon(exit_w, wpoly)
            return _restart_spots(cross, edge, wpoly)
        if v_px is not None:
            step = math.hypot(*v_px)
            if step > 0.3:  # ~sub-noise px motion has no usable direction
                for k in range(1, 61):
                    q = (exit_px[0] + v_px[0] * k, exit_px[1] + v_px[1] * k)
                    if (
                        cv2.pointPolygonTest(
                            img_poly, (float(q[0]), float(q[1])), False
                        )
                        < 0
                    ):
                        qw = geom.image_to_world(np.asarray([q], float))[0]
                        cross, edge = _nearest_on_polygon(qw, wpoly)
                        return _restart_spots(cross, edge, wpoly)
        near_pt, edge = _nearest_on_polygon(exit_w, wpoly)
        if float(np.linalg.norm(exit_w - near_pt)) <= _OOB_NEAR_BOUNDARY_M:
            return _restart_spots(near_pt, edge, wpoly)
        return None

    for t in range(1, n):
        k = len(frames[t])
        kp = len(frames[t - 1])
        ct = np.full(k + 1, np.inf)
        bt = np.full(k + 1, -1, int)
        gap = max(1, gaps[t])
        budget = cfg.vmax_m_per_frame * gap
        for j in range(k + 1):
            e = miss(t) if j == k else emis(t, j)
            best, bi = np.inf, -1
            for i in range(kp + 1):
                if not np.isfinite(cost[t - 1][i]):
                    continue
                if j == k or i == kp:
                    trans = 0.6  # to/from miss: allow coasting an occlusion
                    if (
                        j == k
                        and i == kp
                        and cfg.oob_w > 0
                        and missoob[t - 1] is not None
                    ):
                        # pinned OUT-OF-BOUNDS: waiting at the boundary is the correct
                        # behavior, not a guilty miss — coasting is nearly free
                        trans = 0.1
                    if (
                        j != k
                        and i == kp
                        and cfg.oob_w > 0
                        and missoob[t - 1] is not None
                    ):
                        # out-of-bounds: expectation pinned at the crossing +
                        # rule-based restart spots (nearest one counts)
                        dur = missdur[t - 1] + gap
                        dp = float(
                            np.linalg.norm(missoob[t - 1] - fw[t][j], axis=1).min()
                        )
                        cone = min(_OOB_BASE_M + _OOB_SPREAD_MPF * dur, _OOB_CAP_M)
                        if dp <= cone:
                            trans -= cfg.oob_w * _CONE_BONUS
                    elif (
                        j != k
                        and i == kp
                        and cfg.bridge_w > 0
                        and missw[t - 1] is not None
                    ):
                        dur = missdur[t - 1] + gap
                        d = math.hypot(*(fw[t][j] - missw[t - 1]))
                        x = (d / max(dur, 1.0)) / cfg.air_vmax_mpf
                        if x > 1.0:  # faster than any flight: a true teleport
                            trans += cfg.bridge_w * (x - 1.0) ** 2
                        elif missv[t - 1] is not None:
                            # ballistic landing prediction: exit + v_exit * airtime
                            land = missw[t - 1] + missv[t - 1] * dur
                            dp = math.hypot(*(fw[t][j] - land))
                            if dp <= _CONE_BASE_M + _CONE_SPREAD_MPF * dur:
                                trans -= cfg.bridge_w * _CONE_BONUS
                            elif x >= 0.15:
                                trans -= cfg.bridge_w * _BAND_BONUS
                        elif x >= 0.15:  # no direction known: rate-band only
                            trans -= cfg.bridge_w * _BAND_BONUS
                elif cfg.phys_sigma_px > 0:
                    # physical: real ball motion + depth-dependent measurement noise
                    d = math.hypot(*(fw[t][j] - fw[t - 1][i]))
                    allow = cfg.ball_vmax_mpf * gap + fsig[t][j] + fsig[t - 1][i]
                    if d > 3.0 * allow:
                        continue  # not physical: route through the miss/bridge state
                    trans = (d / allow) ** 2
                else:
                    d = math.hypot(*(fw[t][j] - fw[t - 1][i]))
                    if d > cfg.max_jump_m_per_frame * gap:
                        continue  # teleport forbidden
                    trans = (d / budget) ** 2
                v = cost[t - 1][i] + trans
                if v < best:
                    best, bi = v, i
            ct[j] = e + best
            bt[j] = bi
        cost.append(ct)
        back.append(bt)
        bi_m = int(bt[k])
        if bi_m == kp:  # stayed in miss: keep the frozen exit point, extend duration
            missw.append(missw[t - 1])
            missv.append(missv[t - 1])
            missoob.append(missoob[t - 1])
            missdur.append(missdur[t - 1] + gap)
        elif 0 <= bi_m < kp:  # entered miss from a candidate: freeze where it left
            missw.append(fw[t - 1][bi_m].copy())
            # exit velocity from the entering path's last step (its backpointer),
            # capped at flight speed; sub-launch motion is rolling noise -> no direction
            v = None
            v_px = None
            if t >= 2:
                pv = int(back[t - 1][bi_m])
                if 0 <= pv < len(fw[t - 2]):
                    step = fw[t - 1][bi_m] - fw[t - 2][pv]
                    v = step / max(gaps[t - 1], 1)
                    v_px = (fsrc[t - 1][bi_m] - fsrc[t - 2][pv]) / max(gaps[t - 1], 1)
                    speed = math.hypot(*v)
                    if speed < _LAUNCH_MIN_MPF:
                        v = None
                    elif speed > cfg.air_vmax_mpf:
                        v = v * (cfg.air_vmax_mpf / speed)
            missv.append(v)
            missoob.append(
                _oob_pin(fsrc[t - 1][bi_m], fw[t - 1][bi_m], v_px)
                if cfg.oob_w > 0
                else None
            )
            missdur.append(float(gap))
        else:
            missw.append(None)
            missv.append(None)
            missoob.append(None)
            missdur.append(float(gap))

    path = [int(np.argmin(cost[-1]))]
    for t in range(n - 1, 0, -1):
        path.append(int(back[t][path[-1]]))
    path.reverse()

    preds: dict[int, tuple[float, float]] = {}
    for t, p in enumerate(path):
        if p < len(frames[t]) and len(fsrc[t]):
            preds[t] = (float(fsrc[t][p][0]), float(fsrc[t][p][1]))
    return preds


def track_ball(
    frames: list[list[Candidate]],
    geom: FieldGeometry,
    *,
    motion: list[list[Candidate]] | None = None,
    player_boxes: list[list[tuple[float, float]]] | None = None,
    frame_gaps: list[int] | None = None,
    action_weight: float = 0.5,
    miss_costs: list[float] | None = None,
    config: RerankConfig | None = None,
) -> dict[int, tuple[float, float]]:
    """The full production ball-tracking pipeline (the verified-best config).

    Runs, in order: the player-density :func:`action_density_prior` (if ``player_boxes``
    given) -> the context :func:`rerank` (static-persistence + motion-support + meters-smooth
    Viterbi) -> the :func:`kalman_smooth` RTS smoother / occlusion-coast. Returns a smoothed
    ball position at EVERY frame index in the track span (occluded frames coasted), in source
    pixels — the stable per-frame signal the broadcast renderer's "follow the ball" needs.

    Held-out Spencerport (AutoCam-loses-ball clips, viewport scale R=15 m): **0.58** (leave-
    one-clip-out CV 0.56), vs AutoCam ~0. This is the ceiling of the single-camera context
    tracker — detection augmentation and learned appearance discrimination were both shown not
    to help (see DECISIONS.md D1/D4); the intelligence is here, in context.

    Args:
        frames: per-frame detector candidates (``frames[t]`` = list of :class:`Candidate`).
            Use the **no-aug** detector (highest recall; D1).
        geom: field geometry with a valid homography.
        motion: optional per-frame MOG2 motion blobs (motion-support term).
        player_boxes: optional per-frame YOLO player-box CENTRES in source px (action prior).
        frame_gaps: optional per-frame source-frame gaps (stride-N dumps).
        action_weight: action-prior strength (0.5 optimum; 0 to disable).
        miss_costs: optional per-frame miss cost (see :func:`rerank` — the learned
            selector's ``-log P(no visible ball)``).
        config: :class:`RerankConfig`.
    """
    priors = None
    if player_boxes is not None and action_weight:
        priors = action_density_prior(frames, player_boxes, geom, weight=action_weight)
    sel = rerank(
        frames,
        geom,
        motion=motion,
        frame_gaps=frame_gaps,
        priors=priors,
        miss_costs=miss_costs,
        config=config,
    )
    return kalman_smooth(sel, geom)
