"""Derive `dx_anchors` from footage -- the automated half of the seam calibration.

This is Workflow A of `docs/STITCH_CALIBRATION.md` 10. It consumes panorama
frames, accumulates seam observations across them with `seam_metric`, and emits
the same artifact a human produces by dragging handles in Workflow B: a short
list of `[y, dx]` anchors in source-pixel units, plus a metadata dict saying
what was measured, how well, and what was refused.

WHAT AN OBSERVATION ACTUALLY IS -- and a correction to the design.

10 says "weighted robust regression of `dx` on `y`". That is not available,
because a single seam observation is not a `dx`. `seam_metric` can only fit
structures that span the whole blend window in x, which means near-horizontal
ones, and a near-horizontal line of slope `m` displaced horizontally by `dx`
moves *vertically* at the seam:

    r_y  =  dy  +  m * dx(y)

`dy` is the constant vertical mismatch (a relative lens roll produces one, 2).
So a flat line sees nothing of `dx`, one line under-determines it, and `dx` is
recoverable only jointly, from structures of *differing slope*. There is no
per-observation `dx` to regress on `y`.

The solver therefore fits one joint model over every accumulated observation.
Taking the physical model of 2 -- a relative lens roll gives `dx` exactly
linear in y -- as `dx(y) = a + b*(y - y_ref)`:

    r_y  =  dy  +  a*m  +  b*m*(y - y_ref)

three unknowns, design row `[1, m, m*(y - y_ref)]`. Identifiability follows
from the design matrix, not from the observation count:

  * spread in `m` separates `dy` from `a`. Lines that all share one slope leave
    the two collinear and `dx` is simply not observable, at any n.
  * spread in `m*(y - y_ref)` separates `a` from `b`. Observations at a single
    height leave the shear unobservable, again at any n.

which is why the refusal gate below is a *standard error on dx*, not a count.
Thirteen observations in one row band produce an enormous error bar, and that
is the honest reason to refuse them.

WHAT THE FIT CAN AND CANNOT SEPARATE.

  * It separates translate (`a`) from shear (`b`), given the spread above.
  * It does NOT separate a rigid relative *rotation* of one half from a
    linear-in-y *shear* of it: every observation is taken at one column, and at
    one column those produce identical displacements (2). So the artifact
    stores the curve, never `(tx, ty, rot, shear)`. `roll_theta_rad` below is
    reported as the roll interpretation of `b`, not as a separately measured
    quantity.
  * It cannot attribute the misregistration to a half. `dx` is relative, which
    is all either correction surface needs.
  * `dy` is measured but not emitted: the downstream corrector is horizontal
    only (`stitch_remap.py`), and 4.2 forbids inventing a field one consumer
    silently drops. It appears in the metadata as a finding and as a
    cross-check on the roll model.

WHAT THE BLEND WINDOW COSTS.

Every observation comes from the shoulders, because the 128-px-per-side blend
window has already mixed the two sensors irreversibly. So each shoulder fit is
*extrapolated* at least `blend_w/2` px to reach the seam, and the variance of a
linear extrapolation grows with the square of that distance. `_observation_variance`
computes it rather than assuming it, and it is the dominant term for short
chains: a 384-px chain starting at the blend edge extrapolates from a centroid
320 px away, inflating its variance by ~9x over the in-span case.

That bounds precision, not correctness. What is irrecoverable is the ghosting
already baked into the fused pixels -- a correction measured this way can
register the shoulders, but no post-fusion operation un-mixes the window
(that is what SSR measures, and why the camera-side mesh exists).

SIGN. `dx(y)` is the pixels the RIGHT half must move to the RIGHT at row y to
register with the left, matching `stitch_remap.build_dx_lookup` and 4.4.
Note that `seam_metric.ScrResult.implied_dx` is reported in the OPPOSITE sense
-- it models `r_y = dy - m*dx`, so its `dx` is the misregistration the right
half currently carries, not the correction for it. This module does not use it;
`tests/test_stitch_solver.py` pins the relationship so the trap stays visible.

CLI:
    python stitch_solver.py <frame.jpg> [more.jpg ...] [--seam-x=N] [--json]
                            [--validate]
Exit 0 on a fit, 1 on a refusal (with the measurement report), 2 on usage.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lut2d import FRAC_SCALE, MAX_ABS_DX_PX  # noqa: E402
from seam_metric import (  # noqa: E402
    BLEND_W,
    SEAM_X,
    SHOULDER_W,
    default_band,
    measure,
    seam_continuity_residual,
)

TOOL_VERSION = "stitch_anchor_solver/1"

# Anchor count. 4.2's example artifact carries five, 10 says "sample that
# line at 5 rows", and five is already more than a straight line needs -- the
# redundancy is there so a later curved fit can reuse the same slots.
N_ANCHORS = 5

# Coverage floors, mirroring 9.3 so a solve that passes here cannot be
# rejected by `seam_metric.check_acceptance` on coverage grounds afterwards.
MIN_OBSERVATIONS = 8
MIN_ROW_BANDS = 3
MIN_HEIGHT_COVERAGE = 0.60

# `dx` enters every observation multiplied by the structure's slope, so with no
# slope spread the translate term is collinear with the vertical offset. This
# threshold only catches the degenerate case; the standard-error gate below
# does the quantitative work. Matches the value `seam_metric` uses before it
# will report `implied_dx` at all.
MIN_SLOPE_SPREAD = 0.02

# The gate that matters. 10 stops iterating at `SCR p90 < 1.0 px`; a fit whose
# own uncertainty exceeds the target it is aiming at has not measured anything.
MAX_DX_STDERR_PX = 1.0

# Sub-pixel edge localisation floor. `seam_metric._edge_positions` refines each
# peak with a parabola through three samples, which is good to well under a
# pixel but not to zero -- and a fit_rms of exactly 0 (synthetic input) would
# otherwise claim infinite weight.
MIN_FIT_SIGMA_PX = 0.05


class SolverRefused(Exception):
    """The evidence does not support a calibration.

    Carries `.reasons` (every failed condition, not just the first) and
    `.report` (the 10 measurement report: coverage plus every observation
    found, so the operator can start Workflow B from real numbers).
    """

    def __init__(self, reasons: list[str], report: dict):
        super().__init__("; ".join(reasons))
        self.reasons = reasons
        self.report = report


@dataclass
class Observation:
    """One structure crossing the seam, in one frame."""

    y: float
    slope: float
    residual_y: float
    span_px: int
    fit_rms: float
    variance: float
    frame: int = 0


@dataclass
class SolveResult:
    anchors: list[tuple[int, float]] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


# -- observations -------------------------------------------------------------


def _observation_variance(span_px: float, fit_rms: float, blend_w: int) -> float:
    """Variance of one `r_y`, dominated by extrapolation across the blend window.

    For an OLS line fitted to N points uniformly spanning length L, evaluated a
    distance D from the span's centroid, `Var = sigma^2 * (1/N + D^2/Sxx)` with
    `Sxx = N*L^2/12`. `seam_metric._chains` seeds at the blend edge and walks
    outward one column at a time, so N ~ L and the centroid sits `L/2` beyond
    the blend edge: `D = blend_w/2 + L/2`. Both shoulders contribute, and
    `r_y` is their difference, so the variances add.

    This is why a short chain is nearly worthless even when it fits perfectly:
    halving L doubles `1/L` and quadruples `D^2/L^2`.
    """
    sigma = max(float(fit_rms), MIN_FIT_SIGMA_PX)
    length = max(float(span_px), 2.0)
    d = blend_w / 2.0 + length / 2.0
    per_side = (sigma * sigma / length) * (1.0 + 12.0 * d * d / (length * length))
    return 2.0 * per_side


def collect_observations(
    frames: list[np.ndarray],
    *,
    seam_x: int = SEAM_X,
    blend_w: int = BLEND_W,
    shoulder_w: int = SHOULDER_W,
    band: tuple[int, int] | None = None,
    **scr_kwargs,
) -> tuple[list[Observation], list[int], tuple[int, int]]:
    """Run `seam_continuity_residual` over every frame and pool the results.

    Pooling across frames rather than within one is the whole point (10): a
    90-minute game is ~108,000 frames and no single frame needs to be good. It
    also means per-frame coverage is irrelevant -- only the pooled design
    matters -- and it lets the robust loss see a moving object as the outlier
    it is, because a player contributes an observation that disagrees with the
    static structure around it.
    """
    if not frames:
        raise ValueError("no frames")
    shapes = {f.shape[:2] for f in frames}
    if len(shapes) != 1:
        raise ValueError(f"frames must share one geometry, got {sorted(shapes)}")
    height = frames[0].shape[0]
    band = band or default_band(height)

    obs: list[Observation] = []
    per_frame: list[int] = []
    for i, frame in enumerate(frames):
        scr = seam_continuity_residual(
            frame,
            seam_x=seam_x,
            blend_w=blend_w,
            shoulder_w=shoulder_w,
            band=band,
            **scr_kwargs,
        )
        per_frame.append(scr.n)
        for o in scr.observations:
            obs.append(
                Observation(
                    y=0.5 * (o.y_left + o.y_right),
                    slope=o.slope,
                    residual_y=o.residual_y,
                    span_px=o.span_px,
                    fit_rms=o.fit_rms,
                    variance=_observation_variance(o.span_px, o.fit_rms, blend_w),
                    frame=i,
                )
            )
    return obs, per_frame, band


def coverage_of(
    observations: list[Observation],
    band: tuple[int, int],
    n_frames: int,
    per_frame: list[int],
) -> dict:
    """Per-band coverage, so a caller can see *why* a fit was taken or refused.

    Row bands and height coverage use the same definitions as
    `seam_metric.ScrResult`, so the numbers here and the ones
    `check_acceptance` gates on are the same numbers.
    """
    y0, y1 = band
    height = y1 - y0
    ys = np.array([o.y for o in observations], dtype=float)
    slopes = np.array([o.slope for o in observations], dtype=float)
    counts = [0] * MIN_ROW_BANDS
    for y in ys:
        idx = int((y - y0) / height * MIN_ROW_BANDS)
        counts[min(max(idx, 0), MIN_ROW_BANDS - 1)] += 1
    return {
        "n": len(observations),
        "n_frames": n_frames,
        "n_frames_contributing": sum(1 for c in per_frame if c),
        "observations_per_frame": per_frame,
        "band": [int(y0), int(y1)],
        "row_band_counts": counts,
        "row_bands_covered": sum(1 for c in counts if c),
        "row_bands": MIN_ROW_BANDS,
        "height_coverage": float((ys.max() - ys.min()) / height)
        if len(ys) > 1
        else 0.0,
        "y_min": float(ys.min()) if len(ys) else None,
        "y_max": float(ys.max()) if len(ys) else None,
        "slope_min": float(slopes.min()) if len(ys) else None,
        "slope_max": float(slopes.max()) if len(ys) else None,
        "slope_spread": float(slopes.max() - slopes.min()) if len(ys) > 1 else 0.0,
        "median_span_px": float(np.median([o.span_px for o in observations]))
        if observations
        else None,
    }


# -- the joint robust fit -----------------------------------------------------


def _design(observations: list[Observation], y_ref: float, y_half: float) -> np.ndarray:
    m = np.array([o.slope for o in observations], dtype=float)
    t = np.array([(o.y - y_ref) / y_half for o in observations], dtype=float)
    return np.column_stack([np.ones_like(m), m, m * t])


def _irls(
    a: np.ndarray, r: np.ndarray, prior_w: np.ndarray, iters: int = 20
) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """Weighted least squares with a Huber redescent on top of the prior weights.

    The prior weights are true inverse variances from `_observation_variance`,
    so the robust scale is estimated in *standardised* units and floored at 1.0:
    if the residuals already sit inside the noise the model predicts, nothing is
    down-weighted. Without that floor a synthetic fixture (or a genuinely clean
    game) collapses the scale to ~0 and the loss starts rejecting good data.

    The returned scale is the dispersion estimate the covariance uses. It has to
    be the *robust* one: a plain reduced chi-square counts each rejected
    observation's full residual, so three players crossing the seam would widen
    the error bar until the solver refused data it had correctly cleaned. A
    median-based scale ignores them, which is the entire point of rejecting
    them, while still growing when the *bulk* of the data misfits the model.
    """
    sigma = 1.0 / np.sqrt(prior_w)
    w = prior_w.copy()
    beta = np.zeros(a.shape[1])
    resid = r.copy()
    scale = 1.0
    for _ in range(iters):
        aw = a * w[:, None]
        beta, *_ = np.linalg.lstsq(a.T @ aw, aw.T @ r, rcond=None)
        resid = r - a @ beta
        z = resid / sigma
        scale = max(1.4826 * float(np.median(np.abs(z))), 1.0)
        huber = np.clip(1.345 * scale / np.maximum(np.abs(z), 1e-9), 0.0, 1.0)
        w = prior_w * huber
    return beta, resid, w, scale


def solve(
    observations: list[Observation],
    *,
    source_width: int,
    source_height: int,
    seam_x: int = SEAM_X,
    blend_w: int = BLEND_W,
    band: tuple[int, int] | None = None,
    n_frames: int = 1,
    per_frame: list[int] | None = None,
    frame_labels: list[str] | None = None,
    n_anchors: int = N_ANCHORS,
    min_observations: int = MIN_OBSERVATIONS,
    min_row_bands: int = MIN_ROW_BANDS,
    min_height_coverage: float = MIN_HEIGHT_COVERAGE,
    min_slope_spread: float = MIN_SLOPE_SPREAD,
    max_dx_stderr: float = MAX_DX_STDERR_PX,
    max_abs_dx: float = MAX_ABS_DX_PX,
) -> SolveResult:
    """Fit `dx(y) = a + b*(y - y_ref)` jointly, or refuse and say what was missing.

    Raises `SolverRefused` -- never returns a curve it cannot support. A
    warning printed into a log no chain reads is not a guard, and a fabricated
    calibration is worse than none because it gets applied to the camera and
    then trusted.
    """
    band = band or default_band(source_height)
    per_frame = per_frame if per_frame is not None else [len(observations)]
    cov_info = (
        coverage_of(observations, band, n_frames, per_frame)
        if observations
        else {
            "n": 0,
            "n_frames": n_frames,
            "n_frames_contributing": 0,
            "observations_per_frame": per_frame,
            "band": [int(band[0]), int(band[1])],
            "row_band_counts": [0] * MIN_ROW_BANDS,
            "row_bands_covered": 0,
            "row_bands": MIN_ROW_BANDS,
            "height_coverage": 0.0,
            "y_min": None,
            "y_max": None,
            "slope_min": None,
            "slope_max": None,
            "slope_spread": 0.0,
            "median_span_px": None,
        }
    )

    base = {
        "solver": TOOL_VERSION,
        "source_width": int(source_width),
        "source_height": int(source_height),
        "seam_x": int(seam_x),
        "blend_window": [seam_x - blend_w // 2, seam_x + blend_w // 2],
        "coverage": cov_info,
        "provenance": {
            "workflow": "automated",
            "frames": frame_labels or [],
            "created_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tool_version": TOOL_VERSION,
        },
    }

    reasons: list[str] = []
    if cov_info["n"] < min_observations:
        reasons.append(
            f"only {cov_info['n']} accepted structures across {n_frames} frame(s) "
            f"(need {min_observations})"
        )
    if cov_info["row_bands_covered"] < min_row_bands:
        reasons.append(
            f"structures span {cov_info['row_bands_covered']} of {min_row_bands} row "
            f"bands (counts {cov_info['row_band_counts']})"
        )
    if cov_info["height_coverage"] < min_height_coverage:
        reasons.append(
            f"structures cover {cov_info['height_coverage'] * 100:.0f}% of the field "
            f"band, need {min_height_coverage * 100:.0f}%"
        )
    if cov_info["slope_spread"] <= min_slope_spread:
        reasons.append(
            f"every structure found has effectively the same slope (spread "
            f"{cov_info['slope_spread']:.3f}); dx enters each observation only "
            "through the slope, so it is not observable from this data at any n"
        )
    if reasons:
        raise SolverRefused(
            reasons, {**base, "observations": [asdict(o) for o in observations]}
        )

    y_ref = source_height / 2.0
    y_half = source_height / 2.0
    a = _design(observations, y_ref, y_half)
    r = np.array([o.residual_y for o in observations], dtype=float)
    prior_w = np.array([1.0 / o.variance for o in observations], dtype=float)

    normal = a.T @ (a * prior_w[:, None])
    cond = float(np.linalg.cond(normal))
    if not np.isfinite(cond) or cond > 1e12:
        raise SolverRefused(
            [
                f"the observation geometry is singular (condition number {cond:.3g}); "
                "the structures found do not constrain a translate and a shear "
                "independently"
            ],
            {**base, "observations": [asdict(o) for o in observations]},
        )

    beta, resid, w, scale = _irls(a, r, prior_w)
    dof = max(len(observations) - a.shape[1], 1)
    chi2 = float(np.sum(w * resid * resid))
    # Floored at 1.0: if the data fit *better* than the noise model predicts,
    # that is not licence to claim more precision than the model supports.
    s2 = max(scale * scale, 1.0)
    try:
        cov_beta = s2 * np.linalg.inv(a.T @ (a * w[:, None]))
    except np.linalg.LinAlgError as exc:  # pragma: no cover - cond gate catches it
        raise SolverRefused(
            [f"normal equations are not invertible: {exc}"],
            {**base, "observations": [asdict(o) for o in observations]},
        ) from exc

    dy_px, a_px, b_scaled = (float(v) for v in beta)
    b_px_per_row = b_scaled / y_half

    rows = np.unique(np.round(np.linspace(0, source_height - 1, n_anchors)).astype(int))
    dx_raw = [a_px + b_scaled * ((y - y_ref) / y_half) for y in rows]
    stderr = []
    for y in rows:
        c = np.array([0.0, 1.0, (y - y_ref) / y_half])
        stderr.append(float(np.sqrt(max(c @ cov_beta @ c, 0.0))))

    # Quantise to the mesh's own resolution. Q14.2 is a quarter pixel
    # (`lut2d.FRAC_BITS`); anything finer cannot reach the camera, and pretending
    # otherwise only makes the artifact look more precise than the surface it
    # drives.
    dx_q = [round(v * FRAC_SCALE) / FRAC_SCALE for v in dx_raw]
    anchors = [(int(y), float(d)) for y, d in zip(rows, dx_q, strict=True)]

    ss_res = float(np.sum(w * resid * resid))
    r_mean = float(np.sum(w * r) / np.sum(w))
    ss_tot = float(np.sum(w * (r - r_mean) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")

    findings: list[str] = []
    # 5.2 / 10: is `dx(y)` actually linear in y? A relative lens roll says it
    # must be, and if it is the mesh's 8.44-row control spacing loses nothing.
    # A significant quadratic term is a finding about the camera, not licence
    # for this solver to fit a curve -- that needs a human look first.
    quad_t = None
    if len(observations) >= 8:
        t = np.array([(o.y - y_ref) / y_half for o in observations])
        m = np.array([o.slope for o in observations])
        a4 = np.column_stack([a, m * t * t])
        try:
            xtwx4 = a4.T @ (a4 * w[:, None])
            beta4 = np.linalg.solve(xtwx4, (a4 * w[:, None]).T @ r)
            resid4 = r - a4 @ beta4
            z4 = resid4 * np.sqrt(prior_w)
            s2_4 = max((1.4826 * float(np.median(np.abs(z4)))) ** 2, 1.0)
            se4 = float(np.sqrt(s2_4 * np.linalg.inv(xtwx4)[3, 3]))
            quad_t = float(beta4[3]) / se4 if se4 > 0 else None
        except np.linalg.LinAlgError:
            quad_t = None
    if quad_t is not None and abs(quad_t) > 3.0:
        findings.append(
            f"dx(y) may not be linear in y: a quadratic term is {abs(quad_t):.1f} sigma "
            "from zero. A rigid relative roll cannot do that (2), so this is a "
            "finding about the camera and needs explaining before it ships. The "
            "emitted anchors are still the straight-line fit."
        )

    # The roll cross-check, free from the same fit. 2: a relative roll theta
    # displaces a point at (X, Y) by theta*(-Y, +X), so at the seam column
    # X = source_width/4 from the half's optical centre the same theta that
    # produces the shear must also produce a constant `dy = b * X`.
    x_seam_from_centre = source_width / 4.0
    dy_predicted = b_px_per_row * x_seam_from_centre
    dy_se = float(np.sqrt(max(cov_beta[0, 0], 0.0)))
    if dy_se > 0 and abs(dy_px - dy_predicted) > 3.0 * dy_se + 1.0:
        findings.append(
            f"the measured vertical offset ({dy_px:+.2f} px) disagrees with the "
            f"{dy_predicted:+.2f} px a pure relative roll of this shear would "
            "produce. Something other than roll contributes -- relative pitch, "
            "or parallax at a depth away from the stitch calibration (12.1). "
            "dx is still what the surfaces correct; dy is not emitted (4.2)."
        )

    max_stderr = max(stderr)
    worst_dx = max(abs(d) for d in dx_q)
    late: list[str] = []
    if max_stderr > max_dx_stderr:
        late.append(
            f"dx is uncertain to +/-{max_stderr:.2f} px at the anchor rows (limit "
            f"{max_dx_stderr:.2f} px). The structures found do not pin the curve: "
            f"{cov_info['n']} observations, slope spread "
            f"{cov_info['slope_spread']:.3f}, row-band counts "
            f"{cov_info['row_band_counts']}, condition number {cond:.3g}"
        )
    if worst_dx > max_abs_dx:
        late.append(
            f"fitted |dx| reaches {worst_dx:.1f} px, past the {max_abs_dx:.0f} px "
            "limit nothing physical needs -- treat this as a detector failure, "
            "not a badly aligned camera"
        )
    if any(anchors[i][0] <= anchors[i - 1][0] for i in range(1, len(anchors))):
        late.append("anchor rows are not strictly increasing")

    fit = {
        "model": "dx(y) = a + b*(y - y_ref)",
        "y_ref": y_ref,
        "a_px": a_px,
        "b_px_per_row": b_px_per_row,
        "roll_theta_rad": b_px_per_row,
        "dy_px": dy_px,
        "dy_predicted_from_roll_px": dy_predicted,
        "stderr_a_px": float(np.sqrt(max(cov_beta[1, 1], 0.0))),
        "stderr_b_px_per_row": float(np.sqrt(max(cov_beta[2, 2], 0.0))) / y_half,
        "stderr_dy_px": dy_se,
        "max_anchor_stderr_px": max_stderr,
        "condition_number": cond,
        # Both dispersions are reported. The robust one sets the error bars; a
        # chi2/dof much larger than robust_scale^2 is the signature of a few
        # wild observations rather than a bad model, and is worth seeing.
        "robust_scale": scale,
        "chi2_per_dof": chi2 / dof,
        "weighted_r2": float(r2),
        "residual_rms_px": float(np.sqrt(np.mean(resid**2))),
        "max_residual_px": float(np.max(np.abs(resid))),
        "n_downweighted": int(np.sum(w < 0.5 * prior_w)),
        "quadratic_term_sigma": quad_t,
    }
    meta = {
        **base,
        "sense": {
            "dx_means": (
                "px the RIGHT half must move right, at row y, to register with the left"
            ),
            "downstream_moves": "right_half",
            "camera_mesh_moves": "left_half_with_opposite_sense",
        },
        "units": (
            "anchors are [row, dx] in the source-pixel units of the frames measured "
            f"({source_width}x{source_height}); build_dx_lookup rescales them to the "
            "actual frame"
        ),
        "quantum_px": 1.0 / FRAC_SCALE,
        "fit": fit,
        "anchor_stderr_px": stderr,
        "downstream_projection": [[int(y), int(round(d))] for y, d in anchors],
        "findings": findings,
        "refused": None,
    }

    if late:
        raise SolverRefused(
            late,
            {
                **meta,
                "refused": late,
                "provisional_anchors": [[y, d] for y, d in anchors],
                "observations": [asdict(o) for o in observations],
            },
        )
    return SolveResult(anchors=anchors, metadata=meta)


def solve_from_frames(
    frames: list[np.ndarray],
    *,
    seam_x: int = SEAM_X,
    blend_w: int = BLEND_W,
    shoulder_w: int = SHOULDER_W,
    band: tuple[int, int] | None = None,
    frame_labels: list[str] | None = None,
    scr_kwargs: dict | None = None,
    **solve_kwargs,
) -> SolveResult:
    """`collect_observations` then `solve`, which is the whole of Workflow A."""
    obs, per_frame, band = collect_observations(
        frames,
        seam_x=seam_x,
        blend_w=blend_w,
        shoulder_w=shoulder_w,
        band=band,
        **(scr_kwargs or {}),
    )
    h, w = frames[0].shape[:2]
    return solve(
        obs,
        source_width=w,
        source_height=h,
        seam_x=seam_x,
        blend_w=blend_w,
        band=band,
        n_frames=len(frames),
        per_frame=per_frame,
        frame_labels=frame_labels,
        **solve_kwargs,
    )


# -- validating the downstream projection -------------------------------------


def holdout_split(n: int, every: int = 4) -> tuple[list[int], list[int]]:
    """Deterministic solve/hold-out split. 9.3 condition 4.

    Every `every`-th frame is held out. Deterministic rather than random so a
    re-run of the same footage reproduces the same numbers; taking a stride
    rather than a contiguous tail so the hold-out spans the same span of the
    game as the solve set, and not just its last minutes.
    """
    hold = list(range(0, n, every))
    solve_idx = [i for i in range(n) if i not in set(hold)]
    return solve_idx, hold


def apply_anchors_downstream(
    frame: np.ndarray,
    anchors: list[tuple[int, float]],
    *,
    source_width: int,
    source_height: int,
    seam_x: int,
) -> np.ndarray:
    """Apply the anchors with the *shipped* downstream corrector.

    Deliberately the real `video_grouper.utils.stitch_remap` and not a local
    reimplementation: this is the check that the emitted sign is the sign that
    module consumes, and a private copy of the shift would prove nothing. The
    import is lazy and path-relative for the same reason `stitch_apply.py`
    reaches into `runtime/` -- this directory is a standalone script tree, not
    a package, and must stay importable without the app installed.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from video_grouper.utils.stitch_remap import (
        StitchProfile,
        apply_shift_to_frame_rgb,
        build_dx_lookup,
    )

    profile = StitchProfile(
        source_width=source_width,
        source_height=source_height,
        seam_x=seam_x,
        dx_anchors=[(int(y), int(round(d))) for y, d in anchors],
    )
    h, w = frame.shape[:2]
    lookup = build_dx_lookup(profile, w, h)
    scaled_seam = int(round(seam_x * w / source_width))
    return apply_shift_to_frame_rgb(frame, lookup, scaled_seam)


def _pool(measures: list[dict]) -> dict:
    """Pool per-frame `seam_metric.measure` reports into one before/after dict.

    Percentiles are recomputed over the pooled observation count rather than
    averaged, because averaging p90s across frames is not a p90 of anything.
    """
    n = sum(m["scr"]["n"] for m in measures)
    weighted = [m for m in measures if m["scr"]["n"]]
    if weighted:
        p50 = float(np.median([m["scr"]["p50"] for m in weighted]))
        p90 = float(np.percentile([m["scr"]["p90"] for m in weighted], 90))
        mx = float(max(m["scr"]["max"] for m in weighted))
        bands = max(m["scr"]["row_bands_covered"] for m in weighted)
        covh = float(max(m["scr"]["height_coverage"] for m in weighted))
    else:
        p50 = p90 = mx = float("nan")
        bands, covh = 0, 0.0
    return {
        "scr": {
            "n": n,
            "p50": p50,
            "p90": p90,
            "max": mx,
            "row_bands_covered": bands,
            "height_coverage": covh,
        },
        "ssr": {
            "abs_ln_ssr": float(np.mean([m["ssr"]["abs_ln_ssr"] for m in measures])),
            "noise_floor": measures[0]["ssr"]["noise_floor"],
        },
    }


def validate_downstream(
    frames: list[np.ndarray],
    anchors: list[tuple[int, float]],
    *,
    source_width: int,
    source_height: int,
    seam_x: int = SEAM_X,
    blend_w: int = BLEND_W,
) -> dict:
    """Measure hold-out frames before and after the downstream projection.

    Returns the two dicts `seam_metric.check_acceptance` wants. Call it with
    `camera_side_owner=False`: a post-fusion shift genuinely improves SCR but
    cannot restore gradient energy the blend already destroyed, so demanding an
    SSR improvement from *this* surface would fail a correct correction. Only a
    camera-side owner is held to that.
    """
    before = [measure(f, seam_x=seam_x, blend_w=blend_w) for f in frames]
    after = [
        measure(
            apply_anchors_downstream(
                f,
                anchors,
                source_width=source_width,
                source_height=source_height,
                seam_x=seam_x,
            ),
            seam_x=seam_x,
            blend_w=blend_w,
        )
        for f in frames
    ]
    return {"before": _pool(before), "after": _pool(after)}


# -- CLI ----------------------------------------------------------------------


def _print_report(report: dict) -> None:
    cov = report["coverage"]
    print("  coverage")
    print(
        f"    {cov['n']} observation(s) from {cov['n_frames_contributing']}"
        f"/{cov['n_frames']} frame(s); per frame {cov['observations_per_frame']}"
    )
    print(
        f"    row bands {cov['row_bands_covered']}/{cov['row_bands']}"
        f"  counts {cov['row_band_counts']}"
        f"  height {cov['height_coverage'] * 100:.0f}%"
    )
    if cov["slope_min"] is not None:
        print(
            f"    slopes {cov['slope_min']:+.3f}..{cov['slope_max']:+.3f}"
            f"  (spread {cov['slope_spread']:.3f})"
            f"  median span {cov['median_span_px']:.0f} px"
        )
    obs = report.get("observations") or []
    if obs:
        print("  observations (row, slope, seam break px, span, fit rms, frame)")
        for o in sorted(obs, key=lambda d: d["y"]):
            print(
                f"    {o['y']:8.1f}  {o['slope']:+.4f}  {o['residual_y']:+7.2f}"
                f"  {o['span_px']:4d}  {o['fit_rms']:.2f}  f{o['frame']}"
            )


def main(argv: list[str]) -> int:
    paths = [a for a in argv[1:] if not a.startswith("--")]
    flags = {a for a in argv[1:] if a.startswith("--")}
    seam = SEAM_X
    for a in argv[1:]:
        if a.startswith("--seam-x="):
            seam = int(a.split("=", 1)[1])
    if not paths:
        print(__doc__)
        return 2

    frames, labels = [], []
    for p in paths:
        img = cv2.imread(p, cv2.IMREAD_COLOR)
        if img is None:
            print(f"cannot read {p}")
            return 2
        frames.append(img)
        labels.append(Path(p).name)

    try:
        result = solve_from_frames(frames, seam_x=seam, frame_labels=labels)
    except SolverRefused as exc:
        if "--json" in flags:
            print(json.dumps({"refused": exc.reasons, "report": exc.report}, indent=2))
            return 1
        print("REFUSED -- no calibration emitted")
        for why in exc.reasons:
            print(f"  * {why}")
        _print_report(exc.report)
        print(
            "\n  This is the measurement report (10, step 3): start Workflow B "
            "from these numbers, or place a deliberate target across the seam "
            "and re-shoot."
        )
        return 1

    out = {"dx_anchors": [[y, d] for y, d in result.anchors], **result.metadata}
    if "--validate" in flags and len(frames) >= 4:
        _, hold = holdout_split(len(frames))
        out["validation"] = validate_downstream(
            [frames[i] for i in hold],
            result.anchors,
            source_width=result.metadata["source_width"],
            source_height=result.metadata["source_height"],
            seam_x=seam,
        )
        out["validation"]["surface"] = "downstream"
        out["validation"]["frames_holdout"] = [labels[i] for i in hold]

    if "--json" in flags:
        print(json.dumps(out, indent=2))
        return 0

    fit = result.metadata["fit"]
    print(f"dx_anchors  {[[y, d] for y, d in result.anchors]}")
    print(
        f"  dx(y) = {fit['a_px']:+.2f} {fit['b_px_per_row']:+.5f}*(y - "
        f"{fit['y_ref']:.0f})  +/-{fit['max_anchor_stderr_px']:.2f} px"
    )
    print(
        f"  roll {fit['roll_theta_rad'] * 1000:+.3f} mrad   dy {fit['dy_px']:+.2f} px "
        f"(roll predicts {fit['dy_predicted_from_roll_px']:+.2f})"
    )
    print(
        f"  weighted r2 {fit['weighted_r2']:.3f}  residual rms "
        f"{fit['residual_rms_px']:.2f} px  chi2/dof {fit['chi2_per_dof']:.2f}"
        f"  downweighted {fit['n_downweighted']}"
    )
    _print_report({**result.metadata, "observations": []})
    for f in result.metadata["findings"]:
        print(f"  FINDING: {f}")
    if "validation" in out:
        v = out["validation"]
        print(
            f"  hold-out (downstream projection): SCR p90 "
            f"{v['before']['scr']['p90']:.2f} -> {v['after']['scr']['p90']:.2f} px"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
