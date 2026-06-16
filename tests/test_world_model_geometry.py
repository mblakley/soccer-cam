"""Tests for the zero-touch field geometry of the ball world-model."""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from training.world_model.geometry import (
    DEFAULT_FALLBACK_BALL_PX,
    _touchline_world_points,
    build_field_geometry,
)

# A synthetic ground-truth camera: a world field rectangle projected to an
# image trapezoid (near touchline wide at the bottom, far touchline narrow at
# the top) — the shape a high center-mount sees.
L, W = 95.0, 60.0
_WORLD_CORNERS = np.array(
    [[0, 0], [L, 0], [L, W], [0, W]], dtype=np.float32
)  # near-left, near-right, far-right, far-left
_IMAGE_CORNERS = np.array(
    [[300, 1500], [3800, 1500], [2600, 600], [1500, 600]], dtype=np.float32
)
_H_WORLD2IMG = cv2.getPerspectiveTransform(_WORLD_CORNERS, _IMAGE_CORNERS)


def _world_to_img(pts: np.ndarray) -> np.ndarray:
    p = np.asarray(pts, dtype=np.float64).reshape(-1, 1, 2)
    return cv2.perspectiveTransform(p, _H_WORLD2IMG).reshape(-1, 2)


def _make_polygon() -> np.ndarray:
    """Project the 10 equally-spaced touchline world points to image."""
    return _world_to_img(_touchline_world_points(L, W))


def test_geometry_is_valid_from_clean_polygon():
    geom = build_field_geometry(_make_polygon(), field_length_m=L, field_width_m=W)
    assert geom.valid
    assert geom.h_img2world is not None
    assert geom.h_world2img is not None


def test_image_to_world_roundtrips_touchline_points():
    poly = _make_polygon()
    geom = build_field_geometry(poly, field_length_m=L, field_width_m=W)
    world = geom.image_to_world(poly)
    expected = _touchline_world_points(L, W)
    # Recovered world coords match the equally-spaced touchline points (cm-level).
    assert np.allclose(world, expected, atol=0.1)


def test_world_image_roundtrip():
    geom = build_field_geometry(_make_polygon(), field_length_m=L, field_width_m=W)
    img_pts = np.array([[1200.0, 1100.0], [2500.0, 800.0], [2000.0, 1300.0]])
    back = geom.world_to_image(geom.image_to_world(img_pts))
    assert np.allclose(img_pts, back, atol=1e-3)


def test_expected_ball_size_larger_near_than_far():
    geom = build_field_geometry(_make_polygon(), field_length_m=L, field_width_m=W)
    near_img = _world_to_img([[L / 2, 5.0]])  # close to camera (near touchline)
    far_img = _world_to_img([[L / 2, W - 5.0]])  # far touchline
    near_px = geom.expected_ball_diameter_px(near_img)[0]
    far_px = geom.expected_ball_diameter_px(far_img)[0]
    assert near_px > far_px > 0
    # Perspective gradient should be a meaningful factor, not ~flat.
    assert near_px / far_px > 1.5


def test_support_inside_outside():
    geom = build_field_geometry(_make_polygon(), field_length_m=L, field_width_m=W)
    inside = _world_to_img([[L / 2, W / 2]])  # midfield
    assert geom.is_in_support(inside, margin_px=10.0)[0]
    outside = np.array([[5.0, 5.0]])  # image top-left, well off the field
    assert not geom.is_in_support(outside, margin_px=10.0)[0]


def test_size_consistency_rejects_player_sized_blob_in_far_field():
    geom = build_field_geometry(_make_polygon(), field_length_m=L, field_width_m=W)
    far_img = _world_to_img([[L / 2, W - 3.0]])
    expected_px = geom.expected_ball_diameter_px(far_img)[0]
    lp_ball = geom.size_consistency_logprob(far_img, np.array([expected_px]))[0]
    lp_player = geom.size_consistency_logprob(far_img, np.array([50.0]))[0]
    assert lp_ball > lp_player
    # A correctly-sized blob is near the Gaussian peak.
    assert lp_ball > -0.1


def test_neutral_geometry_on_missing_polygon():
    geom = build_field_geometry(None)
    assert not geom.valid
    # Uniform fallback size everywhere.
    sizes = geom.expected_ball_diameter_px(np.array([[100.0, 100.0], [2000.0, 900.0]]))
    assert np.allclose(sizes, DEFAULT_FALLBACK_BALL_PX)
    # Support accepts everywhere; size prior adds no penalty.
    assert geom.is_in_support(np.array([[0.0, 0.0]])).all()
    assert (
        geom.size_consistency_logprob(np.array([[0.0, 0.0]]), np.array([99.0]))[0]
        == 0.0
    )


def test_neutral_geometry_on_degenerate_polygon():
    tiny = np.array(
        [
            [0, 0],
            [1, 0],
            [2, 0],
            [3, 0],
            [4, 0],
            [4, 1],
            [3, 1],
            [2, 1],
            [1, 1],
            [0, 1],
        ],
        dtype=np.float64,
    )
    geom = build_field_geometry(tiny)
    assert not geom.valid


def test_image_to_world_raises_on_neutral():
    geom = build_field_geometry(None)
    with pytest.raises(ValueError):
        geom.image_to_world(np.array([[1.0, 2.0]]))
