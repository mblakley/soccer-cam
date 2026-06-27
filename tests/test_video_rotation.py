"""Orientation handling: PyAV ignores a video's display-rotation tag, so the crop builders must
apply it in memory. These cover the helper + the game.json fallback resolver."""

import json

import numpy as np

from training.data_prep.warped_dataset import (
    apply_display_rotation,
    resolve_video_rotation,
)


def _img():
    return np.arange(2 * 3 * 3, dtype=np.uint8).reshape(2, 3, 3)


def test_apply_180_flips_both_axes():
    img = _img()
    for rot in (180, -180):
        out = apply_display_rotation(img, rot)
        assert np.array_equal(out, img[::-1, ::-1]), f"rot={rot} should be a 180 flip"


def test_apply_zero_is_noop():
    img = _img()
    assert apply_display_rotation(img, 0) is img
    assert apply_display_rotation(img, None) is img
    assert apply_display_rotation(img, 90) is img  # only +/-180 occurs in this corpus


def test_resolve_explicit_nonzero_wins():
    assert resolve_video_rotation("/no/such/video.mp4", -180) == -180


def test_resolve_falls_back_to_game_json(tmp_path):
    (tmp_path / "game.json").write_text(json.dumps({"video_rotation": -180}))
    vid = tmp_path / "combined.mp4"
    vid.write_text("")
    # no explicit, and explicit 0 (falsy) both fall back to the canonical game.json value
    assert resolve_video_rotation(str(vid), None) == -180
    assert resolve_video_rotation(str(vid), 0) == -180


def test_resolve_default_zero_when_no_game_json(tmp_path):
    vid = tmp_path / "combined.mp4"
    vid.write_text("")
    assert resolve_video_rotation(str(vid), None) == 0
