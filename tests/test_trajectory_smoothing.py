"""Tests for the smooth_with_memory algorithm."""

from __future__ import annotations

import pytest

from video_grouper.inference.trajectory_smoothing import smooth_with_memory


class TestEdgeCases:
    def test_empty_input(self):
        assert smooth_with_memory([]) == []

    def test_all_none(self):
        out = smooth_with_memory([None, None, None])
        assert out == [None, None, None]

    def test_single_detection(self):
        # Single detection at frame 2 — frames 0, 1 stay None;
        # frames 2..end carry the smoothed value (= the detection itself)
        raw = [None, None, {"x": 100.0, "y": 50.0}, None, None]
        out = smooth_with_memory(raw, buffer_frames=10, decay_per_frame=0.95)
        assert out[0] is None
        assert out[1] is None
        for f in range(2, 5):
            assert out[f] is not None
            assert out[f]["x"] == pytest.approx(100.0)
            assert out[f]["y"] == pytest.approx(50.0)


class TestStableTracking:
    def test_constant_position_smooths_to_constant(self):
        raw = [{"x": 100.0, "y": 50.0} for _ in range(20)]
        out = smooth_with_memory(raw, buffer_frames=10, decay_per_frame=0.9)
        for f in range(20):
            assert out[f]["x"] == pytest.approx(100.0)
            assert out[f]["y"] == pytest.approx(50.0)

    def test_velocity_zero_for_constant(self):
        raw = [{"x": 100.0, "y": 50.0} for _ in range(10)]
        out = smooth_with_memory(raw)
        for f in range(1, 10):
            assert out[f]["vx"] == pytest.approx(0.0)
            assert out[f]["vy"] == pytest.approx(0.0)


class TestGapFilling:
    def test_gap_in_middle_is_filled(self):
        # 5 detections, then a 5-frame gap, then 5 more detections at same place
        raw = (
            [{"x": 100.0, "y": 50.0} for _ in range(5)]
            + [None] * 5
            + [{"x": 100.0, "y": 50.0} for _ in range(5)]
        )
        out = smooth_with_memory(raw, buffer_frames=20, decay_per_frame=0.95)
        # All frames after the first detection should be populated
        assert all(o is not None for o in out)
        # Gap-filled values should still be near 100 since position didn't change
        for f in range(5, 10):
            assert out[f]["x"] == pytest.approx(100.0, abs=0.5)

    def test_gap_at_end_is_filled_from_buffer(self):
        # Detections through frame 9, then gone for the rest
        raw = [{"x": 100.0 + i, "y": 50.0} for i in range(10)] + [None] * 30
        out = smooth_with_memory(raw, buffer_frames=20, decay_per_frame=0.95)
        # Through frame 9 we have real detections
        assert all(o is not None for o in out[:10])
        # The buffer keeps entries with frame_idx >= f - buffer_frames + 1.
        # Last detection is at frame 9, so it stays in the buffer through
        # frame 9 + buffer_frames - 1 = 28. After that, buffer empties.
        for f in range(10, 29):  # within buffer reach
            assert out[f] is not None, f"frame {f} should be populated"
        # Frame 29: f - buffer_frames + 1 = 10, so detection at frame 9
        # is dropped — buffer empties, output is None.
        assert out[29] is None


class TestSmoothingBehavior:
    def test_smoothed_position_lags_a_step_change(self):
        # Position jumps from 0 to 100 at frame 5 and stays
        raw = [{"x": 0.0, "y": 0.0} for _ in range(5)] + [
            {"x": 100.0, "y": 0.0} for _ in range(20)
        ]
        out = smooth_with_memory(raw, buffer_frames=10, decay_per_frame=0.85)
        # Frame 5 (just after step) should be closer to 0 than to 100
        # because the buffer still holds 5 frames at 0 and 1 frame at 100
        assert 10 < out[5]["x"] < 50
        # Many frames later, smoothing converges to 100
        assert out[24]["x"] == pytest.approx(100.0, abs=2.0)

    def test_buffer_size_controls_history_window(self):
        # Same inputs, different buffer sizes — bigger buffer = slower convergence
        raw = [{"x": 0.0, "y": 0.0} for _ in range(5)] + [
            {"x": 100.0, "y": 0.0} for _ in range(15)
        ]
        small = smooth_with_memory(raw, buffer_frames=5, decay_per_frame=0.9)
        big = smooth_with_memory(raw, buffer_frames=15, decay_per_frame=0.9)
        # At frame 10 (5 frames after step), small buffer should be closer to 100
        # than big buffer (big buffer still remembers the 0 phase more)
        assert small[10]["x"] > big[10]["x"]

    def test_velocity_matches_finite_difference(self):
        # Linear motion: x = 10 * t
        raw = [{"x": 10.0 * i, "y": 0.0} for i in range(20)]
        out = smooth_with_memory(raw, buffer_frames=5, decay_per_frame=0.9)
        # In steady state, smoothed x should track input (with some lag)
        # Finite-diff vx should be near 10 px/frame
        for f in range(15, 20):
            assert out[f]["vx"] == pytest.approx(10.0, abs=0.5)


class TestDecayCharacteristic:
    def test_high_decay_responds_faster(self):
        # decay=0.99 = slow decay (long memory)
        # decay=0.5 = fast decay (short memory)
        raw = [{"x": 0.0, "y": 0.0} for _ in range(5)] + [
            {"x": 100.0, "y": 0.0} for _ in range(10)
        ]
        slow = smooth_with_memory(raw, buffer_frames=15, decay_per_frame=0.99)
        fast = smooth_with_memory(raw, buffer_frames=15, decay_per_frame=0.5)
        # Fast-decay should be much closer to 100 sooner
        assert fast[7]["x"] > slow[7]["x"]

    def test_decay_one_means_uniform_average(self):
        # decay=1.0 means all in-buffer frames have weight 1 (simple mean)
        raw = [{"x": 0.0, "y": 0.0}, {"x": 100.0, "y": 0.0}]
        out = smooth_with_memory(raw, buffer_frames=10, decay_per_frame=1.0)
        # Frame 1 sees both detections with equal weight → mean = 50
        assert out[1]["x"] == pytest.approx(50.0)


def test_returned_list_has_same_length():
    raw = [None, {"x": 1.0, "y": 2.0}, None, None, {"x": 3.0, "y": 4.0}]
    out = smooth_with_memory(raw)
    assert len(out) == len(raw)


def test_first_few_nones_preserved_until_first_detection():
    raw = [None, None, None, {"x": 100.0, "y": 50.0}]
    out = smooth_with_memory(raw)
    assert out[0] is None
    assert out[1] is None
    assert out[2] is None
    assert out[3] is not None
