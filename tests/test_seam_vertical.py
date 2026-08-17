"""Gates on the two instruments that answer "can this picture see a shift".

Both exist because of a measurement, not a hunch. On the archived Duo 3 frame
set the seam metric reports 40-69 paired structures, passes every coverage
gate, and returns a p90 of 27-36 px on frames whose seams are visibly
registered -- and sliding dx across its whole plausible range moves that p90 by
1-15%. Restricting the sweep to the observations that can see dx at all does
not rescue it either: on three games it moved p90 by 1.3%, 2.9% and 1.8%.

The reason is geometric and is pinned below. SCR fits *near-horizontal*
structures, `r_y = -m*dx`, and a horizontal edge is invariant under a
horizontal shift. On a soccer field almost everything crossing a vertical seam
runs horizontally, so almost every observation is blind to the thing being
tuned. The remedy is not a better statistic. It is upright structure in the
seam -- a person -- and `vertical_structure` is what checks whether one is
there.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import pytest

VPE_DIR = Path(__file__).resolve().parents[1] / "reolink-firmware-patching" / "vpe"
sys.path.insert(0, str(VPE_DIR))

from seam_vertical import (  # noqa: E402
    MIN_VERTICAL_RATIO,
    dx_sensitivity,
    split_by_dx_sensitivity,
    summarise,
    vertical_structure,
)

W, H = 2048, 900
SEAM = W // 2
BLEND = 256
PERSON = (300, 640)


@dataclass
class _Obs:
    slope: float
    residual_perp: float


def _scene(person: bool = False, seed: int = 5) -> np.ndarray:
    """Grass-like texture, optionally with someone standing in the seam."""
    rng = np.random.default_rng(seed)
    img = 110 + 9.0 * cv2.GaussianBlur(rng.normal(0.0, 1.0, (H, W)), (0, 0), 1.1)
    img += 6.0 * cv2.GaussianBlur(rng.normal(0.0, 1.0, (H, W)), (0, 0), 5.0)
    if person:
        y0, y1 = PERSON
        rows = np.arange(y0, y1)
        half = np.where(rows < y0 + 70, 12, np.where(rows < y0 + 230, 30, 22))
        body = rng.normal(45.0, 22.0, (y1 - y0, 60))
        for k, row in enumerate(rows):
            hw = int(half[k])
            img[row, SEAM - hw : SEAM + hw] = body[k, : 2 * hw]
    return np.dstack([np.clip(img, 0, 255).astype(np.uint8)] * 3)


# -- sensitivity -------------------------------------------------------------


def test_a_horizontal_structure_cannot_see_a_horizontal_shift():
    """The whole mechanism, in one assertion.

    `r_y = -m*dx` and `residual_perp = |r_y| / hypot(1, m)`, so the derivative
    of the reported residual with respect to dx is the structure's sine from
    horizontal.
    """
    assert dx_sensitivity(0.0) == 0.0
    assert dx_sensitivity(0.05) == pytest.approx(0.0499, abs=1e-3)
    assert dx_sensitivity(1.0) == pytest.approx(0.7071, abs=1e-4)
    assert dx_sensitivity(-0.35) == dx_sensitivity(0.35)
    # The detector's own ceiling: nothing steeper than 0.35 is ever accepted,
    # so a third of a pixel of residual per pixel of shift is the best SCR can
    # ever do on this camera.
    assert dx_sensitivity(0.35) < 0.35


def test_the_split_separates_what_can_steer_from_what_cannot():
    obs = [_Obs(0.0, 30.0), _Obs(0.01, 28.0), _Obs(0.20, 4.0), _Obs(-0.30, 6.0)]
    split = split_by_dx_sensitivity(obs)
    assert split.n_total == 4
    assert split.n_steering == 2
    assert split.blind_fraction == 0.5
    # The number an operator is trying to minimise, over the structures that
    # can respond to the control in their hand.
    assert split.p90_steering == pytest.approx(5.8, abs=0.3)
    assert split.p90_blind == pytest.approx(29.8, abs=0.3)
    assert split.max_sensitivity == pytest.approx(0.287, abs=0.01)


def test_an_all_horizontal_set_reports_nothing_steering_rather_than_a_number():
    """The real case: many observations, none of them evidence.

    Nine paired structures, every gate an acceptance check applies would pass,
    and a p90 of 30 px -- of which not one pixel is a statement about
    horizontal registration, because at |m| = 0.04 a 20 px error moves the
    residual by 0.8 px.
    """
    split = split_by_dx_sensitivity([_Obs(0.01 * i, 30.0) for i in range(-4, 5)])
    assert split.n_total == 9
    assert split.n_steering == 0
    assert split.blind_fraction == 1.0
    assert split.p90_steering != split.p90_steering, "no steering subset, no number"
    assert split.p90_blind == pytest.approx(30.0)


def test_an_empty_set_is_empty_not_an_exception():
    split = split_by_dx_sensitivity([])
    assert split.n_total == 0 and split.n_steering == 0
    assert split.blind_fraction == 0.0


# -- presence of upright structure -------------------------------------------


def test_bare_grass_reports_no_upright_structure():
    profile = vertical_structure(_scene(person=False), seam_x=SEAM, blend_w=BLEND)
    assert profile.n_with_structure == 0
    assert profile.rows_with_structure == []
    assert profile.best.ratio < MIN_VERTICAL_RATIO
    # Measured on 87 frames of the archived set: the median band ratio is 1.23
    # and the 25th percentile 0.94, so a featureless corridor sits near 1.
    assert 0.4 < profile.best.ratio < 2.0


def test_a_person_in_the_seam_is_found_and_the_rows_are_reported():
    profile = vertical_structure(_scene(person=True), seam_x=SEAM, blend_w=BLEND)
    assert profile.n_with_structure >= 3
    assert profile.best.ratio > 3 * MIN_VERTICAL_RATIO
    covered = [
        r for r in profile.rows_with_structure if r[0] < PERSON[1] and r[1] > PERSON[0]
    ]
    assert covered, profile.rows_with_structure
    lo, hi = covered[0]
    assert PERSON[0] - 60 <= lo and hi <= PERSON[1] + 60
    assert abs(profile.best.peak_x - SEAM) <= BLEND // 2


def test_a_person_beside_the_corridor_is_not_a_person_in_it():
    """Standing near the seam is not standing in it, and the tool must not
    say otherwise -- the whole instruction is about where they stand."""
    img = _scene(person=True)
    shifted = np.roll(img, 600, axis=1)  # same person, 600 px away from the seam
    profile = vertical_structure(shifted, seam_x=SEAM, blend_w=BLEND)
    assert profile.n_with_structure == 0


def test_the_summary_leads_with_the_verdict_and_carries_the_numbers():
    profile = vertical_structure(_scene(person=True), seam_x=SEAM, blend_w=BLEND)
    out = summarise(profile, split_by_dx_sensitivity([_Obs(0.0, 9.0), _Obs(0.3, 2.0)]))
    assert out["n_with_structure"] >= 3
    assert out["best_rows"][0] < out["best_rows"][1]
    assert out["corridor"] == [SEAM - BLEND // 2, SEAM + BLEND // 2]
    assert len(out["bands"]) == out["n_bands"]
    assert out["scr_split"]["n_steering"] == 1
    assert out["scr_split"]["blind_fraction"] == 0.5


def test_a_frame_too_short_to_band_returns_empty_rather_than_dividing_by_zero():
    profile = vertical_structure(np.zeros((8, 600, 3), np.uint8), seam_x=300)
    assert profile.bands == []
    assert profile.best is None
    assert summarise(profile)["n_with_structure"] == 0
