"""Gates on the incremental SCR path, and on the sign of `dx`.

Two things are pinned here, both of which the interactive calibration UI
depends on and neither of which is safe to assume.

**The fast path must equal the slow path.** `detect_shoulder_chains` +
`residual_from_chains(anchors)` scores a candidate curve without re-running the
detector, which is what lets an operator drag and watch the objective move.
That is only legitimate if it agrees with running the whole metric on a frame
that really was shifted -- so the tests below do exactly that comparison rather
than asserting the algebra is right.

**`dx` closes the seam and `-dx` opens it.** This is the signed sanity gate of
`docs/STITCH_CALIBRATION.md` 4.4, run in simulation, where it costs a second
instead of a camera write. It also pins the trap it exists to catch: the sign
of `ScrResult.implied_dx` is the *opposite* of the sign of `dx_anchors`.

The SSR fixture in `test_seam_metric.py` cannot be reused: its structures are
near-horizontal steps, and a horizontal misregistration displaces a structure
vertically by `-m * dx`, so a flat line sees nothing (the metric says so
itself). This fixture therefore draws lines with a deliberate spread of slopes.
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
    detect_shoulder_chains,
    interp_dx_anchors,
    residual_from_chains,
    seam_continuity_residual,
)

from video_grouper.utils.stitch_remap import (  # noqa: E402
    apply_shift_to_frame_rgb,
)

W, H = 2048, 900
SEAM = W // 2
BLEND = 256
SHOULDER = 384

# Slopes deliberately spread: `implied_dx` comes from regressing the vertical
# residual on the slope, so a fixture of parallel lines would leave it
# under-determined and the test would pass on a metric that measured nothing.
_LINES = (
    (-0.30, 760),
    (-0.16, 300),
    (-0.05, 620),
    (0.07, 180),
    (0.19, 500),
    (0.30, 80),
)


def _scene(rng: np.random.Generator, w: int, h: int) -> np.ndarray:
    """Smooth background plus straight lines of varying slope."""
    img = 110 + 6 * cv2.GaussianBlur(rng.normal(0.0, 1.0, (h, w)), (0, 0), 5.0)
    xs = np.arange(w)
    for m, y0 in _LINES:
        ys = y0 + m * (xs - w / 2)
        for offset, amp in ((-1, 30), (0, 55), (1, 30)):
            yy = np.clip(np.round(ys + offset).astype(int), 0, h - 1)
            img[yy, xs] += amp
    return np.clip(img, 0, 255)


def build_panorama(disparity: int = 0, seed: int = 3) -> np.ndarray:
    """One scene seen by two sensors, butt-joined with a 256-px linear blend.

    `disparity` shifts the right sensor's view of the world left by that many
    pixels, so its content sits `disparity` px to the LEFT of where the left
    sensor puts it. Registering the two therefore requires moving the right
    half RIGHT by `disparity` -- i.e. the correcting `dx` is `+disparity`, in
    the project's sign convention.
    """
    rng = np.random.default_rng(seed)
    world = _scene(rng, W + 512, H)
    left = world[:, 256 : 256 + W]
    right = np.roll(world, -disparity, axis=1)[:, 256 : 256 + W]

    out = left.copy()
    lo, hi = SEAM - BLEND // 2, SEAM + BLEND // 2
    alpha = np.linspace(0.0, 1.0, hi - lo)[None, :]
    out[:, hi:] = right[:, hi:]
    out[:, lo:hi] = left[:, lo:hi] * (1 - alpha) + right[:, lo:hi] * alpha
    return np.dstack([np.clip(out, 0, 255).astype(np.uint8)] * 3)


def _scr(img: np.ndarray):
    return seam_continuity_residual(
        img, seam_x=SEAM, blend_w=BLEND, shoulder_w=SHOULDER
    )


def _apply(img: np.ndarray, anchors: list[tuple[float, float]]) -> np.ndarray:
    """Shift the right half per-row exactly as the shipped corrector does."""
    ys = [a[0] for a in anchors]
    ds = [a[1] for a in anchors]
    lut = np.round(np.interp(np.arange(H), ys, ds)).astype(np.int32)
    return apply_shift_to_frame_rgb(img, lut, SEAM)


def _incremental(img: np.ndarray, anchors: list[tuple[float, float]]):
    chains = detect_shoulder_chains(
        img, seam_x=SEAM, blend_w=BLEND, shoulder_w=SHOULDER
    )
    return residual_from_chains(chains, anchors)


# -- the sign convention -----------------------------------------------------


def test_the_fixture_is_actually_misregistered():
    """Guard the guard: if disparity did nothing, every test below is vacuous."""
    clean, bad = _scr(build_panorama(0)), _scr(build_panorama(6))
    assert clean.n >= 6, f"fixture yields only {clean.n} structures"
    assert bad.p50 > clean.p50 + 0.5, (
        f"disparity moved SCR p50 only {clean.p50:.2f} -> {bad.p50:.2f}"
    )


@pytest.mark.parametrize("disparity", [4, 6, 10])
def test_positive_dx_closes_the_seam_and_negative_dx_doubles_it(disparity):
    """The signed sanity gate of STITCH_CALIBRATION.md 4.4, in simulation.

    `dx` means "px the RIGHT half must move right to register with the left".
    A sign error here does not merely fail to help -- it applies the
    misregistration twice, and presents to the operator as "the tool doesn't
    work". Running the gate against a synthetic frame costs a second and needs
    no camera, so there is no excuse for shipping it unchecked.
    """
    img = build_panorama(disparity)
    before = _scr(img).p50
    corrected = _scr(_apply(img, [(0, disparity), (H - 1, disparity)])).p50
    doubled = _scr(_apply(img, [(0, -disparity), (H - 1, -disparity)])).p50

    assert corrected < before * 0.4, (
        f"dx=+{disparity} should close the seam: p50 {before:.2f} -> {corrected:.2f}"
    )
    assert doubled > before * 1.5, (
        f"dx=-{disparity} should roughly double the error: "
        f"p50 {before:.2f} -> {doubled:.2f}"
    )


@pytest.mark.parametrize("disparity", [4, 8])
def test_implied_dx_carries_the_opposite_sign_to_dx_anchors(disparity):
    """A trap, pinned so nobody walks into it twice.

    `ScrResult.implied_dx` comes from fitting `r_y = dy - m*dx`, and that `dx`
    is the misregistration *present in the image*, not the correction for it.
    It is therefore the NEGATIVE of a `dx_anchors` value. Feeding `implied_dx`
    straight into an anchor curve is the sign error of 4.4, and it is an easy
    one to make because both quantities are called `dx`.
    """
    r = _scr(build_panorama(disparity))
    assert r.implied_dx == pytest.approx(-disparity, abs=1.0), (
        f"implied_dx {r.implied_dx:+.2f} for a seam that needs dx=+{disparity}"
    )
    # And the correction derived by negating it does close the seam.
    fixed = _scr(_apply(build_panorama(disparity), [(0, -r.implied_dx)])).p50
    assert fixed < _scr(build_panorama(disparity)).p50 * 0.5


# -- the incremental path ----------------------------------------------------


@pytest.mark.parametrize("dx", [0, 3, 6, -5])
def test_incremental_scoring_matches_a_genuinely_shifted_frame(dx):
    """The claim the interactive UI rests on, checked rather than argued.

    Scoring a candidate curve from cached chains must give the same answer as
    detecting on the frame that curve produces. It cannot be identical -- a
    shear perturbs the gradient field, so a marginal structure could appear or
    vanish, and the shipped corrector rounds dx to whole pixels -- so this pins
    agreement to a tolerance well inside the 2.0 px acceptance gate rather than
    claiming equality.
    """
    img = build_panorama(6)
    anchors = [(0.0, float(dx)), (float(H - 1), float(dx))]

    truth = _scr(_apply(img, anchors))
    fast = _incremental(img, anchors)

    assert fast.n == truth.n, f"n differs: fast {fast.n} vs truth {truth.n}"
    assert fast.p50 == pytest.approx(truth.p50, abs=0.35)
    assert fast.p90 == pytest.approx(truth.p90, abs=0.35)


def test_incremental_scoring_tracks_a_sloped_curve_not_just_a_constant():
    """A roll is linear in y, so the curve that matters is not a translation.

    A constant dx cannot express it, and an implementation that applied the
    curve to the fitted intercept instead of to the points would get the slope
    wrong. Compare against the real thing on a ramp.
    """
    img = build_panorama(6)
    anchors = [(0.0, -4.0), (float(H - 1), 8.0)]
    truth = _scr(_apply(img, anchors))
    fast = _incremental(img, anchors)
    assert fast.n == truth.n
    assert fast.p50 == pytest.approx(truth.p50, abs=0.35)
    assert fast.p90 == pytest.approx(truth.p90, abs=0.35)


def test_incremental_reuse_is_what_makes_it_cheap():
    """One detection, many scores -- the property, not the wall-clock time.

    Timing assertions are flaky on shared CI, so this pins the structural
    claim instead: scoring N curves does N fits and zero detections, and the
    scores genuinely differ from one another.
    """
    chains = detect_shoulder_chains(
        build_panorama(6), seam_x=SEAM, blend_w=BLEND, shoulder_w=SHOULDER
    )
    scores = [
        residual_from_chains(chains, [(0.0, float(d)), (float(H - 1), float(d))]).p50
        for d in (0, 3, 6, 9)
    ]
    assert scores[2] < scores[0], "dx=+6 should beat dx=0 on a +6 disparity"
    assert len({round(s, 3) for s in scores}) == len(scores), (
        f"scores should all differ, got {scores}"
    )


def test_residual_from_chains_with_no_anchors_equals_the_one_shot_metric():
    """The refactor is a split, not a rewrite: the composed path must be
    bit-identical to scoring the cached chains with no correction."""
    img = build_panorama(5)
    chains = detect_shoulder_chains(
        img, seam_x=SEAM, blend_w=BLEND, shoulder_w=SHOULDER
    )
    a, b = residual_from_chains(chains), _scr(img)
    assert (a.n, a.p50, a.p90, a.max) == (b.n, b.p50, b.p90, b.max)


# -- the interpolator --------------------------------------------------------


def test_anchor_interpolation_clamps_outside_its_range():
    """Matches `np.interp` / `build_dx_lookup`, so one curve means one curve."""
    anchors = [(100.0, -4.0), (500.0, 4.0)]
    assert interp_dx_anchors(anchors, 0) == pytest.approx(-4.0)
    assert interp_dx_anchors(anchors, 300) == pytest.approx(0.0)
    assert interp_dx_anchors(anchors, 9999) == pytest.approx(4.0)
    assert interp_dx_anchors([], 42) == 0.0
