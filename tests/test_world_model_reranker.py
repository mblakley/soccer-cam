"""Tests for the motion-consistency re-ranker (EXP-28).

The crux: a BRIGHT STATIC distractor beats the dim moving ball per-frame (brightness wins),
but is repelled by the static-persistence penalty so the re-ranker follows the ball — the
exact failure mode (lines/tents/benches) that a plain smoothness prior gets backwards.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from training.world_model.eval import evaluate_recall, evaluate_recall_metric
from training.world_model.geometry import build_field_geometry
from training.world_model.reranker import (
    RerankConfig,
    action_density_prior,
    coast_occlusions,
    kalman_smooth,
    rerank,
    static_persistence,
    track_ball,
)
from training.world_model.tbd import Candidate

# A real far-corner field polygon (Spencerport clip-1) -> a valid homography.
_POLY = [
    [270.8, 1071.9],
    [2674.9, 1460.6],
    [3749.7, 1530.2],
    [5468.7, 1481.1],
    [7339.5, 1215.1],
    [5337.4, 327.3],
    [4397.9, 204.6],
    [3803.1, 171.8],
    [3277.9, 175.9],
    [2227.7, 261.8],
]
NEUTRAL = build_field_geometry(None)


def test_static_persistence_high_for_fixed_low_for_moving():
    # candidate 0 fixed at origin; candidate 1 moves 4 units/frame (distinct 2-unit cells)
    frames_world = [np.array([[0.0, 0.0], [4.0 * t, 0.0]]) for t in range(10)]
    pers = static_persistence(frames_world, cell_m=2.0)
    # candidate 0 is the fixed point (in its cell every frame) -> ~1.0
    assert all(p[0] > 0.9 for p in pers)
    # candidate 1 moves through a new cell each frame -> small
    assert pers[5][1] < 0.2


def test_rerank_requires_valid_homography():
    with pytest.raises(ValueError):
        rerank([[Candidate(1.0, 1.0, 1.0)]], NEUTRAL)


def test_rerank_follows_moving_ball_over_bright_static_distractor():
    geom = build_field_geometry(np.asarray(_POLY, dtype=float))
    assert geom.valid
    frames: list[list[Candidate]] = []
    gt: list[tuple[int, float, float]] = []
    bright: dict[int, tuple[float, float]] = {}
    for t in range(12):
        # dim ball drifting smoothly across the mid-field (small per-frame world move)
        bx, by = 3200.0 + 26.0 * t, 1000.0
        # bright STATIC distractor parked at a fixed field point
        dx, dy = 4200.0, 1300.0
        cands = [
            Candidate(dx, dy, 0.9),
            Candidate(bx, by, 0.4),
        ]  # distractor first (brighter)
        frames.append(cands)
        gt.append((t, bx, by))
        bright[t] = (cands[0].x, cands[0].y)  # brightness-argmax = the distractor

    # brightness-argmax locks onto the static distractor -> misses the ball
    b_recall, _, _ = evaluate_recall_metric(bright, gt, geom, radius_m=5.0)
    assert b_recall < 0.2

    # the re-ranker's persistence penalty repels the static distractor -> follows the ball
    preds = rerank(frames, geom, config=RerankConfig())
    r_recall, _, _ = evaluate_recall_metric(
        {f: preds[f] for f in preds}, gt, geom, radius_m=5.0
    )
    assert r_recall > 0.8
    assert r_recall > b_recall + 0.6


def test_rerank_handles_empty_frames_and_misses():
    geom = build_field_geometry(np.asarray(_POLY, dtype=float))
    # a moving ball (low persistence) with one empty (fully-occluded) frame in the middle
    frames = []
    for t in range(7):
        frames.append([] if t == 3 else [Candidate(3000.0 + 40.0 * t, 1000.0, 0.6)])
    preds = rerank(frames, geom)
    assert 3 not in preds  # the empty frame coasts as a miss (no candidate to emit)
    assert (
        sum(f in preds for f in (0, 1, 2, 4, 5, 6)) >= 5
    )  # the visible ball is tracked


def test_rerank_per_frame_miss_costs_gate_the_miss_state():
    """The learned selector's -log P(none) drives the miss state PER FRAME: a cheap miss
    lets the path skip a frame, an expensive miss forces it to pick the candidate."""
    geom = build_field_geometry(np.asarray(_POLY, dtype=float))
    frames = [[Candidate(3000.0 + 40.0 * t, 1000.0, 0.2)] for t in range(6)]

    # candidate emission is weak (alpha low), flat miss would win everywhere at 0.0
    cheap = rerank(
        frames,
        geom,
        miss_costs=[-5.0] * 6,  # "no visible ball" extremely likely every frame
        config=RerankConfig(alpha=0.3),
    )
    assert len(cheap) == 0  # every frame prefers the miss state

    forced = rerank(
        frames,
        geom,
        miss_costs=[5.0] * 6,  # "a ball is clearly visible" every frame
        config=RerankConfig(alpha=0.3),
    )
    assert len(forced) == 6  # every frame must take its candidate

    # per-frame: only frame 2 confidently "none" -> only frame 2 misses
    mixed = rerank(
        frames,
        geom,
        miss_costs=[5.0, 5.0, -5.0, 5.0, 5.0, 5.0],
        config=RerankConfig(alpha=0.3),
    )
    assert 2 not in mixed
    assert sum(f in mixed for f in (0, 1, 3, 4, 5)) == 5


def test_rerank_identity_anchor_propagates_bidirectionally():
    """A kickoff-style anchor pins the path to the anchored object's track; identity
    propagates to every other frame through the smoothness transitions."""
    geom = build_field_geometry(np.asarray(_POLY, dtype=float))
    frames = []
    a_track, b_track = [], []
    for t in range(8):
        a = (2800.0 + 30.0 * t, 1350.0)  # bright decoy track (near-left)
        b = (5600.0 + 30.0 * t, 700.0)  # dim game-ball track, far across the field
        frames.append([Candidate(*a, 0.9), Candidate(*b, 0.4)])
        a_track.append(a)
        b_track.append(b)
    # the tracks are an order of magnitude farther apart than any legal one-frame
    # continuation (ball_vmax + far-line jitter, gap 1) -> switching mid-path is gated
    # out by the physical transition model, so an anchor decides the WHOLE path's identity
    aw = geom.image_to_world(np.asarray(a_track, float))
    bw = geom.image_to_world(np.asarray(b_track, float))
    assert float(np.linalg.norm(aw[3] - bw[4])) > 10.0 * RerankConfig().ball_vmax_mpf

    # unanchored, trusting score: the path follows the bright decoy
    # (persistence/motion off — this test isolates score + smoothness + anchor)
    cfg = RerankConfig(alpha=0.5, static_w=0.0, motion_w=0.0)
    free = rerank(frames, geom, config=cfg)
    assert free[4] == a_track[4]

    # anchored mid-sequence on the dim track: identity propagates BOTH directions
    anchored = rerank(frames, geom, anchors={4: b_track[4]}, config=cfg)
    assert anchored[0] == b_track[0]
    assert anchored[4] == b_track[4]
    assert anchored[7] == b_track[7]

    # an anchor with no candidate in radius is ignored, never breaks the path
    ignored = rerank(
        frames, geom, anchors={4: (300.0, 1071.0)}, config=cfg
    )  # left corner, ~90 m from both tracks
    assert ignored[4] == a_track[4]


@pytest.mark.xfail(
    strict=True,
    reason="the aerial bridge cannot yet distinguish a real ballistic landing from a "
    "rate/cone-consistent distractor: exempting flight-consistent re-entries from the "
    "reacq distance bias makes the bridge land on the true ball here, but regresses "
    "held-out Spencerport ball-in-view 0.830 -> 0.808 (far distractors leak through). "
    "Needs a real-landing-vs-distractor discriminator — the aerial-bridge math effort.",
)
def test_rerank_aerial_bridge_prefers_flight_consistent_landing():
    """EXP-DIST-30 v0: a launched ball leaves the camera's view and lands far upfield.
    The legacy miss re-entry is distance-blind (flat 0.6), so the path re-enters on the
    brighter phantom near the launch point; the aerial bridge freezes the exit position
    and gives the flight-consistent landing a bonus — the track must land with the ball."""
    geom = build_field_geometry(np.asarray(_POLY, float))
    A = (3200.0, 1000.0)  # rolling here, then launched at t=2
    B = (4900.0, 820.0)  # landing zone, far upfield
    E = (3300.0, 1010.0)  # phantom blob by the launch point (brighter than B)

    def w(p):
        return geom.image_to_world(np.asarray([p], float))[0]

    a2 = (A[0] + 16.0, A[1])  # last on-ball position (t=2)
    d_ab = float(np.linalg.norm(w(a2) - w(B)))
    d_ae = float(np.linalg.norm(w(a2) - w(E)))
    # 3 empty dump steps at gap 8 => 32 source frames in the air; air_vmax 2.0 m/f
    assert 0.15 * 2.0 * 32 < d_ab < 1.0 * 2.0 * 32  # flight-consistent landing
    assert d_ae < 0.15 * 2.0 * 32  # phantom re-entry is a near-park

    frames: list[list[Candidate]] = []
    for t in range(3):  # ball rolling at A
        frames.append([Candidate(A[0] + 8.0 * t, A[1], 0.8)])
    for _ in range(3):  # airborne, out of the camera's vertical FOV
        frames.append([])
    for t in range(3):  # landing frames: phantom E brighter than true ball B
        frames.append(
            [
                Candidate(E[0] + 8.0 * t, E[1], 0.7),
                Candidate(B[0] + 8.0 * t, B[1], 0.5),
            ]
        )
    gaps = [8] * len(frames)
    # isolate score + transitions; tight vmax so a mid-landing E<->B hop can't pay
    iso = {
        "alpha": 1.0,
        "static_w": 0.0,
        "motion_w": 0.0,
        "phys_sigma_px": 0.0,
        "ball_vmax_mpf": 3.0,
    }

    legacy = rerank(frames, geom, frame_gaps=gaps, config=RerankConfig(**iso))
    assert legacy[6][0] == pytest.approx(E[0])  # distance-blind: takes the phantom

    bridged = rerank(
        frames,
        geom,
        frame_gaps=gaps,
        config=RerankConfig(**iso, bridge_w=2.0),
    )
    assert bridged[6][0] == pytest.approx(B[0])  # lands WITH the ball
    assert bridged[8][0] == pytest.approx(B[0] + 16.0)


def test_rerank_aerial_bridge_ballistic_cone_uses_launch_direction():
    """EXP-DIST-31 physics upgrade (Mark): with detections of the early flight before
    the ball leaves frame, momentum predicts the landing zone (exit + v * airtime).
    Two re-entry candidates sit at the SAME distance from the exit point — identical
    rate, indistinguishable to the direction-blind band — one along the launch
    direction, one behind. The ballistic cone must land on the forward one even
    though the backward one is brighter; without a trustable launch velocity the
    band is direction-blind and the brighter backward candidate wins."""
    geom = build_field_geometry(np.asarray(_POLY, float))

    def w(p):
        return geom.image_to_world(np.asarray([p], float))[0]

    y = 1000.0
    fast = [(3200.0 + 500.0 * t, y) for t in range(3)]  # visible early flight
    exit_p = fast[-1]
    v_step = (w(fast[2]) - w(fast[1])) / 8.0  # world m per source frame
    speed = float(np.hypot(*v_step))
    assert 0.8 <= speed <= 2.0  # a real launch: direction is trustable

    land = w(exit_p) + v_step * 32.0  # 3 empty steps + landing step, gap 8
    fwd = (exit_p[0] + 500.0 * 4.0, y)  # continues the flight line
    bwd = (exit_p[0] - 500.0 * 4.0, y)  # same distance back the way it came
    d_f, d_b = (float(np.linalg.norm(w(p) - w(exit_p))) for p in (fwd, bwd))
    assert abs(d_f - d_b) / d_f < 0.35  # comparable rates (equal to the band)
    assert float(np.linalg.norm(w(fwd) - land)) <= 6.0 + 0.4 * 32.0  # in the cone
    assert float(np.linalg.norm(w(bwd) - land)) > 6.0 + 0.4 * 32.0  # out of it

    frames: list[list[Candidate]] = [[Candidate(*p, 0.8)] for p in fast]
    frames += [[], [], []]  # airborne, out of the camera's vertical FOV
    for t in range(3):  # landing: backward phantom slightly brighter
        frames.append(
            [
                Candidate(bwd[0] + 8.0 * t, bwd[1], 0.55),
                Candidate(fwd[0] + 8.0 * t, fwd[1], 0.5),
            ]
        )
    gaps = [8] * len(frames)
    iso = {
        "alpha": 1.0,
        "static_w": 0.0,
        "motion_w": 0.0,
        "phys_sigma_px": 0.0,
        "ball_vmax_mpf": 3.0,
    }

    bridged = rerank(
        frames, geom, frame_gaps=gaps, config=RerankConfig(**iso, bridge_w=2.0)
    )
    assert bridged[6][0] == pytest.approx(fwd[0])  # physics: lands ahead

    # same geometry but a slow rolling exit (below the launch threshold): no
    # direction to trust -> band-only, the brighter backward candidate wins
    slow = [(3200.0 + 10.0 * t, y) for t in range(3)]
    frames2 = [[Candidate(*p, 0.8)] for p in slow]
    frames2 += [[], [], []]
    for t in range(3):
        frames2.append(
            [
                Candidate(bwd[0] + 8.0 * t, bwd[1], 0.55),
                Candidate(fwd[0] + 8.0 * t, fwd[1], 0.5),
            ]
        )
    blind = rerank(
        frames2, geom, frame_gaps=gaps, config=RerankConfig(**iso, bridge_w=2.0)
    )
    assert blind[6][0] == pytest.approx(bwd[0] if 6 in blind else bwd[0])


def test_rerank_physical_transitions_reject_impossible_ground_hop():
    """EXP-DIST-31b: the sole candidate->candidate model prices transitions by REAL ball
    speed plus depth-dependent measurement noise. A 41 m instant hop at gap 8 is not a
    ball: the default gate rejects it (the path takes the ~1 m rolling step), whereas a
    loose ball-speed ceiling would chase the bright distractor. A plausible ~13 m step at
    the far line (world jitter + real motion) stays affordable under the default gate."""
    geom = build_field_geometry(np.asarray(_POLY, float))
    iso = {"alpha": 1.0, "static_w": 0.0, "motion_w": 0.0}
    A = (3000.0, 1350.0)
    near = (3060.0, 1350.0)  # ~0.8 m: a rolling ball
    B = (6000.0, 1300.0)  # ~41 m away, much brighter
    frames = [
        [Candidate(*A, 0.5)],
        [Candidate(*near, 0.4), Candidate(*B, 0.9)],
    ]
    # a loose ball-speed ceiling makes the 41 m hop affordable -> chases the bright one
    loose = rerank(
        frames, geom, frame_gaps=[8, 8], config=RerankConfig(**iso, ball_vmax_mpf=30.0)
    )
    assert loose[1][0] == pytest.approx(B[0])

    # the default physical gate: 41 m in 8 frames is not a ball -> takes the rolling step
    phys = rerank(frames, geom, frame_gaps=[8, 8], config=RerankConfig(**iso))
    assert phys[1][0] == pytest.approx(near[0])

    # far line: a 13 m step (world jitter + real motion) must remain affordable
    f1, f2 = (3800.0, 260.0), (4200.0, 250.0)
    far_frames = [[Candidate(*f1, 0.5)], [Candidate(*f2, 0.5)]]
    far = rerank(far_frames, geom, frame_gaps=[8, 8], config=RerankConfig(**iso))
    assert 1 in far and far[1][0] == pytest.approx(f2[0])


def test_rerank_oob_pin_holds_the_boundary_and_reacquires_at_the_crossing():
    """EXP-DIST-35 (Mark's physics): a ball crossing the mask boundary does not
    disappear — the rules bring it back near where it left. The exit's velocity ray
    pins the miss expectation at the boundary crossing; during the dead time a
    brighter mid-field distractor must NOT steal the track, and the re-entry
    candidate near the crossing must win."""
    geom = build_field_geometry(np.asarray(_POLY, float))
    # roll toward the near-left boundary and out: last two tracked frames close to it
    a1, a2 = (700.0, 1150.0), (620.0, 1180.0)
    frames: list[list[Candidate]] = [
        [Candidate(*a1, 0.8)],
        [Candidate(*a2, 0.8)],
    ]
    # ball out of the mask for 4 steps: ONLY a bright mid-field distractor visible
    distract = (3800.0, 900.0)
    for _ in range(4):
        frames.append([Candidate(*distract, 0.7)])
    reentry = (640.0, 1160.0)  # comes back in right where it left
    frames.append([Candidate(*reentry, 0.5), Candidate(*distract, 0.7)])
    gaps = [8] * len(frames)
    # learned-emission stand-in: ball candidates are confident (-log p ~ 0.3), the
    # distractor is not (~0.95) — the hand alpha term can't express this because a
    # lone candidate always normalizes to frame-max
    priors = []
    for fr in frames:
        priors.append(
            np.asarray(
                [0.3 if c.x < 1000 else 0.95 for c in fr],
                float,
            )
        )
    iso = {
        "alpha": 0.0,
        "static_w": 0.0,
        "motion_w": 0.0,
        "phys_sigma_px": 0.0,
        "ball_vmax_mpf": 3.0,
    }

    free = rerank(
        frames, geom, frame_gaps=gaps, priors=priors, config=RerankConfig(**iso)
    )
    # The reacq teleport cap (on by default) forbids parking on the far distractor
    # — 43.6 m from the exit, past the ~32 m one-step cap — so the bright mid-field
    # blob never steals the track: it misses the dead time and re-acquires the ball
    # at the crossing, even without the OOB pin.
    assert all(round(free[t][0]) != round(distract[0]) for t in free)
    assert 3 not in free
    assert free[6][0] == pytest.approx(reentry[0])

    pinned = rerank(
        frames,
        geom,
        frame_gaps=gaps,
        priors=priors,
        config=RerankConfig(**iso, oob_w=2.0),
    )
    assert 3 not in pinned  # coasts the dead time at the pinned crossing
    assert pinned[6][0] == pytest.approx(reentry[0])  # re-acquires at the crossing


def test_rerank_oob_endline_restart_spots_cover_goal_kick():
    """EXP-DIST-35b: an END-LINE exit does not restart at the crossing — the rules
    move the ball to the goal area (goal kick) or a corner. The restart spots must
    let the track re-acquire at the goal-kick spot even though it is far beyond the
    crossing cone (the Irondequoit retrieval failure mode)."""
    import cv2

    from training.world_model.reranker import (
        _nearest_on_polygon,
        _restart_spots,
        _world_polygon,
    )

    geom = build_field_geometry(np.asarray(_POLY, float))
    wpoly = _world_polygon(geom)
    img_poly = np.asarray(geom.polygon, np.float32).reshape(-1, 1, 2)

    exit_px = (7400.0, 1150.0)  # past the right end line, near the near-right corner
    assert cv2.pointPolygonTest(img_poly, exit_px, False) < 0

    def w(p):
        return geom.image_to_world(np.asarray([p], float))[0]

    cross, edge = _nearest_on_polygon(w(exit_px), wpoly)
    assert edge == 4  # the 4->5 edge IS the right end line
    spots = _restart_spots(cross, edge, wpoly)
    assert len(spots) == 4  # crossing + goal-kick spot + both corners
    goal_w = spots[1]
    assert float(np.linalg.norm(goal_w - cross)) > 21.0  # beyond the crossing cone
    gx, gy = geom.world_to_image(np.asarray([goal_w]))[0]

    frames: list[list[Candidate]] = [
        [Candidate(*exit_px, 0.8)],
        [Candidate(*exit_px, 0.8)],
    ]
    distract = (3800.0, 900.0)
    for _ in range(4):
        frames.append([Candidate(*distract, 0.7)])
    frames.append([Candidate(float(gx), float(gy), 0.6), Candidate(*distract, 0.7)])
    priors = [
        np.asarray([0.95 if c.x == distract[0] else 0.3 for c in fr], float)
        for fr in frames
    ]
    iso = {
        "alpha": 0.0,
        "static_w": 0.0,
        "motion_w": 0.0,
        "phys_sigma_px": 0.0,
        "ball_vmax_mpf": 3.0,
    }
    pinned = rerank(
        frames,
        geom,
        frame_gaps=[8] * len(frames),
        priors=priors,
        config=RerankConfig(**iso, oob_w=2.0),
    )
    assert 3 not in pinned  # waits out the retrieval
    assert pinned[6][0] == pytest.approx(float(gx))  # re-acquires at the goal kick


def test_action_density_prior_favours_the_player_cluster():
    geom = build_field_geometry(np.asarray(_POLY, dtype=float))
    # each frame: a candidate in a dense player cluster (the action) vs a far lone-player
    # candidate. Both move smoothly. The action prior should pick the cluster one.
    frames, boxes, gt = [], [], []
    for t in range(8):
        action = (3200.0 + 24.0 * t, 1000.0)  # in the cluster
        lone = (6800.0 - 24.0 * t, 1300.0)  # far lone player
        frames.append(
            [Candidate(*lone, 0.6), Candidate(*action, 0.5)]
        )  # lone is brighter
        boxes.append(
            [
                (3150.0, 980.0),
                (3260.0, 1020.0),
                (3200.0, 1060.0),  # cluster around the action
                (6800.0 - 24.0 * t, 1300.0),
            ]  # the lone player
        )
        gt.append((t, *action))
    priors = action_density_prior(frames, boxes, geom, weight=1.0)
    preds = rerank(frames, geom, priors=priors)
    res = evaluate_recall({f: preds[f] for f in preds}, gt, radius_px=80.0)
    assert res.recall_all > 0.8  # tracks the action cluster, not the far lone player


def test_coast_occlusions_fills_gap_on_straight_path():
    # ball tracked at t=0 and t=4; occluded (missing) at t=1,2,3
    preds = {0: (100.0, 200.0), 4: (140.0, 200.0)}
    out = coast_occlusions(preds)
    assert set(out) == {0, 1, 2, 3, 4}  # gap filled
    assert out[2] == (120.0, 200.0)  # midpoint of the straight coast
    assert out[1][0] == 110.0 and out[3][0] == 130.0  # linear across the gap
    # endpoints unchanged
    assert out[0] == (100.0, 200.0) and out[4] == (140.0, 200.0)


def test_coast_occlusions_leaves_trailing_miss_empty():
    preds = {0: (100.0, 200.0), 1: (110.0, 200.0)}  # no later anchor
    out = coast_occlusions(preds)
    assert set(out) == {0, 1}  # nothing to coast toward -> unchanged


def test_kalman_smooth_reduces_jitter_toward_truth():
    geom = build_field_geometry(np.asarray(_POLY, dtype=float))
    rng = np.random.default_rng(0)
    true = {t: (3000.0 + 30.0 * t, 1000.0) for t in range(20)}  # straight CV track
    noisy = {
        t: (x + rng.normal(0, 18), y + rng.normal(0, 18)) for t, (x, y) in true.items()
    }
    sm = kalman_smooth(noisy, geom, q_accel=1.0, r_meas_m=2.5)

    def err(d):
        return float(
            np.mean(
                [math.hypot(d[t][0] - true[t][0], d[t][1] - true[t][1]) for t in true]
            )
        )

    assert err(sm) < err(
        noisy
    )  # smoother is closer to the true line than the raw picks


def test_kalman_smooth_coasts_occlusion_gap_on_motion_model():
    geom = build_field_geometry(np.asarray(_POLY, dtype=float))
    preds = {t: (3000.0 + 30.0 * t, 1000.0) for t in (0, 1, 2, 6, 7, 8)}  # gap 3,4,5
    sm = kalman_smooth(preds, geom)
    assert set(sm) == set(range(9))  # occluded frames filled by the motion model
    for t in (3, 4, 5):  # filled positions follow the constant-velocity path
        assert abs(sm[t][0] - (3000.0 + 30.0 * t)) < 80


def test_track_ball_full_pipeline_follows_the_ball():
    geom = build_field_geometry(np.asarray(_POLY, dtype=float))
    # dim ball drifting through a player cluster; a bright static distractor parked nearby
    frames, motion, boxes, gt = [], [], [], []
    for t in range(14):
        bx, by = 3200.0 + 24.0 * t, 1000.0
        frames.append(
            [Candidate(4200.0, 1300.0, 0.9), Candidate(bx, by, 0.4)]
        )  # distractor brighter
        motion.append([Candidate(bx, by, 1.0)])  # ball is the moving blob
        boxes.append(
            [(bx - 40, by), (bx + 40, by), (bx, by + 50)]
        )  # players around the ball
        gt.append((t, bx, by))
    preds = track_ball(frames, geom, motion=motion, player_boxes=boxes)
    # smoothed track exists at every frame and follows the ball, not the static distractor
    assert len(preds) >= 12
    res = evaluate_recall({t: preds[t] for t in preds}, gt, radius_px=120.0)
    assert res.recall_all > 0.8


def test_kalman_smooth_passthrough_short_or_invalid():
    geom = build_field_geometry(np.asarray(_POLY, dtype=float))
    assert kalman_smooth({0: (1.0, 1.0)}, geom) == {0: (1.0, 1.0)}  # < 2 points
    two = {0: (1.0, 1.0), 1: (2.0, 2.0)}
    assert kalman_smooth(two, NEUTRAL) == two  # invalid homography -> passthrough


def test_action_density_prior_zero_when_no_players():
    geom = build_field_geometry(np.asarray(_POLY, dtype=float))
    frames = [[Candidate(3200.0, 1000.0, 0.5)], [Candidate(3240.0, 1000.0, 0.5)]]
    priors = action_density_prior(frames, [[], []], geom)
    assert all((p == 0).all() for p in priors)  # no detections -> no prior


def test_motion_support_bonus_breaks_ties_toward_moving_blob():
    geom = build_field_geometry(np.asarray(_POLY, dtype=float))
    # two equally-bright, equally-non-static candidates each frame; only one is near a
    # motion blob — the motion-support bonus should select it.
    frames, motion, gt = [], [], []
    for t in range(8):
        on = (3200.0 + 24.0 * t, 1000.0)  # near a motion blob, drifting
        off = (5000.0 - 24.0 * t, 1200.0)  # no motion support, drifting the other way
        frames.append([Candidate(*on, 0.5), Candidate(*off, 0.5)])
        motion.append([Candidate(on[0], on[1], 1.0)])
        gt.append((t, on[0], on[1]))
    preds = rerank(frames, geom, motion=motion, config=RerankConfig(motion_w=1.0))
    res = evaluate_recall({f: preds[f] for f in preds}, gt, radius_px=60.0)
    assert res.recall_all > 0.8
