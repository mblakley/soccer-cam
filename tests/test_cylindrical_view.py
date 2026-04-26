"""Geometry tests for the cylindrical view projection."""

from __future__ import annotations

import numpy as np
import pytest

from video_grouper.inference.cylindrical_view import (
    CylindricalViewParams,
    cylindrical_remap,
    pixel_to_yaw_pitch,
    yaw_pitch_to_pixel,
    yaw_pixel_offset,
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
        offset = yaw_pixel_offset(params, 45.0)
        assert offset == pytest.approx(45.0 / SRC_HFOV * SRC_W)
        assert (map_x_45 - map_x_0).mean() == pytest.approx(offset, abs=1e-3)

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
        # Positive pitch sends the view downward, so we sample lower in the source.
        assert map_y_p.mean() > map_y_0.mean()


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
