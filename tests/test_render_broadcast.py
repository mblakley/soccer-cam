"""Control-logic tests for the cylindrical broadcast render step.

Focus on the pure per-frame camera math: velocity derivation, field-zone
classification, the vertical-containment guarantee ("the ball is always inside
the rendered vertical FOV"), vertical-tracking behaviour, and missing-ball
zoom-out. The cylindrical projection itself is covered by
``test_cylindrical_view.py``.
"""

from __future__ import annotations

from video_grouper.inference.cylindrical_view import pixel_to_yaw_pitch
from video_grouper.pipeline.steps.render import (
    BROADCAST_MODE,
    RenderStepConfig,
    _CameraState,
    _classify_zone,
    _resolve_geometry,
    _tick,
    _zone_base_zoom,
    compute_entries,
)

SRC_W, SRC_H = 7680, 2160


def _geom(cfg, polygon=None):
    return _resolve_geometry(SRC_W, SRC_H, cfg, polygon)


def _view_vfov(view_hfov: float, cfg: RenderStepConfig) -> float:
    return view_hfov * cfg.render_output_height / cfg.render_output_width


# ---------------------------------------------------------------------------
# Velocity / entries
# ---------------------------------------------------------------------------


def test_compute_entries_velocity_and_gaps():
    traj = [[100.0, 100.0], [120.0, 100.0], None, [160.0, 100.0]]
    entries = compute_entries(traj, velocity_ema=0.3)
    assert entries[0] == (100.0, 100.0, 0.0, 0.0)  # first frame: no velocity yet
    assert entries[1][2] > 0  # moving right → positive vx
    assert entries[2] is None  # gap preserved
    assert entries[3] is not None and entries[3][2] > 0  # velocity carries the gap


def test_compute_entries_accepts_dict_rows():
    traj = [{"x": 10.0, "y": 20.0}, {"x": 30.0, "y": 20.0}]
    entries = compute_entries(traj, velocity_ema=0.3)
    assert entries[0][0] == 10.0 and entries[1][2] > 0


# ---------------------------------------------------------------------------
# Zone classification
# ---------------------------------------------------------------------------


def test_zone_classification_boundaries():
    m = BROADCAST_MODE
    assert _classify_zone(0.05, m) == "left_box"
    assert _classify_zone(0.20, m) == "left_third"
    assert _classify_zone(0.50, m) == "midfield"
    assert _classify_zone(0.80, m) == "right_third"
    assert _classify_zone(0.95, m) == "right_box"


def test_box_is_tighter_than_midfield():
    m = BROADCAST_MODE
    assert _zone_base_zoom("left_box", m) < _zone_base_zoom("midfield", m)


# ---------------------------------------------------------------------------
# Vertical-containment guarantee — "always show the ball"
# ---------------------------------------------------------------------------


def _ball_in_vertical_fov(cfg, py):
    """Run one tick for a ball at (centre-x, py) and return whether its pitch
    lands inside the rendered vertical FOV."""
    geom = _geom(cfg)
    state = _CameraState()
    px = SRC_W / 2
    entry = (px, py, 0.0, 0.0)
    yaw, pitch, view_hfov = _tick(
        state, entry, SRC_W, SRC_H, geom, BROADCAST_MODE, cfg, -90.0, 90.0, None
    )
    _, ball_pitch = pixel_to_yaw_pitch(px, py, SRC_W, SRC_H, geom.src_hfov_deg)
    half_vfov = _view_vfov(view_hfov, cfg) / 2.0
    return abs(ball_pitch - pitch) <= half_vfov + 1e-6


def test_horizontal_mode_keeps_far_side_ball_in_frame():
    """No tilt: a far-side ball must still be inside the vertical FOV (zoom out)."""
    cfg = RenderStepConfig(render_vertical_tracking=False)
    # py from near the top (far side) through mid to lower field.
    for py in (400, 700, 1080, 1500, 1760):
        assert _ball_in_vertical_fov(cfg, py), f"ball at py={py} left the frame"


def test_tracking_mode_keeps_far_side_ball_in_frame():
    cfg = RenderStepConfig(render_vertical_tracking=True)
    for py in (400, 700, 1080, 1500, 1760):
        assert _ball_in_vertical_fov(cfg, py), f"ball at py={py} left the frame"


def test_horizontal_mode_zooms_wider_for_edge_ball():
    """A ball near a vertical edge forces a wider view than a centred ball."""
    cfg = RenderStepConfig(render_vertical_tracking=False)
    geom = _geom(cfg)

    def hfov_for(py):
        st = _CameraState()
        _, _, h = _tick(
            st,
            (SRC_W / 2, py, 0.0, 0.0),
            SRC_W,
            SRC_H,
            geom,
            BROADCAST_MODE,
            cfg,
            -90.0,
            90.0,
            None,
        )
        return h

    centre = hfov_for(SRC_H / 2)  # ball at field centre → can be tight
    edge = hfov_for(400)  # ball near far edge → must widen
    assert edge >= centre


# ---------------------------------------------------------------------------
# Vertical tracking vs fixed pitch
# ---------------------------------------------------------------------------


def test_vertical_tracking_moves_pitch_toward_ball():
    geom_cfg = RenderStepConfig(
        render_vertical_tracking=True, render_view_pitch_deg=0.0
    )
    geom = _geom(geom_cfg)
    state = _CameraState()
    px, py = SRC_W / 2, 400.0  # far-side ball, negative pitch
    _, ball_pitch = pixel_to_yaw_pitch(px, py, SRC_W, SRC_H, geom.src_hfov_deg)
    pitch = 0.0
    for _ in range(50):
        _, pitch, _ = _tick(
            state,
            (px, py, 0.0, 0.0),
            SRC_W,
            SRC_H,
            geom,
            BROADCAST_MODE,
            geom_cfg,
            -90.0,
            90.0,
            None,
        )
    # Pitch should have migrated from 0 toward the (negative) ball pitch.
    assert ball_pitch < 0
    assert pitch < -1.0


def test_horizontal_mode_holds_base_pitch():
    cfg = RenderStepConfig(render_vertical_tracking=False, render_view_pitch_deg=0.0)
    geom = _geom(cfg)
    state = _CameraState()
    _, pitch, _ = _tick(
        state,
        (SRC_W / 2, 400.0, 0.0, 0.0),
        SRC_W,
        SRC_H,
        geom,
        BROADCAST_MODE,
        cfg,
        -90.0,
        90.0,
        None,
    )
    assert pitch == 0.0  # never tilts


# ---------------------------------------------------------------------------
# Missing-ball handling
# ---------------------------------------------------------------------------


def test_long_missing_gap_drifts_zoom_wide():
    cfg = RenderStepConfig(render_vertical_tracking=True)
    geom = _geom(cfg)
    state = _CameraState()
    # Seed with a tight tracked ball at midfield.
    _tick(
        state,
        (SRC_W / 2, SRC_H / 2, 0.0, 0.0),
        SRC_W,
        SRC_H,
        geom,
        BROADCAST_MODE,
        cfg,
        -90.0,
        90.0,
        None,
    )
    tracked_zoom = state.smoothed_zoom
    # Now lose the ball for a long time.
    for _ in range(200):
        _tick(state, None, SRC_W, SRC_H, geom, BROADCAST_MODE, cfg, -90.0, 90.0, None)
    # Drifts toward the wide missing-ball default.
    assert state.smoothed_zoom > tracked_zoom
    assert abs(state.smoothed_zoom - BROADCAST_MODE.missing_ball_long_zoom) < 0.02


def test_long_missing_gap_holds_pan_bearing():
    """On a long loss the camera HOLDS the last bearing (where the ball was),
    it must NOT recentre to mid-field and pan onto empty grass."""
    cfg = RenderStepConfig(render_vertical_tracking=True)
    geom = _geom(cfg)
    state = _CameraState()
    # Seed a ball well off-centre (right side) → large positive yaw.
    px = SRC_W * 0.85
    _tick(
        state,
        (px, SRC_H / 2, 0.0, 0.0),
        SRC_W,
        SRC_H,
        geom,
        BROADCAST_MODE,
        cfg,
        -90.0,
        90.0,
        None,
    )
    held_yaw = state.smoothed_yaw
    assert held_yaw > 40.0  # actually pointing at the right side
    # Lose the ball for a long time.
    for _ in range(300):
        _tick(state, None, SRC_W, SRC_H, geom, BROADCAST_MODE, cfg, -90.0, 90.0, None)
    # Pan stays on the last bearing (does not collapse toward 0 / mid-field).
    assert abs(state.smoothed_yaw - held_yaw) < 1.0
