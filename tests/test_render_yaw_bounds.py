"""Tests for the render stage's yaw-bounding helpers.

The render-stage internals — `_yaw_bounds`, `_clamp`, `_load_polygon` —
isolate the field-polygon → yaw-clamp logic from the heavy PyAV/OpenCV
remap loop so we can unit-test them without touching video.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from video_grouper.ball_tracking.providers.homegrown.stages.render import (
    _clamp,
    _load_polygon,
    _yaw_bounds,
)


SRC_W = 4096
SRC_HFOV = 180.0


class TestYawBoundsFromPolygon:
    def test_full_polygon_returns_full_field_with_padding(self):
        # Full lateral extent across source: kpt 0 at x=0, kpt 4 at x=src_w-1.
        poly = np.array(
            [[0, 1500], [4095, 1500], [4095, 300], [0, 300]], dtype=np.float32
        )
        ymin, ymax = _yaw_bounds(poly, SRC_W, SRC_HFOV, padding_deg=5.0)
        # Without padding: ±90°. With +5° padding: clamped to ±90° (source max).
        assert ymin == pytest.approx(-90.0)
        assert ymax == pytest.approx(90.0)

    def test_narrow_central_polygon(self):
        # Middle 25% of source — yaw range ~[-22.5°, +22.5°] before padding.
        poly = np.array(
            [[1536, 1500], [2560, 1500], [2560, 300], [1536, 300]], dtype=np.float32
        )
        ymin, ymax = _yaw_bounds(poly, SRC_W, SRC_HFOV, padding_deg=5.0)
        assert ymin == pytest.approx(-27.5, abs=0.5)
        assert ymax == pytest.approx(27.5, abs=0.5)

    def test_no_polygon_returns_full_source_range(self):
        ymin, ymax = _yaw_bounds(None, SRC_W, SRC_HFOV, padding_deg=5.0)
        assert ymin == -90.0
        assert ymax == 90.0

    def test_padding_does_not_exceed_source_max(self):
        poly = np.array(
            [[100, 1500], [3995, 1500], [3995, 300], [100, 300]], dtype=np.float32
        )
        ymin, ymax = _yaw_bounds(poly, SRC_W, SRC_HFOV, padding_deg=20.0)
        # Polygon yaw range about ±88° → padded would be ±108° but clamped to ±90°.
        assert ymin >= -90.0
        assert ymax <= 90.0


class TestClamp:
    def test_inside_range_passes_through(self):
        assert _clamp(5.0, -10.0, 10.0) == 5.0

    def test_below_range_clamps_to_low(self):
        assert _clamp(-50.0, -10.0, 10.0) == -10.0

    def test_above_range_clamps_to_high(self):
        assert _clamp(50.0, -10.0, 10.0) == 10.0


class TestLoadPolygon:
    def test_no_path_returns_none(self):
        assert _load_polygon(None) is None
        assert _load_polygon("") is None

    def test_missing_file_returns_none(self, tmp_path: Path):
        assert _load_polygon(str(tmp_path / "nope.json")) is None

    def test_null_polygon_in_payload_returns_none(self, tmp_path: Path):
        payload = {"polygon": None, "homography": None}
        path = tmp_path / "field_polygon.json"
        path.write_text(json.dumps(payload))
        assert _load_polygon(str(path)) is None

    def test_polygon_round_trip(self, tmp_path: Path):
        polygon = [[0.0, 1500.0], [4095.0, 1500.0], [2048.0, 300.0]]
        payload = {"polygon": polygon, "homography": None}
        path = tmp_path / "field_polygon.json"
        path.write_text(json.dumps(payload))
        loaded = _load_polygon(str(path))
        assert loaded is not None
        assert loaded.shape == (3, 2)
        np.testing.assert_array_almost_equal(
            loaded, np.array(polygon, dtype=np.float32)
        )
