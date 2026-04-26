"""Tests for the per-frame camera control logic in the render stage.

Covers zone classification, dead-ball detection, lead-room offset,
asymmetric pan smoothing, and broadcast vs coach mode parameter
overrides. The PyAV/OpenCV remap loop is tested only indirectly via
its inputs — these tests exercise the pure helpers.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from video_grouper.ball_tracking.providers.homegrown.stages.render import (
    BROADCAST_MODE,
    COACH_MODE,
    _CameraState,
    _ball_field_x,
    _classify_zone,
    _deadball_zone_zoom,
    _normalized_speed,
    _resolve_mode,
    _tick,
    _trajectory_entry,
    _zone_base_zoom,
)


SRC_W = 4096
SRC_H = 1800
SRC_HFOV = 180.0


class TestModeResolve:
    def test_broadcast_default(self):
        assert _resolve_mode("broadcast") is BROADCAST_MODE

    def test_coach(self):
        assert _resolve_mode("coach") is COACH_MODE

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="render_mode"):
            _resolve_mode("studio")

    def test_coach_is_wider_than_broadcast(self):
        # Coach mode should be wider at every zone (less zoomed-in).
        assert COACH_MODE.zoom_box > BROADCAST_MODE.zoom_box
        assert COACH_MODE.zoom_third > BROADCAST_MODE.zoom_third
        assert COACH_MODE.zoom_midfield > BROADCAST_MODE.zoom_midfield

    def test_coach_has_smaller_lead_room(self):
        assert COACH_MODE.max_lead_room_fraction < BROADCAST_MODE.max_lead_room_fraction


class TestClassifyZone:
    def test_left_box(self):
        assert _classify_zone(0.05, BROADCAST_MODE) == "left_box"

    def test_left_third(self):
        assert _classify_zone(0.20, BROADCAST_MODE) == "left_third"

    def test_midfield(self):
        assert _classify_zone(0.50, BROADCAST_MODE) == "midfield"

    def test_right_third(self):
        assert _classify_zone(0.80, BROADCAST_MODE) == "right_third"

    def test_right_box(self):
        assert _classify_zone(0.95, BROADCAST_MODE) == "right_box"


class TestZoneZoom:
    def test_box_uses_box_zoom(self):
        assert _zone_base_zoom("left_box", BROADCAST_MODE) == BROADCAST_MODE.zoom_box
        assert _zone_base_zoom("right_box", BROADCAST_MODE) == BROADCAST_MODE.zoom_box

    def test_midfield_uses_midfield_zoom(self):
        assert (
            _zone_base_zoom("midfield", BROADCAST_MODE) == BROADCAST_MODE.zoom_midfield
        )


class TestDeadballZoom:
    def test_box_zone_uses_deadball_box_zoom(self):
        assert (
            _deadball_zone_zoom("left_box", COACH_MODE) == COACH_MODE.deadball_box_zoom
        )

    def test_midfield_uses_deadball_midfield_zoom(self):
        assert (
            _deadball_zone_zoom("midfield", COACH_MODE)
            == COACH_MODE.deadball_midfield_zoom
        )

    def test_coach_deadball_is_wider_than_broadcast(self):
        # The whole point of coach mode at dead balls is to show more context.
        assert _deadball_zone_zoom("left_box", COACH_MODE) > _deadball_zone_zoom(
            "left_box", BROADCAST_MODE
        )


class TestBallFieldX:
    def test_with_homography(self):
        # Identity homography on a 1×1 field → field_x = px.
        h = np.eye(3, dtype=np.float32)
        assert _ball_field_x(0.42, 0.5, src_w=4096, homography=h) == pytest.approx(0.42)

    def test_no_homography_falls_back_to_pixel_ratio(self):
        assert _ball_field_x(
            2048.0, 900.0, src_w=4096, homography=None
        ) == pytest.approx(0.5)


class TestNormalizedSpeed:
    def test_zero(self):
        assert _normalized_speed(0.0, 0.0, max_expected=100.0) == 0.0

    def test_clamps_to_one(self):
        assert _normalized_speed(500.0, 500.0, max_expected=100.0) == 1.0

    def test_diagonal_is_pythagorean(self):
        assert _normalized_speed(3.0, 4.0, max_expected=10.0) == pytest.approx(0.5)


class TestTrajectoryEntry:
    def test_none_passthrough(self):
        assert _trajectory_entry(None) is None

    def test_dict_with_velocity(self):
        e = _trajectory_entry({"x": 100.0, "y": 200.0, "vx": 1.5, "vy": -0.5})
        assert e == (100.0, 200.0, 1.5, -0.5)

    def test_dict_missing_velocity_defaults_to_zero(self):
        e = _trajectory_entry({"x": 100.0, "y": 200.0})
        assert e == (100.0, 200.0, 0.0, 0.0)

    def test_legacy_list(self):
        e = _trajectory_entry([100.0, 200.0])
        assert e == (100.0, 200.0, 0.0, 0.0)


class TestTick:
    """Integration of the per-frame state machine."""

    def _params(self):
        return dict(
            src_w=SRC_W,
            src_h=SRC_H,
            src_hfov_deg=SRC_HFOV,
            homography=None,
            mode=BROADCAST_MODE,
            yaw_min=-90.0,
            yaw_max=90.0,
        )

    def test_first_call_with_none_returns_safe_defaults(self):
        state = _CameraState()
        yaw, hfov = _tick(state, None, **self._params())
        assert yaw == pytest.approx(0.0)
        assert hfov == pytest.approx(BROADCAST_MODE.zoom_midfield * SRC_HFOV)

    def test_first_call_with_centered_ball_initializes_smoothed_to_target(self):
        state = _CameraState()
        # Ball at source center, no velocity.
        entry = (SRC_W / 2.0, SRC_H / 2.0, 0.0, 0.0)
        yaw, hfov = _tick(state, entry, **self._params())
        # Center pixel → yaw 0; midfield zone → midfield zoom.
        assert yaw == pytest.approx(0.0, abs=0.01)
        assert hfov == pytest.approx(BROADCAST_MODE.zoom_midfield * SRC_HFOV, abs=0.5)

    def test_yaw_clamped_to_polygon_bounds(self):
        state = _CameraState()
        entry = (SRC_W * 0.95, SRC_H / 2.0, 200.0, 0.0)
        yaw, _hfov = _tick(state, entry, **{**self._params(), "yaw_max": 30.0})
        assert yaw <= 30.0

    def test_dead_ball_eventually_triggers_override(self):
        state = _CameraState()
        # 20 ticks of a stationary ball in the left-box zone.
        for _ in range(20):
            yaw, hfov = _tick(
                state,
                (SRC_W * 0.05, SRC_H / 2.0, 0.0, 0.0),
                **self._params(),
            )
        # After deadball_frame_count (15) frames, dead-ball override kicks in.
        # In broadcast mode the dead-ball box zoom equals the regular box zoom,
        # so we instead check the stationary counter.
        assert state.stationary_frames >= BROADCAST_MODE.deadball_frame_count

    def test_fast_ball_widens_zoom_via_speed_bias(self):
        state_slow = _CameraState()
        state_fast = _CameraState()
        # Same midfield position, different velocities.
        slow_entry = (SRC_W / 2.0, SRC_H / 2.0, 5.0, 0.0)
        fast_entry = (SRC_W / 2.0, SRC_H / 2.0, 200.0, 0.0)
        # Run multiple ticks so smoothed_zoom converges toward the target.
        for _ in range(40):
            _tick(state_slow, slow_entry, **self._params())
            _tick(state_fast, fast_entry, **self._params())
        assert state_fast.smoothed_zoom > state_slow.smoothed_zoom

    def test_lead_room_pulls_smoothed_yaw_ahead_of_ball(self):
        """A fast ball moving right should pull the smoothed yaw to the right
        of the ball's instantaneous yaw."""
        state = _CameraState()
        entry = (SRC_W / 2.0, SRC_H / 2.0, 200.0, 0.0)
        # Many ticks so smoothed_yaw catches up to the lead-shifted target.
        for _ in range(60):
            yaw, _ = _tick(state, entry, **self._params())
        # Ball at center → yaw 0; positive vx → lead pulls target yaw positive.
        assert state.smoothed_yaw > 0

    def test_pan_smoothing_alpha_grows_with_speed(self):
        """Indirectly: a faster ball should converge faster to its target yaw."""
        slow_state = _CameraState()
        fast_state = _CameraState()
        slow_state.smoothed_yaw = -10.0
        fast_state.smoothed_yaw = -10.0
        # Single tick toward the same target (centered ball).
        _tick(slow_state, (SRC_W / 2.0, SRC_H / 2.0, 1.0, 0.0), **self._params())
        _tick(fast_state, (SRC_W / 2.0, SRC_H / 2.0, 200.0, 0.0), **self._params())
        # Fast state moved further toward 0.
        assert abs(fast_state.smoothed_yaw) < abs(slow_state.smoothed_yaw)


class TestCoachModeIsWider:
    def test_render_mode_changes_zoom_visibly(self):
        """With identical inputs the modes converge to different zooms."""
        broadcast_state = _CameraState()
        coach_state = _CameraState()
        entry = (SRC_W / 2.0, SRC_H / 2.0, 5.0, 0.0)
        common = dict(
            src_w=SRC_W,
            src_h=SRC_H,
            src_hfov_deg=SRC_HFOV,
            homography=None,
            yaw_min=-90.0,
            yaw_max=90.0,
        )
        for _ in range(80):  # ample iterations to converge
            _tick(broadcast_state, entry, mode=BROADCAST_MODE, **common)
            _tick(coach_state, entry, mode=COACH_MODE, **common)
        assert coach_state.smoothed_zoom > broadcast_state.smoothed_zoom


class TestMissingBallGap:
    def _common(self):
        return dict(
            src_w=SRC_W,
            src_h=SRC_H,
            src_hfov_deg=SRC_HFOV,
            homography=None,
            mode=BROADCAST_MODE,
            yaw_min=-90.0,
            yaw_max=90.0,
        )

    def test_short_gap_holds_yaw_and_zoom(self):
        state = _CameraState()
        # Establish a known state with a few real ticks.
        for _ in range(10):
            _tick(state, (SRC_W * 0.7, SRC_H / 2.0, 5.0, 0.0), **self._common())
        before_yaw = state.smoothed_yaw
        before_zoom = state.smoothed_zoom
        # Short gap (under threshold): should hold.
        for _ in range(BROADCAST_MODE.missing_ball_short_frames - 1):
            _tick(state, None, **self._common())
        assert state.smoothed_yaw == pytest.approx(before_yaw)
        assert state.smoothed_zoom == pytest.approx(before_zoom)

    def test_medium_gap_drifts_zoom_toward_midfield(self):
        state = _CameraState()
        # Establish a tight zoom (right_box → broadcast box zoom = 0.25).
        for _ in range(20):
            _tick(state, (SRC_W * 0.95, SRC_H / 2.0, 0.0, 0.0), **self._common())
        before_zoom = state.smoothed_zoom
        # Run for many None ticks so the medium-gap drift kicks in.
        for _ in range(40):  # > short_frames=15
            _tick(state, None, **self._common())
        # Zoom should have widened toward midfield (0.45).
        assert state.smoothed_zoom > before_zoom
        assert state.smoothed_zoom < BROADCAST_MODE.zoom_midfield  # not all the way

    def test_long_gap_drifts_yaw_to_center_and_zoom_to_wide_default(self):
        state = _CameraState()
        # Far-right tight framing.
        for _ in range(20):
            _tick(state, (SRC_W * 0.95, SRC_H / 2.0, 0.0, 0.0), **self._common())
        # Long gap: many ticks past medium_frames=60.
        for _ in range(300):
            _tick(state, None, **self._common())
        # Yaw should have drifted toward 0 (centered field).
        assert abs(state.smoothed_yaw) < 5.0
        # Zoom should have approached missing_ball_long_zoom (0.55).
        assert state.smoothed_zoom == pytest.approx(
            BROADCAST_MODE.missing_ball_long_zoom, abs=0.05
        )

    def test_real_entry_resets_missing_counter(self):
        state = _CameraState()
        for _ in range(20):
            _tick(state, None, **self._common())
        assert state.missing_frames == 20
        _tick(state, (SRC_W / 2.0, SRC_H / 2.0, 0.0, 0.0), **self._common())
        assert state.missing_frames == 0


def test_smoothed_yaw_decays_to_zero_over_many_ticks_with_no_movement():
    """Sanity check: a stationary ball at source center → smoothed yaw stays near 0."""
    state = _CameraState()
    entry = (SRC_W / 2.0, SRC_H / 2.0, 0.0, 0.0)
    for _ in range(30):
        yaw, _ = _tick(
            state,
            entry,
            src_w=SRC_W,
            src_h=SRC_H,
            src_hfov_deg=SRC_HFOV,
            homography=None,
            mode=BROADCAST_MODE,
            yaw_min=-90.0,
            yaw_max=90.0,
        )
    assert math.isclose(yaw, 0.0, abs_tol=1e-3)
