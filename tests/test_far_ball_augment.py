"""Tests for far-ball cut-paste augmentation (R7)."""

from __future__ import annotations

import numpy as np

from training.data_prep.far_ball_augment import (
    augment_crop_with_ball,
    crop_ball_patch,
    dim_ball,
    erase_ball,
    estimate_ball_velocity,
    match_contrast_to_background,
    occlude_ball,
    onfield_mask,
    paste_ball,
    path_onfield,
    sample_field_locations,
    sample_onfield_location,
    sample_realistic_contrast,
    sample_velocity,
    track_shift,
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


def test_augment_crop_with_ball_pastes_into_all_frames():
    # a real-ish ball patch (dark blob on grass)
    band = np.full((60, 60), 120, np.uint8)
    band[26:34, 26:34] = 80
    patch, alpha = crop_ball_patch(band, 30, 30, r=14)
    stack = np.full((3, 200, 200), 130, np.uint8)  # ball-free negative crop
    out = augment_crop_with_ball(stack, patch, alpha, 100, 100)  # vel=(0,0) -> static
    assert out.shape == (3, 200, 200)
    assert not np.array_equal(out, stack)  # input untouched, output changed
    for i in range(3):
        assert out[i, 100, 100] != 130  # ball pasted in every frame
    assert stack[0, 100, 100] == 130  # original not modified


def _ball_center(frame, around, win=12):
    """Darkest-pixel location near ``around`` (a dark ball on bright grass)."""
    ax, ay = around
    sub = frame[ay - win : ay + win, ax - win : ax + win].astype(np.float32)
    fy, fx = np.unravel_index(int(np.argmin(sub)), sub.shape)
    return ax - win + fx, ay - win + fy


def test_augment_motion_is_coherent_and_target_on_last_frame():
    """Ball traces a straight path; LAST frame sits on the label, earlier frames offset
    backwards by velocity (no iid jitter, no teleport)."""
    band = np.full((60, 60), 160, np.uint8)
    band[24:36, 24:36] = 40  # strong dark ball so the centroid is findable
    patch, alpha = crop_ball_patch(band, 30, 30, r=14)
    stack = np.full((3, 200, 240), 160, np.uint8)
    cx, cy = 140.0, 100.0
    vel = (8.0, 0.0)  # moving +x, 8 px/frame
    out = augment_crop_with_ball(stack, patch, alpha, cx, cy, vel=vel, blur=False)
    # frame t (last) on the target; frame t-2 is 2*vel back along the path
    cx2, _ = _ball_center(out[2], (int(cx), int(cy)))
    cx0, _ = _ball_center(out[0], (int(cx - 16), int(cy)))
    assert abs(cx2 - cx) <= 2  # last frame == label
    assert abs(cx0 - (cx - 16)) <= 2  # t-2 offset back by 2*vel (16 px)
    assert cx2 - cx0 >= 12  # the ball clearly MOVED across the stack


def test_estimate_velocity_round_is_slow_streak_is_directional():
    rng = np.random.default_rng(0)
    # round sharp ball -> near-static (small speed)
    band = np.full((60, 60), 160, np.uint8)
    cv2_circle = __import__("cv2").circle
    cv2_circle(band, (30, 30), 5, 40, -1)
    patch, alpha = crop_ball_patch(band, 30, 30, r=14)
    speeds = [
        float(np.hypot(*estimate_ball_velocity(patch, alpha, rng))) for _ in range(20)
    ]
    assert np.median(speeds) < 5.0  # round -> slow
    # horizontal streak -> velocity along x, clearly faster
    band2 = np.full((60, 60), 160, np.uint8)
    band2[28:33, 18:42] = 40  # a wide horizontal smear
    patch2, alpha2 = crop_ball_patch(band2, 30, 30, r=14)
    vs = [estimate_ball_velocity(patch2, alpha2, rng) for _ in range(40)]
    sp = np.median([np.hypot(vx, vy) for vx, vy in vs])
    assert sp > 8.0  # streak -> fast
    # dominant axis is horizontal: |vx| >> |vy| on average
    assert np.mean([abs(vx) for vx, vy in vs]) > 3 * np.mean([abs(vy) for vx, vy in vs])


def test_erase_ball_removes_the_blob():
    stack = np.full((3, 80, 80), 150, np.uint8)
    for i in range(3):
        __import__("cv2").circle(stack[i], (40, 40), 6, 30, -1)  # dark ball
    assert stack[:, 40, 40].mean() < 60  # ball present
    erase_ball(stack, 40, 40, r=12)
    assert stack[:, 40, 40].mean() > 120  # grass restored (ball gone) in every frame


def test_flip_mirrors_velocity():
    band = np.full((60, 60), 160, np.uint8)
    band[24:36, 24:36] = 40
    patch, alpha = crop_ball_patch(band, 30, 30, r=14)
    stack = np.full((3, 200, 240), 160, np.uint8)
    cx, cy = 140.0, 100.0
    out = augment_crop_with_ball(stack, patch, alpha, cx, cy, vel=(8.0, 0.0), flip=True)
    # with flip, +x velocity becomes -x: last frame on label, t-2 is 16px to the RIGHT
    cx2, _ = _ball_center(out[2], (int(cx), int(cy)))
    cx0, _ = _ball_center(out[0], (int(cx + 16), int(cy)))
    assert abs(cx2 - cx) <= 2
    assert abs(cx0 - (cx + 16)) <= 2


def test_occlude_ball_covers_part_keeps_rest():
    # bright ball on darker grass
    stack = np.full((3, 80, 80), 120, np.uint8)
    for i in range(3):
        __import__("cv2").circle(stack[i], (40, 40), 7, 200, -1)
    rng = np.random.default_rng(0)
    occlude_ball(stack, 40, 40, r=7, frac=0.5, rng=rng, level=30)
    ball = stack[0]
    disc = [
        (x, y)
        for y in range(33, 48)
        for x in range(33, 48)
        if (x - 40) ** 2 + (y - 40) ** 2 <= 49
    ]
    occluded = sum(ball[y, x] < 60 for x, y in disc)
    visible = sum(ball[y, x] > 150 for x, y in disc)
    assert (
        occluded > 0 and visible > 0
    )  # partial: some covered, some ball still showing


def test_dim_ball_reduces_contrast_toward_grass():
    stack = np.full((3, 80, 80), 120, np.uint8)
    for i in range(3):
        __import__("cv2").circle(stack[i], (40, 40), 6, 210, -1)  # bright ball
    before = int(stack[0, 40, 40])
    dim_ball(stack, 40, 40, r=6, factor=0.3)
    after = int(stack[0, 40, 40])
    assert before == 210
    # ball centre pulled toward grass (~120) in every frame, but not erased
    assert 120 < after < 210
    assert abs(after - 120) < abs(before - 120)
    assert all(stack[i, 40, 40] == after for i in range(3))


def test_track_shift_moves_ball_and_clears_original():
    # a bright ball on darker grass (a real-ish positive crop)
    stack = np.full((3, 200, 200), 120, np.uint8)
    for i in range(3):
        __import__("cv2").circle(stack[i], (100, 100), 6, 210, -1)
    rng = np.random.default_rng(0)
    out = track_shift(stack, 100, 100, rng, offset=(0.0, 50.0), scale=1.0)
    assert out is not None
    new_stack, nx, ny = out
    assert (nx, ny) == (100.0, 150.0)  # label shifted down 50px
    # the shifted location is now bright (ball pasted there) in frame t
    assert new_stack[2, 150, 100] > 160
    # the original location is cleared back toward grass (ball erased), not still bright
    assert new_stack[2, 100, 100] < 170
    assert stack[2, 100, 100] == 210  # input not modified


def test_track_shift_none_when_ball_too_near_edge():
    stack = np.full((3, 200, 200), 120, np.uint8)
    assert (
        track_shift(stack, 5, 5, np.random.default_rng(0), offset=(0.0, 30.0)) is None
    )


def test_sample_realistic_contrast_matches_distribution():
    rng = np.random.default_rng(0)
    cs = np.array([sample_realistic_contrast(rng) for _ in range(2000)])
    assert (cs > 0).mean() > 0.6  # ~71% brighter than grass (EXP-29)
    assert 5 < np.median(cs) < 25  # median ~+11
    assert (cs < 0).any()  # the in-shade darker tail exists
    assert cs.min() >= -45 and cs.max() <= 95


def test_match_contrast_sets_centre_relative_to_local_grass():
    # a ball patch whose centre is far brighter than its source grass
    band = np.full((60, 60), 200, np.uint8)
    __import__("cv2").circle(band, (30, 30), 5, 255, -1)
    patch, alpha = crop_ball_patch(band, 30, 30, r=14)
    out = match_contrast_to_background(
        patch, alpha, bg_level=120.0, target_contrast=10.0
    )
    centre = float(out[alpha > 0.8].mean())
    assert abs(centre - 130.0) < 3.0  # centre sits at local grass(120) + contrast(10)
    # a darker (in-shade) target pulls the ball below the local grass
    dark = match_contrast_to_background(
        patch, alpha, bg_level=120.0, target_contrast=-15.0
    )
    assert float(dark[alpha > 0.8].mean()) < 120.0


def test_sample_velocity_is_bounded():
    rng = np.random.default_rng(0)
    for _ in range(500):
        vx, vy = sample_velocity(rng, max_speed=22.0)
        assert (vx * vx + vy * vy) ** 0.5 <= 22.0 + 1e-6


def test_onfield_sampling_keeps_ball_on_pitch():
    # a masked crop: on-field square in the middle, off-field (0) border
    stack = np.zeros((3, 100, 100), np.uint8)
    stack[:, 30:70, 30:70] = 120
    mask = onfield_mask(stack)
    assert mask[50, 50] and not mask[5, 5]
    rng = np.random.default_rng(1)
    for _ in range(50):
        loc = sample_onfield_location(mask, r=6, rng=rng)
        assert loc is not None
        cx, cy = loc
        # whole footprint on-field (eroded by r)
        assert 36 <= cx <= 64 and 36 <= cy <= 64


def test_path_onfield_rejects_offfield_path():
    mask = np.zeros((100, 100), bool)
    mask[30:70, 30:70] = True
    # static ball in the middle: on-field
    assert path_onfield(mask, 50, 50, (0.0, 0.0), 3, r=5)
    # fast ball that started off the pitch (t-2 well outside): rejected
    assert not path_onfield(mask, 36, 50, (20.0, 0.0), 3, r=5)


def test_sample_onfield_location_none_when_no_room():
    stack = np.zeros((3, 40, 40), np.uint8)
    stack[:, 19:21, 19:21] = 120  # tiny on-field speck, no room for r=6 ball
    assert (
        sample_onfield_location(onfield_mask(stack), r=6, rng=np.random.default_rng(0))
        is None
    )
