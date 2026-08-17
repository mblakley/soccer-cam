"""Gates on the seam metric: it must respond to disparity and refuse thin data.

Real frames are not committed (they are 800 KB stills of a room), so the tests
synthesise a panorama from noise-driven texture and blend two shifted copies
across a 256-px window exactly as VIDEOPROC 2 does. That is enough to pin the
properties that matter: the metric must move when disparity is introduced, must
be two-sided rather than monotone, must be immune to the photometric step
between the two sensors, and must refuse to bless a result it cannot support.
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

VPE_DIR = Path(__file__).resolve().parents[1] / "reolink-firmware-patching" / "vpe"
sys.path.insert(0, str(VPE_DIR))

from seam_metric import (  # noqa: E402
    SeamGateFailed,
    check_acceptance,
    measure,
    seam_continuity_residual,
    seam_sharpness_ratio,
)

W, H = 2048, 600
SEAM = W // 2
BLEND = 256


def _texture(rng: np.random.Generator, w: int, h: int) -> np.ndarray:
    """Broadband texture with real edges, so gradient energy means something.

    Several smoothed octaves of noise plus a scatter of hard steps. The energy
    per column has to be roughly uniform -- a fixture with dead columns makes
    the shoulder background fit meaningless and the test would then be
    measuring the fixture rather than the metric.
    """
    img = np.zeros((h, w))
    for sigma, amp in ((1.2, 1.0), (3.0, 1.8), (9.0, 3.5)):
        img += amp * cv2.GaussianBlur(rng.normal(0.0, 1.0, (h, w)), (0, 0), sigma)
    for x in rng.integers(30, w - 30, 40):
        img[:, int(x) :] += rng.normal(0, 0.35)
    for y in rng.integers(30, h - 30, 30):
        img[int(y) :, :] += rng.normal(0, 0.25)
    img = 128 + 24 * (img - img.mean()) / (img.std() + 1e-9)
    return np.clip(img, 0, 255)


def build_panorama(
    disparity: int = 0,
    photometric_step: float = 0.0,
    hard_step: float = 0.0,
    seed: int = 7,
) -> np.ndarray:
    """One scene seen by two sensors, butt-joined with a 256-px linear blend.

    `disparity` shifts the right sensor's view of the world, which is exactly
    what a depth away from the stitch's calibration distance produces.
    `photometric_step` is an AE difference that the blend ramps in over the
    window; `hard_step` is one applied abruptly at the seam column, which is
    the case detrending exists to survive.
    """
    rng = np.random.default_rng(seed)
    world = _texture(rng, W + 512, H)
    left = world[:, 256 : 256 + W]
    right = np.roll(world, -disparity, axis=1)[:, 256 : 256 + W] + photometric_step

    out = left.copy()
    lo, hi = SEAM - BLEND // 2, SEAM + BLEND // 2
    alpha = np.linspace(0.0, 1.0, hi - lo)[None, :]
    out[:, hi:] = right[:, hi:]
    out[:, lo:hi] = left[:, lo:hi] * (1 - alpha) + right[:, lo:hi] * alpha
    if hard_step:
        out[:, SEAM:] += hard_step
    return np.clip(out, 0, 255).astype(np.uint8)


def ssr(img, **kw):
    return seam_sharpness_ratio(img, seam_x=SEAM, blend_w=BLEND, shoulder_w=384, **kw)


# -- SSR ---------------------------------------------------------------------


def test_ssr_is_near_one_for_a_registered_seam():
    r = ssr(build_panorama(0))
    assert r.abs_ln_ssr < 0.35, f"|ln SSR| {r.abs_ln_ssr:.3f} on a perfect seam"


def test_ssr_detects_disparity():
    clean = ssr(build_panorama(0)).abs_ln_ssr
    for d in (4, 12, 32):
        bad = ssr(build_panorama(d)).abs_ln_ssr
        assert bad > clean + 0.10, (
            f"disparity {d} px moved |ln SSR| only {clean:.3f} -> {bad:.3f}"
        )


def test_ssr_saturates_and_is_therefore_a_detector_not_an_estimator():
    """The property that decides how SSR may be used.

    Measured on this fixture (and matching the design's independent table on a
    real frame to within a few percent): 1.011, 0.885, 0.657, 0.557, 0.583 for
    0/1/2/3/4 px, then flat at ~0.68 from 8 px to 200 px. So it separates a
    registered seam from a misregistered one cleanly, and says nothing at all
    about *how* misregistered beyond ~4 px. A solver must therefore descend
    SCR, not this -- and a loop that optimised SSR would wander.
    """
    curve = {d: ssr(build_panorama(d)).ssr for d in (0, 1, 2, 8, 16, 64, 200)}
    assert curve[0] > 0.9, f"clean seam should sit near 1.0, got {curve[0]:.3f}"
    assert curve[1] < curve[0] and curve[2] < curve[1], "small disparity must bite"
    tail = [curve[d] for d in (8, 16, 64, 200)]
    assert max(tail) - min(tail) < 0.08, (
        f"the tail should be flat (saturated), spread {max(tail) - min(tail):.3f}"
    )
    assert max(tail) < curve[0] - 0.15, "saturated tail must still be clearly worse"


def test_ssr_can_exceed_one_so_the_metric_must_be_two_sided():
    """`|ln SSR|`, not `1 - SSR`.

    Blending suppresses energy, so misregistration usually drives SSR *below*
    1 -- but the ratio is against a background fitted on the shoulders, and
    content that is intrinsically busier at the seam than on the shoulders
    drives it *above* 1. The live camera frame does exactly that (SSR 3.64 at
    ~0.3 m subject distance). A one-sided reading would score that as better
    than perfect.
    """
    img = build_panorama(0).astype(np.float64)
    lo, hi = SEAM - BLEND // 2, SEAM + BLEND // 2
    rng = np.random.default_rng(3)
    img[:, lo:hi] += rng.normal(0, 26, img[:, lo:hi].shape)
    r = ssr(np.clip(img, 0, 255).astype(np.uint8))
    assert r.ssr > 1.2, f"busy-seam case should exceed 1, got {r.ssr:.3f}"
    assert r.abs_ln_ssr > 0.15, "and |ln SSR| must register it as bad"


def test_detrending_survives_a_hard_photometric_step():
    """The two sensors run independent AE and differ by tens of grey levels.

    Correcting the design: on this camera that step is ramped in across the
    256-px blend window, so its per-column gradient is ~step/256 and it barely
    reaches the raw metric at all (measured on the live frame: raw 3.633 vs
    detrended 3.639, a 0.2% difference -- not the "swamps the structural
    signal" the design claims). Detrending is still worth its two lines,
    because a *hard* step -- which is what an unblended or narrowly-blended
    seam would give -- does pollute the raw figure, as this test shows.
    """
    r_ramped = ssr(build_panorama(0, photometric_step=60.0))
    assert r_ramped.ssr_raw_undetrended == pytest.approx(r_ramped.ssr, rel=0.05), (
        "a blend-ramped step should not move either figure much"
    )

    r_hard = ssr(build_panorama(0, hard_step=90.0))
    assert r_hard.abs_ln_ssr < 0.35, (
        f"detrended metric polluted by a hard step: {r_hard.abs_ln_ssr:.3f}"
    )
    assert r_hard.ssr_raw_undetrended > r_hard.ssr * 1.5, (
        f"the raw metric should be the one that breaks: raw "
        f"{r_hard.ssr_raw_undetrended:.3f} vs detrended {r_hard.ssr:.3f}"
    )


# -- SCR ---------------------------------------------------------------------


def test_scr_reports_nothing_rather_than_guessing_on_a_blank_seam():
    """Featureless grass is the expected case at mid-field, and the honest
    output there is 'no observations', not an extrapolation from three."""
    flat = np.full((H, W), 120, dtype=np.uint8)
    r = seam_continuity_residual(flat, seam_x=SEAM, blend_w=BLEND, shoulder_w=384)
    assert r.n == 0
    assert np.isnan(r.p90)


def test_scr_finds_a_line_that_crosses_the_seam_and_measures_its_break():
    """A sloped line drawn across the seam with the right half displaced
    horizontally by `dx` breaks vertically by -slope*dx at the seam."""
    img = np.full((H, W), 40, dtype=np.uint8)
    slope, dx = 0.20, 10.0
    for x in range(W):
        # right of the seam the world is shifted right by dx
        xs = x - dx if x > SEAM else x
        y = int(round(300 + slope * (xs - SEAM)))
        if 3 <= y < H - 3:
            img[y - 2 : y + 3, x] = 230
    r = seam_continuity_residual(
        img, seam_x=SEAM, blend_w=BLEND, shoulder_w=384, band=(0, H), min_len=40
    )
    assert r.n >= 1, "a high-contrast line across the seam must be found"
    got = r.observations[0].residual_y
    assert got == pytest.approx(-slope * dx, abs=0.8), (
        f"seam break {got:.2f} px, expected {-slope * dx:.2f}"
    )


# -- acceptance --------------------------------------------------------------


def _fake(n, bands, cov, p90, ln_ssr):
    return {
        "scr": {
            "n": n,
            "p50": p90 / 2,
            "p90": p90,
            "max": p90,
            "row_bands_covered": bands,
            "height_coverage": cov,
            "implied_dx": 0.0,
            "implied_dy": 0.0,
            "slope_spread": 0.2,
        },
        "ssr": {"abs_ln_ssr": ln_ssr, "noise_floor": 0.10},
    }


def test_acceptance_passes_a_genuine_improvement():
    check_acceptance(_fake(20, 3, 0.8, 6.8, 0.51), _fake(20, 3, 0.8, 1.4, 0.09))


def test_acceptance_refuses_thin_coverage():
    """This is the case the live indoor frame actually hits, and the gate must
    stop it rather than report a number nobody can trust."""
    with pytest.raises(SeamGateFailed, match="row band"):
        check_acceptance(_fake(13, 1, 0.19, 31.4, 1.29), _fake(13, 1, 0.19, 1.4, 0.09))


def test_acceptance_refuses_too_few_observations():
    with pytest.raises(SeamGateFailed, match="accepted structures"):
        check_acceptance(_fake(3, 3, 0.8, 6.8, 0.51), _fake(3, 3, 0.8, 1.0, 0.09))


def test_acceptance_refuses_a_panorama_that_merely_moved():
    """SCR improves, |ln SSR| flat. That is a post-fusion shift, and the one
    thing the camera-side surface is supposed to beat."""
    with pytest.raises(SeamGateFailed, match="only moves the panorama"):
        check_acceptance(_fake(20, 3, 0.8, 6.8, 0.51), _fake(20, 3, 0.8, 1.4, 0.51))


def test_acceptance_refuses_when_ssr_regresses():
    with pytest.raises(SeamGateFailed, match="got worse"):
        check_acceptance(_fake(20, 3, 0.8, 6.8, 0.20), _fake(20, 3, 0.8, 1.4, 0.90))


def test_acceptance_refuses_a_small_improvement():
    with pytest.raises(SeamGateFailed, match="improved"):
        check_acceptance(_fake(20, 3, 0.8, 3.0, 0.51), _fake(20, 3, 0.8, 1.9, 0.09))


def test_measure_returns_a_serialisable_report():
    import json

    m = measure(build_panorama(8), seam_x=SEAM, blend_w=BLEND)
    json.dumps(m)  # must not raise
    assert m["blend_window"] == [SEAM - BLEND // 2, SEAM + BLEND // 2]
    assert "abs_ln_ssr" in m["ssr"]
