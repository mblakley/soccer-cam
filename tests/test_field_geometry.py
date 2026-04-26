"""Geometry tests for field-keypoint helpers (polygon, homography, yaw extent)."""

from __future__ import annotations

import numpy as np
import pytest

from video_grouper.inference.field_geometry import (
    FIELD_KEYPOINT_FIELD_COORDS,
    build_field_polygon,
    field_homography,
    field_lateral_yaw_extent,
    is_on_field,
    pixel_to_field,
)


# A synthetic full set of keypoints arranged in a trapezoidal shape that
# matches the expected layout (near sideline at y=1500, far at y=300, with
# perspective narrowing). Confidence is irrelevant for these helpers.
_FULL_KPTS: list[tuple[float | None, float | None, float]] = [
    (0.0, 1500.0, 0.9),  # 0 near-left
    (1024.0, 1500.0, 0.9),  # 1
    (2048.0, 1500.0, 0.9),  # 2
    (3072.0, 1500.0, 0.9),  # 3
    (4095.0, 1500.0, 0.9),  # 4 near-right
    (3072.0, 300.0, 0.9),  # 5 far-right
    (2560.0, 300.0, 0.9),  # 6
    (2048.0, 300.0, 0.9),  # 7
    (1536.0, 300.0, 0.9),  # 8
    (1024.0, 300.0, 0.9),  # 9 far-left
]


class TestBuildFieldPolygon:
    def test_full_keypoints_produces_10_vertices(self):
        poly = build_field_polygon(_FULL_KPTS)
        assert poly is not None
        assert poly.shape == (10, 2)

    def test_too_few_near_returns_none(self):
        kpts = [(None, None, 0.0)] * 5 + _FULL_KPTS[5:]
        kpts[0] = (0.0, 1500.0, 0.9)  # only 1 near keypoint
        assert build_field_polygon(kpts) is None

    def test_too_few_far_returns_none(self):
        kpts = _FULL_KPTS[:5] + [(None, None, 0.0)] * 5
        kpts[5] = (3072.0, 300.0, 0.9)  # only 1 far keypoint
        assert build_field_polygon(kpts) is None


class TestFieldHomography:
    def test_full_keypoints_returns_3x3(self):
        h = field_homography(_FULL_KPTS)
        assert h is not None
        assert h.shape == (3, 3)

    def test_corners_round_trip_through_homography(self):
        h = field_homography(_FULL_KPTS)
        # kpt 0 → field (0, 0), kpt 4 → (1, 0), kpt 5 → (1, 1), kpt 9 → (0, 1)
        for idx, expected in [
            (0, (0.0, 0.0)),
            (4, (1.0, 0.0)),
            (5, (1.0, 1.0)),
            (9, (0.0, 1.0)),
        ]:
            px, py, _ = _FULL_KPTS[idx]
            fx, fy = pixel_to_field(px, py, h)
            assert fx == pytest.approx(expected[0], abs=0.01)
            assert fy == pytest.approx(expected[1], abs=0.01)

    def test_too_few_keypoints_returns_none(self):
        kpts: list[tuple[float | None, float | None, float]] = [(None, None, 0.0)] * 10
        kpts[0] = (0.0, 1500.0, 0.9)
        kpts[4] = (4095.0, 1500.0, 0.9)
        kpts[5] = (3072.0, 300.0, 0.9)  # only 3 — need ≥4
        assert field_homography(kpts) is None


class TestIsOnField:
    def test_inside_polygon_is_true(self):
        poly = build_field_polygon(_FULL_KPTS)
        assert is_on_field(2048.0, 800.0, poly) is True

    def test_outside_with_margin_is_false(self):
        poly = build_field_polygon(_FULL_KPTS)
        # Well above the far sideline (y=300), beyond the 50px margin.
        assert is_on_field(2048.0, 100.0, poly, margin=50.0) is False

    def test_outside_within_margin_is_true(self):
        poly = build_field_polygon(_FULL_KPTS)
        # Just above the far sideline, within margin.
        assert is_on_field(2048.0, 270.0, poly, margin=50.0) is True

    def test_no_polygon_passes_through(self):
        assert is_on_field(0.0, 0.0, None) is True


class TestFieldLateralYawExtent:
    SRC_W = 4096
    SRC_HFOV = 180.0

    def test_full_polygon_spans_full_field(self):
        poly = build_field_polygon(_FULL_KPTS)
        ymin, ymax = field_lateral_yaw_extent(poly, self.SRC_W, self.SRC_HFOV)
        # kpt 0 at x=0 → yaw = -90°, kpt 4 at x=4095 → yaw ≈ +90°
        assert ymin == pytest.approx(-90.0, abs=0.05)
        assert ymax == pytest.approx(90.0, abs=0.05)

    def test_no_polygon_returns_full_range(self):
        ymin, ymax = field_lateral_yaw_extent(None, self.SRC_W, self.SRC_HFOV)
        assert ymin == -90.0
        assert ymax == 90.0

    def test_central_polygon_returns_narrow_range(self):
        # Synthetic narrow polygon: only middle 25% of source width
        poly = np.array(
            [
                [1536, 1500],
                [2560, 1500],
                [2560, 300],
                [1536, 300],
            ],
            dtype=np.float32,
        )
        ymin, ymax = field_lateral_yaw_extent(poly, self.SRC_W, self.SRC_HFOV)
        assert ymin == pytest.approx(-22.5, abs=0.5)
        assert ymax == pytest.approx(22.5, abs=0.5)


class TestKeypointFieldCoords:
    def test_layout_matches_documented_diagram(self):
        # Near sideline: y=0; far sideline: y=1
        for i in range(5):
            assert FIELD_KEYPOINT_FIELD_COORDS[i, 1] == 0.0
        for i in range(5, 10):
            assert FIELD_KEYPOINT_FIELD_COORDS[i, 1] == 1.0
        # Near sideline: x increases left-to-right (0→4)
        for i in range(4):
            assert (
                FIELD_KEYPOINT_FIELD_COORDS[i, 0]
                < FIELD_KEYPOINT_FIELD_COORDS[i + 1, 0]
            )
        # Far sideline: x decreases right-to-left (5→9), per the layout
        # (kpt 5 = far-right, kpt 9 = far-left)
        for i in range(5, 9):
            assert (
                FIELD_KEYPOINT_FIELD_COORDS[i, 0]
                > FIELD_KEYPOINT_FIELD_COORDS[i + 1, 0]
            )
