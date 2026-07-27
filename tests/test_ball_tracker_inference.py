"""Selection/tracking stack (product): physics Viterbi + RTS smoother basics."""

from __future__ import annotations

import numpy as np
import pytest

from video_grouper.inference.ball_tracker import (
    Candidate,
    RerankConfig,
    kalman_smooth,
    rerank,
    static_persistence,
)
from video_grouper.inference.world_geometry import build_field_geometry

SRC_W, SRC_H = 1920, 1080
POLY = np.array(
    [
        [100.0, 1000.0],
        [500.0, 1010.0],
        [960.0, 1015.0],
        [1420.0, 1010.0],
        [1820.0, 1000.0],
        [1600.0, 300.0],
        [1280.0, 295.0],
        [960.0, 290.0],
        [640.0, 295.0],
        [320.0, 300.0],
    ],
    float,
)


def _geom():
    g = build_field_geometry(POLY)
    assert g.valid
    return g


def _moving_vs_static_frames(n=30):
    """A ball moving smoothly along the field + a static distractor, per frame."""
    frames = []
    for t in range(n):
        ball = Candidate(x=400.0 + 30.0 * t, y=700.0, score=0.4)
        static = Candidate(x=1200.0, y=650.0, score=0.9)  # brighter, never moves
        frames.append([ball, static])
    return frames


def test_static_persistence_flags_the_fixed_distractor():
    geom = _geom()
    frames = _moving_vs_static_frames()
    world = [
        geom.image_to_world(np.array([[c.x, c.y] for c in cs], float)) for cs in frames
    ]
    pers = static_persistence(world, cell_m=2.0)
    assert np.mean([p[1] for p in pers]) > 0.9  # the static candidate
    assert np.mean([p[0] for p in pers]) < 0.4  # the moving ball


def test_rerank_prefers_the_moving_ball_over_the_bright_static():
    geom = _geom()
    frames = _moving_vs_static_frames()
    preds = rerank(frames, geom, config=RerankConfig())
    picks_ball = sum(
        1 for t, (x, _y) in preds.items() if abs(x - (400.0 + 30.0 * t)) < 1.0
    )
    assert picks_ball >= 0.9 * len(preds)


def test_physical_transitions_forbid_teleports():
    """With phys transitions on, a distant one-frame flicker cannot be taken."""
    geom = _geom()
    frames = _moving_vs_static_frames()
    # Insert a very bright far-corner flicker mid-track.
    frames[15] = [frames[15][0], Candidate(x=1800.0, y=310.0, score=5.0)]
    cfg = RerankConfig(alpha=1.0, phys_sigma_px=5.0)
    preds = rerank(frames, geom, config=cfg)
    if 15 in preds:
        assert abs(preds[15][0] - (400.0 + 30.0 * 15)) < 200.0


def test_kalman_smooth_fills_gaps_and_dejitters():
    geom = _geom()
    preds = {
        t: (400.0 + 30.0 * t + (5.0 if t % 2 else -5.0), 700.0)
        for t in range(20)
        if t not in (8, 9, 10)
    }
    sm = kalman_smooth(preds, geom)
    assert set(sm) == set(range(20))  # occlusion coasted
    xs = [sm[t][0] for t in range(20)]
    steps = np.diff(xs)
    assert np.std(steps) < 10.0  # de-jittered vs the ±5 px input wobble


def test_offfield_gate_suppresses_offfield_distractor():
    """A bright STATIC distractor above the far touchline (off-field) must not be
    selected during in-field play when the off-field state gate is on."""
    geom = _geom()
    frames = []
    for t in range(30):
        ball = Candidate(
            x=400.0 + 30.0 * t, y=700.0, score=0.4
        )  # in-field, moving, dim
        distractor = Candidate(
            x=960.0, y=200.0, score=0.95
        )  # OFF-field, bright, static
        frames.append([ball, distractor])
    cfg = RerankConfig(offfield_gate=True, offfield_penalty=6.0, static_w=2.0)
    preds = rerank(frames, geom, config=cfg)
    at_distractor = sum(
        1 for x, y in preds.values() if abs(x - 960.0) < 5 and abs(y - 200.0) < 5
    )
    assert at_distractor == 0
    # and it still follows the in-field ball
    on_ball = sum(
        1 for t, (x, _y) in preds.items() if abs(x - (400.0 + 30.0 * t)) < 30.0
    )
    assert on_ball >= 0.8 * len(preds)


# ---------------------------------------------------------------------------
# trajectory/2 seam: the return_states opt-ins must not perturb selection
# ---------------------------------------------------------------------------


def _frames_with_occlusion(n=30, gap=(12, 16)):
    """Moving ball + static distractor, with the ball's candidates removed over
    ``gap`` (an occlusion: the Viterbi coasts it as a miss)."""
    frames = _moving_vs_static_frames(n)
    for t in range(*gap):
        frames[t] = []
    return frames


def test_rerank_return_states_leaves_selection_identical():
    geom = _geom()
    frames = _frames_with_occlusion()
    base = rerank(frames, geom, config=RerankConfig())
    preds, conf = rerank(frames, geom, config=RerankConfig(), return_states=True)
    assert preds == base  # exact same selection, frame for frame
    assert set(conf) == set(preds)
    assert all(0.0 <= c <= 1.0 for c in conf.values())
    assert all(t not in preds for t in range(12, 16))  # occlusion stays a miss


def test_kalman_smooth_return_states_tags_fills():
    geom = _geom()
    preds = {
        t: (400.0 + 30.0 * t + (5.0 if t % 2 else -5.0), 700.0)
        for t in range(20)
        if t not in (8, 9, 10)
    }
    base = kalman_smooth(preds, geom)
    sm, states = kalman_smooth(preds, geom, return_states=True)
    assert sm == base  # positions identical with the opt-in
    assert all(states[t] == "C" for t in (8, 9, 10))  # in-span coast fills
    assert all(states[t] == "T" for t in sm if t not in (8, 9, 10))


def test_track_ball_return_states_identity_and_channels():
    from video_grouper.inference.ball_tracker import track_ball

    geom = _geom()
    frames = _frames_with_occlusion()
    base = track_ball(frames, geom)
    track, states, conf = track_ball(frames, geom, return_states=True)
    assert track == base  # the opt-in changes NOTHING about the track
    assert set(states) == set(track) and set(conf) == set(track)
    for t in range(12, 16):  # occlusion interior: coasted, zero confidence
        assert states[t] == "C" and conf[t] == 0.0
    t_frames = [t for t, s in states.items() if s == "T"]
    assert len(t_frames) >= 20
    assert all(conf[t] > 0.0 for t in t_frames)


def test_candidate_dispersion_rms():
    from video_grouper.inference.ball_tracker import candidate_dispersion

    frames = [
        [],  # no candidates -> no signal
        [Candidate(x=100.0, y=100.0, score=1.0)],  # single: zero-spread cloud
        [
            Candidate(x=0.0, y=0.0, score=1.0),
            Candidate(x=300.0, y=400.0, score=1.0),
        ],
    ]
    disp = candidate_dispersion(frames)
    assert disp[0] is None
    assert disp[1] == 0.0
    assert disp[2] == 250.0  # both 250 px from the (150, 200) centroid


# ---------------------------------------------------------------------------
# W3 stage-1 (task #22): state-dependent miss-ENTRY cost arms (EXP-OP-20)
# ---------------------------------------------------------------------------


def _scramble_frames(n=30, window=range(10, 15), y=940.0, drift=0.5):
    """One slow candidate drifting near the bottom (near band); its emission
    cost is controlled via priors, so the scramble is a pure cost story."""
    return [[Candidate(x=500.0 + drift * t, y=y, score=0.5)] for t in range(n)]


def _scramble_run(cfg, y=940.0, cand_cost=1.5, window=range(10, 15), n=30):
    """Track cost 0.3 outside the window, ``cand_cost`` inside (P(ball)
    collapses); miss floor 0.7 throughout. Returns the window frames the
    track KEPT (did not enter miss over)."""
    geom = _geom()
    frames = _scramble_frames(n=n, y=y)
    priors = [np.array([cand_cost if t in window else 0.3], float) for t in range(n)]
    miss_costs = [0.7] * n
    preds = rerank(frames, geom, priors=priors, miss_costs=miss_costs, config=cfg)
    return [t for t in window if t in preds]


def test_miss_entry_near_arm_holds_the_near_slow_scramble():
    geom = _geom()
    diam = float(geom.expected_ball_diameter_px(np.array([[500.0, 940.0]]))[0])
    base = RerankConfig(alpha=0.0, motion_w=0.0, static_w=0.0)
    # default: the brief P(ball) collapse makes the miss path cheaper -> the
    # tracker abandons a candidate that never moved (the near-autopsy class)
    assert _scramble_run(base) == []
    # arm N: entering miss over a NEAR+SLOW top candidate is expensive -> held
    armed = RerankConfig(
        alpha=0.0,
        motion_w=0.0,
        static_w=0.0,
        miss_entry_near_k=4.0,
        miss_entry_near_diam_px=diam * 0.9,
    )
    assert len(_scramble_run(armed)) == 5


def test_miss_entry_near_arm_does_not_fire_far():
    geom = _geom()
    diam_near = float(geom.expected_ball_diameter_px(np.array([[500.0, 940.0]]))[0])
    diam_far = float(geom.expected_ball_diameter_px(np.array([[500.0, 320.0]]))[0])
    assert diam_far < diam_near  # perspective gradient sanity
    armed = RerankConfig(
        alpha=0.0,
        motion_w=0.0,
        static_w=0.0,
        miss_entry_near_k=4.0,
        miss_entry_near_diam_px=diam_far * 1.5,  # far candidate sits BELOW the gate
    )
    # the same scramble at the FAR touchline: the near arm must not rescue it
    assert _scramble_run(armed, y=320.0) == []


def test_miss_entry_multiplier_math():
    from video_grouper.inference.ball_tracker import miss_entry_multiplier

    both = RerankConfig(
        miss_entry_near_k=4.0,
        miss_entry_margin_k=2.0,
        miss_entry_near_diam_px=15.0,
        miss_entry_slow_mpf=0.15,
    )
    # arm N: near AND slow -> 1 + k_N; unknown step counts as slow
    assert miss_entry_multiplier(
        both, diam_px=20.0, step_mpf=0.1, e_top=1.0, e_second=None, floor=0.5
    ) == pytest.approx(5.0)
    assert miss_entry_multiplier(
        both, diam_px=20.0, step_mpf=None, e_top=1.0, e_second=None, floor=0.5
    ) == pytest.approx(5.0)
    # near but FAST, or slow but FAR -> arm N silent
    assert miss_entry_multiplier(
        both, diam_px=20.0, step_mpf=0.5, e_top=1.0, e_second=None, floor=0.5
    ) == pytest.approx(1.0)
    assert miss_entry_multiplier(
        both, diam_px=8.0, step_mpf=0.1, e_top=1.0, e_second=None, floor=0.5
    ) == pytest.approx(1.0)
    # arm M: adv = (floor - e_top)/floor clipped to [0,1]; lone candidate sep=1
    m = miss_entry_multiplier(
        both, diam_px=8.0, step_mpf=0.5, e_top=0.35, e_second=None, floor=0.7
    )
    assert m == pytest.approx(1.0 + 2.0 * 0.5)
    # rank-1 NOT beating the floor -> adv 0 -> silent (the near-autopsy class
    # belongs to arm N, per the design)
    assert miss_entry_multiplier(
        both, diam_px=8.0, step_mpf=0.5, e_top=1.2, e_second=None, floor=0.7
    ) == pytest.approx(1.0)
    # separation scales the margin: e2 close to e1 -> small sep
    tight = miss_entry_multiplier(
        both, diam_px=8.0, step_mpf=0.5, e_top=0.35, e_second=0.36, floor=0.7
    )
    wide = miss_entry_multiplier(
        both, diam_px=8.0, step_mpf=0.5, e_top=0.35, e_second=3.5, floor=0.7
    )
    assert 1.0 < tight < wide <= 1.0 + 2.0 * 0.5
    # anchored/invalid floor disables arm M; both arms can stack
    assert miss_entry_multiplier(
        both, diam_px=8.0, step_mpf=0.5, e_top=0.35, e_second=None, floor=float("inf")
    ) == pytest.approx(1.0)
    stacked = miss_entry_multiplier(
        both, diam_px=20.0, step_mpf=0.1, e_top=0.35, e_second=None, floor=0.7
    )
    assert stacked == pytest.approx(5.0 * 2.0)
    # defaults (both k = 0) are exactly 1.0
    assert (
        miss_entry_multiplier(
            RerankConfig(),
            diam_px=20.0,
            step_mpf=0.0,
            e_top=0.1,
            e_second=None,
            floor=0.7,
        )
        == 1.0
    )


def test_miss_entry_defaults_are_bit_identical():
    geom = _geom()
    frames = _moving_vs_static_frames()
    plain = rerank(frames, geom, config=RerankConfig())
    with_fields = rerank(
        frames,
        geom,
        config=RerankConfig(miss_entry_near_k=0.0, miss_entry_margin_k=0.0),
    )
    assert plain == with_fields
