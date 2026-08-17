"""Gates on the automatic stitch-anchor solver.

Two kinds of test, and the distinction matters for how much they prove.

  * **Ground truth.** A known shear is applied to a synthetic panorama and the
    solver has to recover it. This is the only place the true answer is known,
    so it is the only place an accuracy number means anything. Tolerances are
    stated in the assertions, not hidden in a helper.

  * **Refusal.** Every way the evidence can be too thin, each checked to raise
    rather than return a plausible curve. This is the half that matters most:
    the seam sits mid-field, mid-field is featureless grass, and a fabricated
    calibration is worse than none because it gets applied to the camera and
    then trusted.

The fixture blends two shifted views across a 256-px window exactly as
VIDEOPROC 2 does, so the solver sees what it will see in the field: evidence
only on the shoulders, with the window itself already irreversibly mixed.

Real frames are not committed (a Duo 3 still is 730 KB and is calibration data
for one physical unit). What the live camera *did* show while this was written
is recorded in `docs/STITCH_CALIBRATION.md` and in the branch commit message:
an indoor 0.3 m scene with one lens occluded, on which the solver refuses --
which is the correct behaviour, not a gap in the test suite.
"""

from __future__ import annotations

import json
import sys
from functools import cache
from pathlib import Path

import cv2
import numpy as np
import pytest

VPE_DIR = Path(__file__).resolve().parents[1] / "reolink-firmware-patching" / "vpe"
sys.path.insert(0, str(VPE_DIR))

from seam_metric import seam_continuity_residual  # noqa: E402
from stitch_solver import (  # noqa: E402
    MAX_DX_STDERR_PX,
    Observation,
    SolverRefused,
    _observation_variance,
    apply_anchors_downstream,
    holdout_split,
    main,
    solve,
    solve_from_frames,
)

W, H = 1200, 800
SEAM, BLEND, SHOULDER = 600, 256, 256
KW = {"seam_x": SEAM, "blend_w": BLEND, "shoulder_w": SHOULDER}

# Six straight edges crossing the seam. Slopes fan from -0.25 to +0.25 and the
# seam-crossing rows are spaced 96 px apart, which puts every pairwise crossing
# ~1000 px from the seam -- well outside the +/-384 px the detector looks at, so
# no two structures can be confused for one another inside the analysis band.
# Alternating polarity keeps the cumulative brightness inside 8 bits; six
# same-signed steps would saturate the lower half of the frame and the bottom
# structures would simply vanish.
FANNED = tuple(
    (-0.25 + 0.10 * k, 220.0 + 96.0 * k, 50.0 if k % 2 == 0 else -50.0)
    for k in range(6)
)


def _render(
    lines: tuple, *, e_of_y=None, dy: float = 0.0, seed: int = 0, noise: float = 1.5
) -> np.ndarray:
    yy = np.arange(H, dtype=np.float32)[:, None]
    xx = np.arange(W, dtype=np.float32)[None, :]
    if e_of_y is None:
        e = np.zeros((H, 1), dtype=np.float32)
    else:
        e = e_of_y(np.arange(H, dtype=np.float32))[:, None].astype(np.float32)
    xs = xx - e
    img = np.full((H, W), 100.0, dtype=np.float32)
    for m, y0, amp in lines:
        d = (yy - dy) - (m * (xs - SEAM) + y0)
        img += amp * 0.5 * (1.0 + np.tanh(d / 1.2))
    rng = np.random.default_rng(seed)
    return img + rng.normal(0.0, noise, img.shape).astype(np.float32)


def _fuse(lines: tuple, e_of_y, dy: float, seed: int) -> np.ndarray:
    """One scene seen by two sensors, butt-joined with a 256-px linear blend.

    `e_of_y(y)` is the *misregistration*: how far right the right sensor's view
    of the world sits at row y. The correction the solver must emit is its
    negative, per the sign convention of section 4.4.
    """
    left = _render(lines, seed=seed)
    right = _render(lines, e_of_y=e_of_y, dy=dy, seed=seed + 1000)
    out = left.copy()
    lo, hi = SEAM - BLEND // 2, SEAM + BLEND // 2
    alpha = np.linspace(0.0, 1.0, hi - lo, dtype=np.float32)[None, :]
    out[:, hi:] = right[:, hi:]
    out[:, lo:hi] = left[:, lo:hi] * (1 - alpha) + right[:, lo:hi] * alpha
    return np.clip(out, 0, 255).astype(np.uint8)


def _dx_truth(a: float, b: float):
    """The correction: dx(y) = a + b*(y - H/2)."""
    return lambda y: a + b * (np.asarray(y, dtype=float) - H / 2.0)


@cache
def frames(
    a: float = -4.0,
    b: float = 0.006,
    quad: float = 0.0,
    dy: float = 0.0,
    n: int = 3,
    lines: tuple = FANNED,
) -> tuple[np.ndarray, ...]:
    """`n` frames of the same static scene, differing only in detector noise.

    Static scene, independent noise, which is what accumulating across a game
    actually gives you: the field lines do not move, so extra frames buy
    averaging and nothing else. They do not improve the design matrix, and the
    solver must not pretend they do.
    """

    def e_of_y(y):
        y = np.asarray(y, dtype=float)
        return -(a + b * (y - H / 2.0) + quad * (y - H / 2.0) ** 2)

    return tuple(_fuse(lines, e_of_y, dy, seed=s) for s in range(1, n + 1))


def _solve(fs, **kw):
    return solve_from_frames(list(fs), **KW, **kw)


# -- ground truth: recover a shear we put there ------------------------------


def test_recovers_a_known_shear_to_a_third_of_a_pixel():
    a, b = -4.0, 0.006
    truth = _dx_truth(a, b)
    result = _solve(frames(a=a, b=b))

    errs = [abs(dx - float(truth(y))) for y, dx in result.anchors]
    assert max(errs) < 0.35, (
        f"recovered {result.anchors} against truth "
        f"{[(y, round(float(truth(y)), 2)) for y, _ in result.anchors]}"
    )
    fit = result.metadata["fit"]
    assert fit["a_px"] == pytest.approx(a, abs=0.3)
    assert fit["b_px_per_row"] == pytest.approx(b, abs=0.0008)


def test_the_reported_uncertainty_actually_bounds_the_error():
    """The stderr is the refusal gate, so it has to mean something.

    Checked on two fixtures with very different conditioning -- a well-fanned
    one and a barely-fanned one -- because an error bar that is only right when
    the data is good is not an error bar.
    """
    shallow = tuple(
        (-0.02 if k % 2 else 0.02, 220.0 + 96.0 * k, 50.0 if k % 2 == 0 else -50.0)
        for k in range(6)
    )
    for lines, tol_mult in ((FANNED, 3.0), (shallow, 3.0)):
        truth = _dx_truth(-4.0, 0.006)
        result = _solve(frames(lines=lines))
        for (y, dx), se in zip(
            result.anchors, result.metadata["anchor_stderr_px"], strict=True
        ):
            err = abs(dx - float(truth(y)))
            assert err <= tol_mult * se + 0.25, (
                f"row {y}: error {err:.2f} px against a claimed +/-{se:.2f} px"
            )


def test_recovers_a_pure_translation_as_a_flat_curve():
    result = _solve(frames(a=-5.0, b=0.0))
    assert all(dx == pytest.approx(-5.0, abs=0.35) for _, dx in result.anchors)
    assert result.metadata["fit"]["b_px_per_row"] == pytest.approx(0.0, abs=0.0005)


def test_the_vertical_offset_cross_checks_the_roll_model():
    """A relative roll that shears dx must also produce a specific dy.

    Section 2: a roll theta displaces a point at (X, Y) by theta*(-Y, +X), so
    the same theta that gives `dx` its slope in y must give a constant vertical
    offset of `theta * X_seam`, with X_seam = source_width/4 from the half's
    optical centre. Injecting exactly that and confirming the solver finds it
    is a free consistency check on the physics, not just on the algebra -- and
    it is the check that fires when something other than a roll is present.
    """
    b = 0.006
    result = _solve(frames(a=-4.0, b=b, dy=b * (W / 4.0)))
    fit = result.metadata["fit"]
    assert fit["dy_px"] == pytest.approx(fit["dy_predicted_from_roll_px"], abs=0.4)
    assert not any("vertical offset" in f for f in result.metadata["findings"])


def test_a_dy_inconsistent_with_the_roll_is_reported_as_a_finding():
    result = _solve(frames(a=-4.0, b=0.006, dy=6.0))
    assert any("vertical offset" in f for f in result.metadata["findings"])


# -- the sign, end to end through the shipped corrector ----------------------


def _pooled_p90(fs) -> float:
    ps = [seam_continuity_residual(f, **KW) for f in fs]
    return float(np.median([r.p90 for r in ps if r.n]))


def test_the_emitted_sign_is_the_one_the_shipped_corrector_consumes():
    """The one test that makes a sign error impossible to ship.

    A sign error here does not fail loudly: it doubles the seam break instead
    of closing it and presents as "the tool does not work". So the recovered
    anchors are fed to the real `stitch_remap` applier -- not a local copy --
    and the metric has to improve; the same anchors negated have to make it
    worse. Both directions are asserted, because "it got better" alone is also
    satisfied by a correction of half the right size.
    """
    fs = list(frames(a=-4.0, b=0.006))
    result = _solve(fs)
    before = _pooled_p90(fs)

    def applied(anchors):
        return [
            apply_anchors_downstream(
                f, anchors, source_width=W, source_height=H, seam_x=SEAM
            )
            for f in fs
        ]

    after = _pooled_p90(applied(result.anchors))
    flipped = _pooled_p90(applied([(y, -dx) for y, dx in result.anchors]))
    assert after < before * 0.5, f"SCR p90 {before:.2f} -> {after:.2f} px"
    assert flipped > before, f"negating the anchors must hurt: {flipped:.2f} px"


def test_anchors_are_in_the_pixel_units_of_the_frames_measured():
    """Solve on a half-size frame; the anchors must be half-size too.

    `build_dx_lookup` rescales anchors by `actual/source`, so getting this
    wrong scales every correction downstream. Solving the same scene at half
    resolution must give half the dx, at half the row.
    """
    full = frames(a=-4.0, b=0.006)
    small = [
        cv2.resize(f, (W // 2, H // 2), interpolation=cv2.INTER_AREA) for f in full
    ]
    result = solve_from_frames(
        small, seam_x=SEAM // 2, blend_w=BLEND // 2, shoulder_w=SHOULDER // 2
    )
    assert result.metadata["source_width"] == W // 2
    assert result.metadata["source_height"] == H // 2

    truth = _dx_truth(-4.0, 0.006)
    for y, dx in result.anchors:
        # y and dx are both in half-size units; scale both back up.
        assert dx * 2.0 == pytest.approx(float(truth(y * 2.0)), abs=0.6)


# -- refusal -----------------------------------------------------------------


def test_refuses_a_seam_with_no_structure_at_all():
    blank = [np.full((H, W), 120, dtype=np.uint8)] * 2
    with pytest.raises(SolverRefused) as e:
        _solve(blank)
    assert "0 accepted structures" in str(e.value)
    assert e.value.report["coverage"]["n"] == 0


def test_refuses_too_few_structures():
    one = ((0.22, 400.0, 50.0),)
    with pytest.raises(SolverRefused) as e:
        _solve(frames(lines=one))
    assert any("accepted structures" in r for r in e.value.reasons)


def test_refuses_thin_row_coverage_which_is_the_case_the_live_frame_hits():
    """Structures all bunched at one height.

    This is exactly what `seam_metric` reported on the live indoor frame: a
    handful of observations in one of three row bands over a fifth of the
    height. A shear fitted to that is an extrapolation dressed as a
    measurement.
    """
    bunched = tuple(
        (-0.25 + 0.10 * k, 380.0 + 7.0 * k, 50.0 if k % 2 == 0 else -50.0)
        for k in range(6)
    )
    with pytest.raises(SolverRefused) as e:
        _solve(frames(lines=bunched))
    assert any("row band" in r for r in e.value.reasons)
    assert any("field band" in r for r in e.value.reasons)
    cov = e.value.report["coverage"]
    assert cov["row_bands_covered"] == 1
    assert sum(cov["row_band_counts"]) == cov["n"]


def test_refuses_when_every_structure_shares_one_slope():
    """dx enters each observation only as `m * dx`.

    With one slope the translate term is collinear with the vertical offset, so
    no number of observations makes dx observable -- which is why this refuses
    on geometry and not on count.
    """
    parallel = tuple(
        (0.22, 220.0 + 96.0 * k, 50.0 if k % 2 == 0 else -50.0) for k in range(6)
    )
    with pytest.raises(SolverRefused) as e:
        _solve(frames(lines=parallel))
    assert any("same slope" in r for r in e.value.reasons)


def test_refuses_when_the_slope_lever_is_too_weak_to_pin_dx():
    """Coverage passes, precision does not -- the gate doing independent work.

    Every count-and-coverage condition of 9.3 is satisfied here: 18
    observations, all three row bands, 70% of the height. Only the error bar
    says no. Without this gate the solver would emit a confident-looking curve
    that is wrong by more than the correction it is trying to make.
    """
    barely = tuple(
        (-0.011 if k % 2 else 0.011, 220.0 + 96.0 * k, 50.0 if k % 2 == 0 else -50.0)
        for k in range(6)
    )
    with pytest.raises(SolverRefused) as e:
        _solve(frames(lines=barely))
    assert any("uncertain to" in r for r in e.value.reasons)
    cov = e.value.report["coverage"]
    assert cov["n"] >= 8 and cov["row_bands_covered"] == 3
    assert cov["height_coverage"] > 0.60
    assert e.value.report["fit"]["max_anchor_stderr_px"] > MAX_DX_STDERR_PX


def test_refuses_a_physically_implausible_shear():
    with pytest.raises(SolverRefused) as e:
        _solve(frames(a=-80.0, b=0.0))
    assert any("64 px limit" in r for r in e.value.reasons)


def test_a_refusal_carries_the_measurement_report():
    """Section 10 step 3: emit the observations, not just a shrug.

    The operator's next move is Workflow B, and starting it from real rows and
    real residuals is the difference between a two-minute fix and a guess.
    """
    bunched = tuple(
        (-0.25 + 0.10 * k, 380.0 + 7.0 * k, 50.0 if k % 2 == 0 else -50.0)
        for k in range(6)
    )
    with pytest.raises(SolverRefused) as e:
        _solve(frames(lines=bunched))
    report = e.value.report
    assert len(report["observations"]) == report["coverage"]["n"]
    for o in report["observations"]:
        assert {"y", "slope", "residual_y", "span_px", "fit_rms", "frame"} <= set(o)
    json.dumps(report)  # must survive serialisation


def test_no_anchors_are_returned_alongside_a_refusal():
    """A refusal must not be recoverable into a calibration by a careless caller.

    `provisional_anchors` exists only on the late (post-fit) refusals and is
    deliberately named so that nothing mistakes it for `dx_anchors`.
    """
    with pytest.raises(SolverRefused) as e:
        _solve(frames(a=-80.0, b=0.0))
    assert "dx_anchors" not in e.value.report
    assert e.value.report["refused"] == e.value.reasons


# -- reporting ---------------------------------------------------------------


def test_reports_per_band_coverage_so_a_caller_can_see_why():
    result = _solve(frames())
    cov = result.metadata["coverage"]
    assert cov["row_bands_covered"] == 3
    assert len(cov["row_band_counts"]) == 3
    assert sum(cov["row_band_counts"]) == cov["n"]
    assert cov["n_frames"] == 3 and cov["n_frames_contributing"] == 3
    assert len(cov["observations_per_frame"]) == 3
    assert cov["height_coverage"] > 0.60


def test_flags_a_nonlinear_dx_as_a_finding_without_fitting_a_curve():
    """Section 5.2 / 10: the mesh's y-coarseness is lossless *if* dx is linear.

    A rigid relative roll cannot produce curvature, so curvature is a finding
    about the camera. The solver reports it and still emits the straight line;
    fitting a curve to it is a decision for a human with the report in hand.
    """
    result = _solve(frames(a=-4.0, b=0.006, quad=3.0e-5))
    assert result.metadata["fit"]["quadratic_term_sigma"] > 3.0
    assert any("not be linear in y" in f for f in result.metadata["findings"])
    assert len(result.anchors) == 5
    diffs = np.diff([dx for _, dx in result.anchors])
    assert np.allclose(diffs, diffs[0], atol=0.3), "emitted curve must stay straight"


def test_metadata_is_serialisable_and_states_the_sign_convention():
    result = _solve(frames())
    json.dumps({"dx_anchors": result.anchors, **result.metadata})
    sense = result.metadata["sense"]
    assert sense["dx_means"].startswith("px the RIGHT half must move right")
    assert sense["downstream_moves"] == "right_half"
    assert result.metadata["quantum_px"] == 0.25
    assert all(abs(dx * 4 - round(dx * 4)) < 1e-9 for _, dx in result.anchors), (
        "anchors must be whole quarter-pixels, the mesh's own resolution"
    )


def test_anchor_rows_are_strictly_increasing_and_span_the_frame():
    result = _solve(frames())
    rows = [y for y, _ in result.anchors]
    assert rows == sorted(set(rows))
    assert rows[0] == 0 and rows[-1] == H - 1


# -- the pieces, unit-tested -------------------------------------------------


def test_short_chains_are_penalised_for_extrapolating_further():
    """Weight comes from extrapolation variance, and it is steeply non-linear.

    A chain seeded at the blend edge has its centroid `blend_w/2 + L/2` from the
    seam, so halving its length both halves the sample count and roughly
    quadruples the leverage term. Treating a 96-px chain like a 384-px one is
    how a handful of scraps outvote the good data.
    """
    long_v = _observation_variance(384, 0.5, BLEND)
    short_v = _observation_variance(96, 0.5, BLEND)
    assert short_v > 10 * long_v, f"{short_v:.3f} vs {long_v:.3f}"
    assert _observation_variance(384, 1.0, BLEND) == pytest.approx(4 * long_v)
    # A zero-rms fit must not claim infinite weight.
    assert np.isfinite(_observation_variance(384, 0.0, BLEND))
    assert _observation_variance(384, 0.0, BLEND) > 0


def _synthetic_obs(a: float, b: float, n_each: int = 6) -> list[Observation]:
    """Observations straight from the model, with the noise the weights assume."""
    var = _observation_variance(384, 0.3, BLEND)
    rng = np.random.default_rng(5)
    obs = []
    for y in (150.0, 400.0, 650.0):
        for k in range(n_each):
            m = -0.25 + 0.5 * k / max(n_each - 1, 1)
            r = m * (a + b * (y - H / 2.0)) + rng.normal(0.0, np.sqrt(var))
            obs.append(
                Observation(
                    y=y,
                    slope=m,
                    residual_y=r,
                    span_px=384,
                    fit_rms=0.3,
                    variance=var,
                )
            )
    return obs


def test_the_robust_loss_survives_a_minority_of_wild_observations():
    """Moving objects straddle depths and cannot be excluded by detection alone.

    Section 10 rejects players by policy, but a policy is not a filter -- some
    get through, and each one contributes a residual that has nothing to do
    with the seam. Three corrupted observations in twelve must not move the
    answer.
    """
    a, b = -4.0, 0.006
    clean = _synthetic_obs(a, b)
    dirty = list(clean)
    for i, bad in zip(
        (1, len(clean) // 2, len(clean) - 2), (14.0, -11.0, 9.0), strict=True
    ):
        dirty[i] = Observation(**{**vars(clean[i]), "residual_y": bad})

    ref = solve(clean, source_width=W, source_height=H, seam_x=SEAM, blend_w=BLEND)
    got = solve(dirty, source_width=W, source_height=H, seam_x=SEAM, blend_w=BLEND)
    for (_, want), (_, have) in zip(ref.anchors, got.anchors, strict=True):
        assert have == pytest.approx(want, abs=0.5)
    assert got.metadata["fit"]["n_downweighted"] >= 3
    # The diagnostic that says "outliers, not a bad model": a reduced chi-square
    # wrecked by three wild points while the robust dispersion stays near 1.
    assert got.metadata["fit"]["chi2_per_dof"] > 10
    assert got.metadata["fit"]["robust_scale"] < 2.0


def test_holdout_split_is_deterministic_and_spans_the_footage():
    solve_idx, hold = holdout_split(12, every=4)
    assert hold == [0, 4, 8]
    assert set(solve_idx) & set(hold) == set()
    assert sorted(solve_idx + hold) == list(range(12))


def test_seam_metric_implied_dx_is_the_opposite_sense_to_the_artifact():
    """A landmine pinned in code rather than left in prose.

    `seam_metric` models `r_y = dy - m*dx` and this solver models
    `r_y = dy + m*dx`, because the first is measuring how far the right half
    has drifted and the second is emitting how far it must be moved back. Same
    magnitude, opposite sign, both correct for what they mean. Nothing in
    either module's signature says so, so it is asserted here.
    """
    result = _solve(frames(a=-4.0, b=0.0))
    scr = seam_continuity_residual(frames(a=-4.0, b=0.0)[0], **KW)
    assert scr.slope_spread > 0.02, "the fixture must give implied_dx meaning"
    mean_dx = float(np.mean([dx for _, dx in result.anchors]))
    assert scr.implied_dx == pytest.approx(-mean_dx, abs=0.6)


# -- CLI ---------------------------------------------------------------------


def test_cli_exits_non_zero_when_it_refuses(tmp_path, capsys):
    """A guard that warns and continues is not a guard.

    In an automated chain the only thing downstream reads is the exit code.
    """
    p = tmp_path / "blank.png"
    cv2.imwrite(str(p), np.full((H, W, 3), 120, dtype=np.uint8))
    assert main(["stitch_solver.py", str(p), f"--seam-x={SEAM}"]) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "accepted structures" in out


def test_cli_refusal_json_carries_the_reasons(tmp_path, capsys):
    p = tmp_path / "blank.png"
    cv2.imwrite(str(p), np.full((H, W, 3), 120, dtype=np.uint8))
    assert main(["stitch_solver.py", str(p), f"--seam-x={SEAM}", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["refused"]
    assert payload["report"]["coverage"]["n"] == 0


def test_cli_reports_usage_without_frames(capsys):
    assert main(["stitch_solver.py"]) == 2
