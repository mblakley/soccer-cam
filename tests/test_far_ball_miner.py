"""Unit tests for the far-ball gap miner (training/data_prep/far_ball_miner.py).

Fully synthetic — no real video, no GPU, no labels on disk.
"""

import json

import pytest

from training.data_prep.far_ball_miner import (
    FarBallCandidate,
    FarBallMinerConfig,
    FrameDetection,
    SearchBox,
    candidates_to_queue,
    merge_candidates,
    mine_far_ball_gaps,
    queue_to_candidates,
    write_queue_json,
)

# A 4096x1800 panorama-shaped field band: far edge (top) at y=300, near edge at
# y=1500. far third => upper third => y <= 300 + (1200/3) = 700.
FIELD_BAND = (300.0, 1500.0)
FRAME_BOUNDS = (0.0, 4096.0)


def _cfg(**overrides):
    base = FarBallMinerConfig(frame_stride=4, fps=25.0, min_gap_frames=10)
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def _far_moving_then_lost_trajectory():
    """Ball moving UP (toward far) and last seen in the far third, then lost.

    frame_stride=4. Detections at 0,4,8,12 climbing from y=720 to y=660
    (vy=-5 px/frame), last seen at y=660 (< far cutoff 700) -> far field.
    Then a long gap until frame 112 (a 96-frame hole = ~3.8s).
    """
    return [
        FrameDetection(frame_idx=0, x=2000.0, y=720.0, confidence=0.8),
        FrameDetection(frame_idx=4, x=2010.0, y=700.0, confidence=0.8),
        FrameDetection(frame_idx=8, x=2020.0, y=680.0, confidence=0.8),
        FrameDetection(frame_idx=12, x=2030.0, y=660.0, confidence=0.8),
        # ---- big gap (frames 16..108 missing) ----
        FrameDetection(frame_idx=112, x=2200.0, y=640.0, confidence=0.7),
        FrameDetection(frame_idx=116, x=2210.0, y=650.0, confidence=0.7),
    ]


def test_far_moving_then_lost_is_candidate():
    cfg = _cfg()
    cands = mine_far_ball_gaps(
        _far_moving_then_lost_trajectory(),
        FIELD_BAND,
        cfg,
        game_id="flash__2025.06.02",
        segment="seg01",
        frame_bounds=FRAME_BOUNDS,
    )
    assert len(cands) == 1
    c = cands[0]
    assert c.classification == "far_ball"
    assert c.game_id == "flash__2025.06.02"
    assert c.segment == "seg01"
    # Gap is the missing span between detection at frame 12 and frame 112.
    assert c.last_seen_frame == 12
    assert c.start_frame == 16  # 12 + stride
    assert c.end_frame == 108  # 112 - stride
    # 96 missing frames -> duration_frames includes the stride span.
    assert c.duration_frames == 96
    assert c.duration_seconds == pytest.approx(96 / 25.0, abs=1e-3)
    # Velocity is upward (toward far): vy negative.
    vx, vy = c.pre_gap_velocity
    assert vy < 0
    assert vx > 0
    assert "toward far" in c.reason


def test_extrapolation_continues_upward_and_clamps_to_far_field():
    cfg = _cfg(search_box_half_size=50.0)
    c = mine_far_ball_gaps(
        _far_moving_then_lost_trajectory(), FIELD_BAND, cfg, frame_bounds=FRAME_BOUNDS
    )[0]
    assert c.extrapolated_regions, "should seed at least one search box"
    boxes = c.extrapolated_regions
    # Sample frames are inside the gap.
    for b in boxes:
        assert c.start_frame <= b.frame_idx <= c.end_frame
    # Linear extrapolation of vy=-5 keeps moving up, but is clamped to the
    # far-field band: cy never above the far edge (y_top=300) and never below
    # the far-field cutoff (700).
    far_cutoff = FIELD_BAND[0] + (1.0 / 3.0) * (FIELD_BAND[1] - FIELD_BAND[0])
    for b in boxes:
        assert FIELD_BAND[0] <= b.cy <= far_cutoff
        # Box x stays within frame bounds.
        assert FRAME_BOUNDS[0] <= b.x0 <= b.x1 <= FRAME_BOUNDS[1]
    # The first sampled box centre should be near the last-seen x (2030) plus a
    # little drift, not wildly off.
    assert boxes[0].cx > 2030.0


def test_occlusion_downward_gap_is_not_candidate():
    """Ball last seen in far field but moving DOWN (toward near) -> occlusion."""
    traj = [
        FrameDetection(0, 2000.0, 660.0, 0.8),
        FrameDetection(4, 2010.0, 680.0, 0.8),
        FrameDetection(8, 2020.0, 700.0, 0.8),
        FrameDetection(12, 2030.0, 720.0, 0.8),  # moving DOWN, vy positive
        FrameDetection(112, 2200.0, 900.0, 0.7),
    ]
    cands = mine_far_ball_gaps(traj, FIELD_BAND, _cfg(), frame_bounds=FRAME_BOUNDS)
    assert cands == []


def test_near_field_moving_far_is_not_candidate():
    """Ball moving up but last seen in the NEAR field (below far cutoff)."""
    traj = [
        FrameDetection(0, 2000.0, 1400.0, 0.8),
        FrameDetection(4, 2010.0, 1380.0, 0.8),
        FrameDetection(8, 2020.0, 1360.0, 0.8),
        FrameDetection(12, 2030.0, 1340.0, 0.8),  # vy<0 but y=1340 >> cutoff 700
        FrameDetection(112, 2200.0, 1300.0, 0.7),
    ]
    cands = mine_far_ball_gaps(traj, FIELD_BAND, _cfg(), frame_bounds=FRAME_BOUNDS)
    assert cands == []


def test_short_gap_below_min_is_ignored():
    """An 8-frame gap (< min_gap_frames=10) is not mined even if far+up."""
    traj = [
        FrameDetection(0, 2000.0, 720.0, 0.8),
        FrameDetection(4, 2010.0, 700.0, 0.8),
        FrameDetection(8, 2020.0, 680.0, 0.8),
        FrameDetection(12, 2030.0, 660.0, 0.8),
        # gap of only 8 missing frames (16..20), reappear at 24
        FrameDetection(24, 2040.0, 650.0, 0.8),
    ]
    cands = mine_far_ball_gaps(traj, FIELD_BAND, _cfg(), frame_bounds=FRAME_BOUNDS)
    assert cands == []


def test_priority_scales_with_gap_duration_and_caps():
    cfg = _cfg(priority_saturation_seconds=10.0, max_priority=100.0)

    def far_then_gap(reappear_frame):
        return [
            FrameDetection(0, 2000.0, 720.0, 0.8),
            FrameDetection(4, 2010.0, 700.0, 0.8),
            FrameDetection(8, 2020.0, 680.0, 0.8),
            FrameDetection(12, 2030.0, 660.0, 0.8),
            FrameDetection(reappear_frame, 2100.0, 650.0, 0.8),
        ]

    # Short gap (~1.6s) vs long gap (~20s, saturates the 10s cap).
    short = mine_far_ball_gaps(far_then_gap(60), FIELD_BAND, cfg)[0]
    long = mine_far_ball_gaps(far_then_gap(512), FIELD_BAND, cfg)[0]
    assert 0 < short.priority < long.priority
    assert long.priority == 100.0  # saturated at the cap


def test_candidates_sorted_by_priority_descending():
    cfg = _cfg(priority_saturation_seconds=10.0)
    # One trajectory with a short far-gap then a long far-gap. Both qualify.
    traj = [
        FrameDetection(0, 2000.0, 720.0, 0.8),
        FrameDetection(4, 2010.0, 700.0, 0.8),
        FrameDetection(8, 2020.0, 680.0, 0.8),
        FrameDetection(12, 2030.0, 660.0, 0.8),
        # short gap (reappear @ 60)
        FrameDetection(60, 2100.0, 650.0, 0.8),
        FrameDetection(64, 2090.0, 640.0, 0.8),  # vy<0 again, far
        FrameDetection(68, 2080.0, 630.0, 0.8),
        FrameDetection(72, 2070.0, 620.0, 0.8),
        # long gap (reappear @ 600)
        FrameDetection(600, 2300.0, 610.0, 0.8),
    ]
    cands = mine_far_ball_gaps(traj, FIELD_BAND, cfg)
    assert len(cands) == 2
    assert cands[0].priority >= cands[1].priority
    assert cands[0].duration_frames > cands[1].duration_frames


def test_sparse_and_explicit_none_inputs_are_equivalent():
    """A sparse trajectory (absent frames) == one with explicit None holes."""
    sparse = _far_moving_then_lost_trajectory()
    # Build the same trajectory but with explicit None records filling the gap.
    with_holes = list(sparse)
    for fi in range(16, 112, 4):
        with_holes.append(FrameDetection(frame_idx=fi, x=None, y=None, confidence=0.0))

    cfg = _cfg()
    a = mine_far_ball_gaps(sparse, FIELD_BAND, cfg, frame_bounds=FRAME_BOUNDS)
    b = mine_far_ball_gaps(with_holes, FIELD_BAND, cfg, frame_bounds=FRAME_BOUNDS)
    assert len(a) == len(b) == 1
    assert a[0].start_frame == b[0].start_frame
    assert a[0].end_frame == b[0].end_frame
    assert a[0].pre_gap_velocity == b[0].pre_gap_velocity


def test_tuple_and_dict_record_shapes_accepted():
    """Loose (frame, x, y, conf) tuples and dicts coerce identically."""
    tuples = [
        (0, 2000.0, 720.0, 0.8),
        (4, 2010.0, 700.0, 0.8),
        (8, 2020.0, 680.0, 0.8),
        (12, 2030.0, 660.0, 0.8),
        (112, 2200.0, 640.0, 0.7),
    ]
    dicts = [
        {"frame_idx": 0, "x": 2000.0, "y": 720.0, "confidence": 0.8},
        {"frame_idx": 4, "x": 2010.0, "y": 700.0, "confidence": 0.8},
        {"frame_idx": 8, "x": 2020.0, "y": 680.0, "confidence": 0.8},
        {"frame_idx": 12, "x": 2030.0, "y": 660.0, "confidence": 0.8},
        {"frame_idx": 112, "x": 2200.0, "y": 640.0, "confidence": 0.7},
    ]
    cfg = _cfg()
    ct = mine_far_ball_gaps(tuples, FIELD_BAND, cfg)
    cd = mine_far_ball_gaps(dicts, FIELD_BAND, cfg)
    assert len(ct) == len(cd) == 1
    assert ct[0].start_frame == cd[0].start_frame
    assert ct[0].pre_gap_velocity == cd[0].pre_gap_velocity


def test_far_field_fraction_is_resolution_independent():
    """Same physical geometry at 2x resolution yields the same classification.

    A trajectory in a band [300,1500] and the SAME trajectory scaled 2x in a
    band [600,3000] must both be classified far-ball, because the far-field
    threshold is a FRACTION of the band, not an absolute pixel.
    """
    cfg = _cfg()
    base = _far_moving_then_lost_trajectory()
    scaled = [
        FrameDetection(d.frame_idx, d.x * 2, d.y * 2, d.confidence)
        if d.is_detection
        else d
        for d in base
    ]
    a = mine_far_ball_gaps(base, (300.0, 1500.0), cfg)
    b = mine_far_ball_gaps(scaled, (600.0, 3000.0), cfg)
    assert len(a) == len(b) == 1
    assert a[0].classification == b[0].classification == "far_ball"

    # And an ABSOLUTE override changes the cutoff: with far_field_y_abs=500, the
    # last-seen y=660 is no longer "far" -> not a candidate.
    cfg_abs = _cfg(far_field_y_abs=500.0)
    assert mine_far_ball_gaps(base, (300.0, 1500.0), cfg_abs) == []


def test_json_serialization_round_trip():
    cfg = _cfg()
    cands = mine_far_ball_gaps(
        _far_moving_then_lost_trajectory(),
        FIELD_BAND,
        cfg,
        game_id="flash__2025.06.02",
        segment="seg01",
        frame_bounds=FRAME_BOUNDS,
    )
    # Candidate dict <-> JSON text <-> Candidate.
    text = json.dumps([c.to_dict() for c in cands])
    restored = [FarBallCandidate.from_dict(d) for d in json.loads(text)]
    assert len(restored) == 1
    r = restored[0]
    orig = cands[0]
    assert r.start_frame == orig.start_frame
    assert r.end_frame == orig.end_frame
    assert r.priority == orig.priority
    assert r.last_seen_xy == orig.last_seen_xy
    assert r.pre_gap_velocity == orig.pre_gap_velocity
    assert r.field_band == orig.field_band
    assert all(isinstance(b, SearchBox) for b in r.extrapolated_regions)
    assert r.extrapolated_regions[0].cx == orig.extrapolated_regions[0].cx


def test_queue_serialization_shape_and_round_trip():
    cfg = _cfg()
    cands = mine_far_ball_gaps(
        _far_moving_then_lost_trajectory(),
        FIELD_BAND,
        cfg,
        game_id="flash__2025.06.02",
        segment="seg01",
    )
    rows = candidates_to_queue(cands)
    assert len(rows) == 1
    row = rows[0]
    # Shape compatible with flywheel.priority_queue consumers.
    assert row["game_id"] == "flash__2025.06.02"
    assert row["segment"] == "seg01"
    assert row["frame_start"] == cands[0].start_frame
    assert row["priority"] == cands[0].priority
    assert row["queue_kind"] == "far_ball"
    assert row["reviewed"] is False
    assert row["sample_frame_indices"]
    # Round-trip back to candidates.
    back = queue_to_candidates(rows)
    assert len(back) == 1
    assert back[0].start_frame == cands[0].start_frame
    assert back[0].priority == cands[0].priority


def test_write_queue_json_file(tmp_path):
    cfg = _cfg()
    cands = mine_far_ball_gaps(
        _far_moving_then_lost_trajectory(), FIELD_BAND, cfg, game_id="g1"
    )
    out = tmp_path / "sub" / "far_ball_queue.json"
    n = write_queue_json(cands, out)
    assert n == 1
    assert out.exists()
    data = json.loads(out.read_text())
    assert data[0]["queue_kind"] == "far_ball"
    assert data[0]["game_id"] == "g1"


def test_merge_candidates_across_games_sorted():
    cfg = _cfg(priority_saturation_seconds=10.0)

    def far_then_gap(reappear, game):
        traj = [
            FrameDetection(0, 2000.0, 720.0, 0.8),
            FrameDetection(4, 2010.0, 700.0, 0.8),
            FrameDetection(8, 2020.0, 680.0, 0.8),
            FrameDetection(12, 2030.0, 660.0, 0.8),
            FrameDetection(reappear, 2100.0, 650.0, 0.8),
        ]
        return mine_far_ball_gaps(traj, FIELD_BAND, cfg, game_id=game)

    g1 = far_then_gap(60, "flash__a")  # short gap, low priority
    g2 = far_then_gap(512, "heat__b")  # long gap, saturated priority
    merged = merge_candidates(g1, g2)
    assert len(merged) == 2
    assert merged[0].game_id == "heat__b"  # higher priority first
    assert merged[0].priority >= merged[1].priority


def test_empty_and_single_detection_trajectories():
    cfg = _cfg()
    assert mine_far_ball_gaps([], FIELD_BAND, cfg) == []
    assert mine_far_ball_gaps([FrameDetection(0, 1.0, 1.0, 0.5)], FIELD_BAND, cfg) == []
