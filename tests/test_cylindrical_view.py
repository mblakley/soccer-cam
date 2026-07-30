"""Geometry tests for the cylindrical view projection."""

from __future__ import annotations

import numpy as np
import pytest

from video_grouper.inference.cylindrical_view import (
    CylindricalViewParams,
    cylindrical_remap,
    pixel_depression_deg,
    pixel_to_yaw_pitch,
    polygon_leveling_rotation,
    yaw_pitch_to_pixel,
)

SRC_W = 4096
SRC_H = 1800
SRC_HFOV = 180.0


def _params(
    view_yaw_unused=0.0, view_hfov=60.0, view_pitch=0.0, out_w=1920, out_h=1080
):
    return CylindricalViewParams(
        src_w=SRC_W,
        src_h=SRC_H,
        src_hfov_deg=SRC_HFOV,
        out_w=out_w,
        out_h=out_h,
        view_hfov_deg=view_hfov,
        view_pitch_deg=view_pitch,
    )


class TestPixelYawRoundTrip:
    @pytest.mark.parametrize(
        "px,py", [(0, 0), (SRC_W // 2, SRC_H // 2), (SRC_W - 1, SRC_H - 1), (1024, 600)]
    )
    def test_pixel_to_yaw_to_pixel_identity(self, px, py):
        yaw, pitch = pixel_to_yaw_pitch(px, py, SRC_W, SRC_H, SRC_HFOV)
        rx, ry = yaw_pitch_to_pixel(yaw, pitch, SRC_W, SRC_H, SRC_HFOV)
        assert rx == pytest.approx(px, abs=1e-6)
        assert ry == pytest.approx(py, abs=1e-6)

    def test_center_pixel_is_zero_angle(self):
        yaw, pitch = pixel_to_yaw_pitch(SRC_W / 2, SRC_H / 2, SRC_W, SRC_H, SRC_HFOV)
        assert yaw == pytest.approx(0.0)
        assert pitch == pytest.approx(0.0)

    def test_left_edge_is_minus_half_hfov(self):
        yaw, _ = pixel_to_yaw_pitch(0, SRC_H / 2, SRC_W, SRC_H, SRC_HFOV)
        assert yaw == pytest.approx(-SRC_HFOV / 2)

    def test_right_edge_is_plus_half_hfov(self):
        yaw, _ = pixel_to_yaw_pitch(SRC_W, SRC_H / 2, SRC_W, SRC_H, SRC_HFOV)
        assert yaw == pytest.approx(SRC_HFOV / 2)


class TestRemapBaseGrid:
    def test_zero_yaw_center_pixel_samples_source_center(self):
        params = _params()
        map_x, map_y = cylindrical_remap(params, view_yaw_deg=0.0)
        cx, cy = params.out_w // 2, params.out_h // 2
        assert map_x[cy, cx] == pytest.approx(SRC_W / 2.0, abs=0.5)
        assert map_y[cy, cx] == pytest.approx(SRC_H / 2.0, abs=0.5)

    def test_view_yaw_shifts_source_x_linearly(self):
        params = _params()
        map_x_0, _ = cylindrical_remap(params, view_yaw_deg=0.0)
        map_x_45, _ = cylindrical_remap(params, view_yaw_deg=45.0)
        # With no mount tilt, a yaw rotation is an exact azimuth shift (Ry preserves
        # the x-z magnitude), so map_x translates by 45° worth of source pixels.
        offset = 45.0 / SRC_HFOV * SRC_W
        assert (map_x_45 - map_x_0).mean() == pytest.approx(offset, abs=1e-2)

    def test_smaller_view_hfov_means_smaller_source_x_range(self):
        wide = _params(view_hfov=80.0)
        tight = _params(view_hfov=30.0)
        map_x_wide, _ = cylindrical_remap(wide, view_yaw_deg=0.0)
        map_x_tight, _ = cylindrical_remap(tight, view_yaw_deg=0.0)
        wide_range = map_x_wide.max() - map_x_wide.min()
        tight_range = map_x_tight.max() - map_x_tight.min()
        assert tight_range < wide_range

    def test_yaw_offset_does_not_change_map_y(self):
        params = _params()
        _, map_y_0 = cylindrical_remap(params, view_yaw_deg=0.0)
        _, map_y_45 = cylindrical_remap(params, view_yaw_deg=45.0)
        np.testing.assert_array_equal(map_y_0, map_y_45)

    def test_view_pitch_shifts_map_y(self):
        no_pitch = _params(view_pitch=0.0)
        pitched = _params(view_pitch=10.0)
        _, map_y_0 = cylindrical_remap(no_pitch, view_yaw_deg=0.0)
        _, map_y_p = cylindrical_remap(pitched, view_yaw_deg=0.0)
        # The full-rotation projection orients the look-at via a real Rx(pitch_w): +pitch
        # lifts the optical axis, so the view samples HIGHER in the source (smaller map_y).
        # Production vertical framing is driven by the numerically-solved
        # view_pitch_offset_deg (see render._solve_framing), not this raw sign.
        assert map_y_p.mean() < map_y_0.mean()


class TestSquarePixelDefaults:
    def test_view_vfov_auto_keeps_square_pixels(self):
        params = CylindricalViewParams(
            src_w=SRC_W,
            src_h=SRC_H,
            src_hfov_deg=SRC_HFOV,
            out_w=1920,
            out_h=1080,
            view_hfov_deg=60.0,
        )
        # Auto vfov should be 60 * 1080 / 1920 = 33.75°
        from video_grouper.inference.cylindrical_view import _resolved_view_vfov

        assert _resolved_view_vfov(params) == pytest.approx(60.0 * 1080 / 1920)

    def test_src_vfov_auto_keeps_square_pixels(self):
        params = CylindricalViewParams(
            src_w=SRC_W,
            src_h=SRC_H,
            src_hfov_deg=SRC_HFOV,
            out_w=1920,
            out_h=1080,
            view_hfov_deg=60.0,
        )
        from video_grouper.inference.cylindrical_view import _resolved_src_vfov

        # 180 * 1800 / 4096 ≈ 79.1°
        assert _resolved_src_vfov(params) == pytest.approx(180.0 * 1800 / 4096)


class TestPixelDepression:
    """Depression angle below the polygon-leveled horizon — the lens-correct
    farness axis for the depression-conditioned far-hold (EXP-OP-32/33)."""

    # A plausible 10-point field polygon: near touchline low+wide (0-4,
    # left->right), far touchline high+narrow (5-9, right->left).
    POLY = [
        [200.0, 1500.0],
        [1100.0, 1520.0],
        [2048.0, 1530.0],
        [3000.0, 1520.0],
        [3900.0, 1500.0],
        [3400.0, 700.0],
        [2720.0, 690.0],
        [2048.0, 685.0],
        [1370.0, 690.0],
        [700.0, 700.0],
    ]

    def test_leveling_rotation_is_orthonormal(self):
        r = polygon_leveling_rotation(self.POLY, SRC_W, SRC_H, SRC_HFOV)
        assert r is not None
        assert np.allclose(r.T @ r, np.eye(3), atol=1e-9)

    def test_degenerate_polygon_returns_none(self):
        assert (
            polygon_leveling_rotation([[0, 0], [1, 1]], SRC_W, SRC_H, SRC_HFOV) is None
        )

    def test_depression_increases_toward_image_bottom(self):
        """Nearer ground (lower in image) = steeper ray = larger depression."""
        r = polygon_leveling_rotation(self.POLY, SRC_W, SRC_H, SRC_HFOV)
        cx = SRC_W / 2
        d_far = pixel_depression_deg(cx, 700.0, r, SRC_W, SRC_H, SRC_HFOV)
        d_mid = pixel_depression_deg(cx, 1100.0, r, SRC_W, SRC_H, SRC_HFOV)
        d_near = pixel_depression_deg(cx, 1500.0, r, SRC_W, SRC_H, SRC_HFOV)
        assert d_far < d_mid < d_near
        assert d_near > 0  # near touchline is below the leveled horizon

    def test_depression_is_bounded(self):
        """Unlike ground range, depression stays finite at/above the horizon."""
        r = polygon_leveling_rotation(self.POLY, SRC_W, SRC_H, SRC_HFOV)
        for py in (0.0, 300.0, SRC_H - 1.0):
            d = pixel_depression_deg(2048.0, py, r, SRC_W, SRC_H, SRC_HFOV)
            assert -90.0 <= d <= 90.0


class TestRayFieldGeometry:
    """Ray-ground world model (EXP-OP-34): correct meters from the polygon-
    leveled orientation; height anchored so the near touchline = field length."""

    def _geom(self):
        from video_grouper.inference.world_geometry import build_ray_field_geometry

        return build_ray_field_geometry(
            TestPixelDepression.POLY, SRC_W, SRC_H, SRC_HFOV
        )

    def test_builds_from_valid_polygon(self):
        g = self._geom()
        assert g is not None and g.valid
        assert g.cam_height_m > 0

    def test_near_touchline_is_scale_anchored(self):
        g = self._geom()
        poly = np.asarray(TestPixelDepression.POLY, float)
        ends = g.image_to_world(poly[[0, 4]])
        assert np.linalg.norm(ends[1] - ends[0]) == pytest.approx(
            g.field_length_m, rel=1e-6
        )

    def test_image_world_round_trip_on_field(self):
        g = self._geom()
        pts = np.array([[2048.0, 1400.0], [1000.0, 1000.0], [3000.0, 800.0]])
        back = g.world_to_image(g.image_to_world(pts))
        assert np.allclose(back, pts, atol=1e-6)

    def test_expected_size_shrinks_with_distance(self):
        g = self._geom()
        sizes = g.expected_ball_diameter_px(
            np.array([[2048.0, 1500.0], [2048.0, 1100.0], [2048.0, 700.0]])
        )
        assert sizes[0] > sizes[1] > sizes[2] > 0

    def test_horizon_ray_stays_finite(self):
        g = self._geom()
        w = g.image_to_world(np.array([[2048.0, 0.0]]))
        assert np.all(np.isfinite(w))

    def test_flipped_polygon_returns_none(self):
        from video_grouper.inference.world_geometry import build_ray_field_geometry

        poly = np.asarray(TestPixelDepression.POLY, float)
        flipped = np.concatenate([poly[5:], poly[:5]])  # near/far swapped
        assert build_ray_field_geometry(flipped, SRC_W, SRC_H, SRC_HFOV) is None

    def test_rerank_accepts_ray_geometry(self):
        """The tracker runs end-to-end on the ray world model (duck interface)."""
        from video_grouper.inference.ball_tracker import Candidate, rerank

        g = self._geom()
        frames = [
            [Candidate(x=1800.0 + 12.0 * i, y=1200.0, score=0.8, size_px=8.0)]
            for i in range(12)
        ]
        sel = rerank(frames, g, frame_gaps=[1] * 12)
        assert sum(1 for v in sel.values() if v is not None) > 0

    def test_is_in_support_matches_planar(self):
        """EXP-OP-35: support is polygon-only, so the ray geometry answers it
        identically to the planar one (teacher_track calls it on either)."""
        from video_grouper.inference.world_geometry import build_field_geometry

        g = self._geom()
        planar = build_field_geometry(np.asarray(TestPixelDepression.POLY, float))
        pts = np.array(
            [
                [2048.0, 1100.0],  # mid-field: inside
                [10.0, 10.0],  # image corner: far off-field
                [2048.0, 650.0],  # just above the far line: dome zone
            ]
        )
        for dome in (0.0, 60.0):
            np.testing.assert_array_equal(
                g.is_in_support(pts, margin_px=10.0, dome_px=dome),
                planar.is_in_support(pts, margin_px=10.0, dome_px=dome),
            )
        assert g.is_in_support(pts[:1], margin_px=10.0)[0]
        assert not g.is_in_support(pts[1:2], margin_px=10.0)[0]

    def test_build_features_accepts_ray_geometry(self):
        """EXP-OP-35: the selector feature builder runs on the ray world model
        (metric-consistent features for the v8 retrain)."""
        from video_grouper.inference.ball_selector import (
            FEATURE_NAMES,
            build_features,
        )
        from video_grouper.inference.ball_tracker import Candidate

        g = self._geom()
        frames = [
            [
                Candidate(x=1800.0 + 12.0 * i, y=1200.0, score=0.8, size_px=8.0),
                Candidate(x=2500.0, y=800.0, score=0.4, size_px=6.0),
            ]
            for i in range(6)
        ]
        feats = build_features(frames, g, ef=[i * 8 for i in range(6)])
        assert len(feats) == 6
        for x in feats:
            assert x.shape == (2, len(FEATURE_NAMES))
            assert np.all(np.isfinite(x))
        # geometry features are live (not the neutral fallback): the far
        # candidate must read as smaller expected diameter than the near one
        depth_i = FEATURE_NAMES.index("depth")
        assert feats[0][1, depth_i] < feats[0][0, depth_i]
