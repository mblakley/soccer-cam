"""Tests for far-ball cut-paste augmentation (R7)."""

from __future__ import annotations

import numpy as np

from training.data_prep.far_ball_augment import (
    crop_ball_patch,
    paste_ball,
    sample_field_locations,
)


def test_crop_ball_patch_shape_and_bounds():
    band = np.full((100, 120), 128, np.uint8)
    out = crop_ball_patch(band, 60, 50, r=14)
    assert out is not None
    patch, alpha = out
    assert patch.shape == (28, 28) and alpha.shape == (28, 28)
    assert alpha[14, 14] > 0.9  # solid centre
    assert alpha[0, 0] == 0.0  # feathered corner
    assert crop_ball_patch(band, 5, 5, r=14) is None  # out of bounds


def test_paste_ball_composites_onto_background():
    band = np.full((80, 80), 100, np.uint8)
    band[36:44, 36:44] = 30  # a dark ball-ish patch to crop
    patch, alpha = crop_ball_patch(band, 40, 40, r=14)
    bg = np.full((80, 80), 200, np.uint8)
    before = bg.copy()
    assert paste_ball(bg, patch, alpha, 40, 40)
    assert not np.array_equal(bg, before)  # something changed
    assert bg[40, 40] < 200  # the dark ball centre darkened the bright background
    # corners untouched (alpha 0 there)
    assert bg[0, 0] == 200


def test_paste_ball_out_of_bounds_returns_false():
    band = np.full((60, 60), 120, np.uint8)
    patch, alpha = crop_ball_patch(band, 30, 30, r=14)
    bg = np.full((60, 60), 120, np.uint8)
    assert paste_ball(bg, patch, alpha, 2, 2) is False  # off the edge


def test_sample_field_locations_inside_mask():
    mask = np.zeros((50, 50), np.uint8)
    mask[20:30, 20:30] = 255
    locs = sample_field_locations(mask, 20, np.random.default_rng(0))
    assert len(locs) == 20
    assert all(20 <= x < 30 and 20 <= y < 30 for x, y in locs)


def test_patch_is_clean_accepts_ball_rejects_clutter():
    from training.data_prep.far_ball_augment import patch_is_clean

    r = 14
    yy, xx = np.mgrid[0 : 2 * r, 0 : 2 * r].astype(np.float32)
    alpha = np.clip((r * 0.6 - np.hypot(xx - r, yy - r)) / (r * 0.35), 0, 1)
    rng = np.random.default_rng(0)
    # clean: uniform grass (~120) + a small darker ball blob in the centre
    grass = np.full((2 * r, 2 * r), 120.0) + rng.normal(0, 4, (2 * r, 2 * r))
    ball = grass.copy()
    ball[r - 4 : r + 4, r - 4 : r + 4] = 90.0
    assert patch_is_clean(ball, alpha)
    # clutter: a bright player-shirt block in the border -> high border std
    clutter = grass.copy()
    clutter[:r, :r] = 240.0
    assert not patch_is_clean(clutter, alpha)
    # no ball: centre == grass (no contrast)
    assert not patch_is_clean(grass, alpha)
