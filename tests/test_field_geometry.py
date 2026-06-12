"""Tests for ``inference.field_geometry.load_field`` — covers the
stabilization-aware coord-space shift that the track + render steps rely on
when the upstream stabilize step ran and produced a motion sidecar."""

from __future__ import annotations

import json

import numpy as np
import pytest

from video_grouper.inference.field_geometry import load_field


def _write_polygon(path, polygon: list[list[float]], keypoints=None, homography=None):
    payload = {"polygon": polygon}
    if keypoints is not None:
        payload["keypoints"] = keypoints
    if homography is not None:
        payload["homography"] = homography
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_motion(path, safe_inset_yx: tuple[int, int]):
    payload = {
        "src_size": [2160, 7680],
        "output_size": [2160 - 2 * safe_inset_yx[0], 7680 - 2 * safe_inset_yx[1]],
        "safe_inset": list(safe_inset_yx),
        "frames": [],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_load_field_no_motion_path_returns_raw_polygon(tmp_path):
    pgon = tmp_path / "polygon.json"
    raw = [[100.0, 200.0], [300.0, 200.0], [300.0, 400.0], [100.0, 400.0]]
    _write_polygon(pgon, raw)

    polygon, homography = load_field(str(pgon))
    assert polygon is not None
    np.testing.assert_array_equal(polygon, np.array(raw, dtype=np.float32))
    assert homography is None


def test_load_field_with_motion_path_shifts_polygon_by_safe_inset(tmp_path):
    pgon = tmp_path / "polygon.json"
    motion = tmp_path / "motion.json"
    raw = [[100.0, 200.0], [300.0, 200.0], [300.0, 400.0], [100.0, 400.0]]
    _write_polygon(pgon, raw)
    _write_motion(motion, safe_inset_yx=(60, 80))  # (y, x)

    polygon, _ = load_field(str(pgon), motion_path=str(motion))
    assert polygon is not None
    # Every vertex must lose (x=80, y=60) so it lines up with stabilized frames.
    expected = np.array(
        [[20.0, 140.0], [220.0, 140.0], [220.0, 340.0], [20.0, 340.0]],
        dtype=np.float32,
    )
    np.testing.assert_allclose(polygon, expected, rtol=0, atol=1e-5)


def test_load_field_with_motion_path_shifts_homography(tmp_path):
    pgon = tmp_path / "polygon.json"
    motion = tmp_path / "motion.json"
    raw = [[100.0, 200.0], [300.0, 200.0], [300.0, 400.0]]
    # Identity-like homography (well, scaled identity) so the math is easy
    # to reason about: H_raw(p) = p in field-plane coords.
    H_raw = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    _write_polygon(pgon, raw, homography=H_raw)
    _write_motion(motion, safe_inset_yx=(60, 80))

    polygon, H = load_field(str(pgon), motion_path=str(motion))
    assert H is not None
    # Apply H to a stabilized-coord point and verify it recovers the raw
    # coord it was shifted from — that's the whole point of the shift: a
    # ball whose detection comes back at (raw - inset) in stabilized space
    # must still map to the same field-plane coordinate it would have in
    # raw coords.
    stab_pt = np.array([[[20.0, 140.0]]], dtype=np.float32)
    import cv2

    field_pt = cv2.perspectiveTransform(stab_pt, H)
    np.testing.assert_allclose(field_pt[0, 0], [100.0, 200.0], rtol=0, atol=1e-4)


def test_load_field_unusable_motion_returns_raw(tmp_path, caplog):
    pgon = tmp_path / "polygon.json"
    raw = [[100.0, 200.0], [300.0, 200.0], [300.0, 400.0]]
    _write_polygon(pgon, raw)

    # Missing motion file: must not crash, must return raw polygon (the safe
    # fallback — better to mis-filter by safe_inset than to drop the step).
    polygon, _ = load_field(str(pgon), motion_path=str(tmp_path / "missing.json"))
    np.testing.assert_array_equal(polygon, np.array(raw, dtype=np.float32))


def test_load_field_no_polygon_path_returns_none():
    polygon, H = load_field(None)
    assert polygon is None
    assert H is None


def test_load_field_unreadable_polygon_returns_none(tmp_path):
    polygon, H = load_field(str(tmp_path / "missing.json"))
    assert polygon is None
    assert H is None


@pytest.mark.parametrize("safe_inset_yx", [(0, 0), (10, 20), (125, 125)])
def test_load_field_shift_roundtrip(tmp_path, safe_inset_yx):
    """A polygon at raw coord (X, Y) ends up at stabilized coord
    (X - inset_x, Y - inset_y), regardless of the inset magnitude."""
    pgon = tmp_path / "polygon.json"
    motion = tmp_path / "motion.json"
    raw = [[100.0, 200.0], [3500.0, 200.0], [3500.0, 1800.0], [100.0, 1800.0]]
    _write_polygon(pgon, raw)
    _write_motion(motion, safe_inset_yx=safe_inset_yx)

    polygon, _ = load_field(str(pgon), motion_path=str(motion))
    iy, ix = safe_inset_yx
    expected = np.array(
        [[v[0] - ix, v[1] - iy] for v in raw],
        dtype=np.float32,
    )
    np.testing.assert_allclose(polygon, expected, rtol=0, atol=1e-5)
