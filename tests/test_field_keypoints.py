"""Unit tests for field-keypoint distillation helpers.

Covers the correctness-critical pure functions: the horizontal-flip index
remap, crop clipping, team routing / id building, polygon IoU, and the
placement clustering + split (which guard against train/val leakage).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from training.field_keypoints import make_game_id, slugify, team_from_name
from training.field_keypoints.augment import (
    augment_sample,
    flip_keypoints,
    transform_keypoints_for_crop,
)
from training.field_keypoints.dataset import (
    Sample,
    build_clusters,
    make_split,
    polygon_iou,
)


def _ring(top: float = 0.2, bot: float = 0.8) -> np.ndarray:
    near = [[0.1, bot], [0.3, bot], [0.5, bot], [0.7, bot], [0.9, bot]]
    far = [[0.9, top], [0.7, top], [0.5, top], [0.3, top], [0.1, top]]
    return np.array(near + far, dtype=np.float32)


# --- routing / ids -------------------------------------------------------


def test_team_from_name():
    assert team_from_name("Western New York Flash - 13B ECNL-RL Rochester") == "flash"
    assert team_from_name("BU14 - Guzzetta") == "heat"
    assert team_from_name("Hilton Heat") == "heat"
    assert team_from_name("Random Opponent FC") is None
    assert team_from_name("") is None
    assert team_from_name(None) is None


def test_slugify_and_game_id():
    assert slugify("Grace & Truth (home)") == "grace_truth_home"
    assert slugify("Davis Park") == "davis_park"
    assert (
        make_game_id("heat", "2026.05.27", "Chili Vortex", "Davis Park")
        == "heat__2026.05.27_vs_chili_vortex_davis_park"
    )


# --- flip remap (correctness-critical) -----------------------------------


def test_flip_keypoints_remap_and_mirror():
    kpts = np.array([[i / 10, 0.0 if i < 5 else 1.0] for i in range(10)], np.float32)
    scores = np.arange(10, dtype=np.float32)
    fk, fs = flip_keypoints(kpts, scores)
    # index 0 (near-left) takes mirrored original point 4 (near-right)
    assert np.isclose(fk[0, 0], 1.0 - 0.4)
    assert np.isclose(fk[0, 1], 0.0)
    assert fs[0] == 4
    # far center (7) is its own mirror partner
    assert fs[7] == 7


def test_flip_keypoints_is_involution():
    kpts = _ring()
    scores = np.linspace(0.3, 0.9, 10, dtype=np.float32)
    k2, s2 = flip_keypoints(*flip_keypoints(kpts, scores))
    assert np.allclose(k2, kpts)
    assert np.allclose(s2, scores)


# --- crop clipping -------------------------------------------------------


def test_crop_identity_keeps_all():
    kpts = _ring()
    new, in_frame = transform_keypoints_for_crop(kpts, 1000, 500, (0, 0, 1000, 500))
    assert in_frame.all()
    assert np.allclose(new, kpts, atol=1e-5)


def test_crop_drops_outside_points():
    kpts = _ring(top=0.1, bot=0.9)
    # crop to the left half -> right-side points fall out
    new, in_frame = transform_keypoints_for_crop(kpts, 1000, 500, (0, 0, 400, 500))
    assert not in_frame.all()
    assert in_frame[0]  # x=0.1 -> px 100 inside [0,400)
    assert not in_frame[4]  # x=0.9 -> px 900 outside
    assert (new >= 0).all() and (new <= 1).all()  # clamped


# --- polygon IoU ---------------------------------------------------------


def test_polygon_iou_identical_and_disjoint():
    a = _ring(top=0.2, bot=0.8)
    assert polygon_iou(a, a) > 0.99
    b = _ring(top=0.0, bot=0.15)  # thin band up top, far from a
    assert polygon_iou(a, b) < 0.2


# --- clustering + split --------------------------------------------------


def _samples(team, venue, poly, n=4, gate=True):
    return [
        Sample(
            jpg=Path("x.jpg"),
            kpts=poly,
            scores=np.full(10, 0.8, np.float32),
            mean_score=0.8,
            gate_pass=gate,
            team=team,
            venue=venue,
            game_id=f"{team}__{venue}",
        )
        for _ in range(n)
    ]


def test_cross_team_home_not_merged():
    # Same venue name "Home", different teams + different fields -> 2 clusters
    samples = _samples("flash", "Home", _ring(0.2, 0.8)) + _samples(
        "heat", "Home", _ring(0.0, 0.4)
    )
    info = build_clusters(samples)
    assert len(info) == 2
    assert len({s.cluster for s in samples}) == 2


def test_same_field_different_names_merge():
    # Same team, near-identical polygon, different venue spelling -> 1 cluster
    samples = _samples("heat", "Davis Park", _ring(0.20, 0.80)) + _samples(
        "heat", "Davis Park Field 2", _ring(0.21, 0.79)
    )
    info = build_clusters(samples)
    assert len(info) == 1


def test_make_split_partitions_all_clusters():
    samples = (
        _samples("heat", "Davis Park", _ring(0.2, 0.8))
        + _samples("heat", "Parma", _ring(0.15, 0.75))
        + _samples("heat", "Total Sports Experience", _ring(0.1, 0.5), gate=False)
        + _samples("heat", "Camp Eastman", _ring(0.25, 0.85))
        + _samples("flash", "Home", _ring(0.3, 0.9))
    )
    info = build_clusters(samples)
    split = make_split(info, seed=42)
    all_ids = set(info)
    got = set(split["train"]) | set(split["val"]) | set(split["test"])
    assert got == all_ids
    # disjoint
    assert not (set(split["train"]) & set(split["val"]))
    assert not (set(split["train"]) & set(split["test"]))
    assert not (set(split["val"]) & set(split["test"]))
    # determinism
    assert make_split(info, seed=42) == split


# --- augmentation smoke --------------------------------------------------


def test_augment_shapes_train_and_val():
    rng = np.random.default_rng(0)
    img = (rng.integers(0, 256, (540, 1920, 3), dtype=np.uint8)).astype(np.uint8)
    kpts = _ring()
    scores = np.full(10, 0.8, np.float32)
    for train in (True, False):
        out_img, out_k, out_s, in_frame = augment_sample(
            img, kpts, scores, rng, train=train
        )
        assert out_img.shape == (384, 768, 3)
        assert (
            out_img.dtype == np.float32 and 0.0 <= out_img.min() <= out_img.max() <= 1.0
        )
        assert out_k.shape == (10, 2)
        assert out_s.shape == (10,)
        assert in_frame.shape == (10,)
    # val mode is the deployment path: keypoints unchanged, all in-frame
    out_img, out_k, out_s, in_frame = augment_sample(
        img, kpts, scores, rng, train=False
    )
    assert np.allclose(out_k, kpts)
    assert in_frame.all()
