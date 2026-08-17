"""Measure the Duo 3's stitch seam, so 'did the calibration help' has a number.

Two metrics, answering different questions. Both are computed over a field band
only, and both take their structure from *outside* the blend window -- the
pixels inside it are already a mixture of the two sensors, so nothing measured
there is evidence about registration.

  SCR  -- Seam Continuity Residual, in pixels. Near-horizontal structures are
          fitted independently on the left and right shoulders and extrapolated
          to the seam column; the mismatch is the residual. This is the
          magnitude estimator, and a solver can descend it.

  SSR  -- Seam Sharpness Ratio, dimensionless, reported as |ln SSR| and
          minimised at 0. Gradient energy inside the blend window relative to a
          background fitted on the shoulders. It is a *detector*, not an
          estimator: it saturates, so it separates "registered" from "not" but
          not 4 px from 32 px.

Why both. A downstream post-fusion shift genuinely improves SCR -- it really
does move the right shoulder relative to the left -- but it cannot restore
gradient energy that blending already destroyed. So:

    SCR improving, |ln SSR| flat   =>  the panorama moved.
    SCR and |ln SSR| both improving =>  registration improved before the blend.

That is the whole reason the camera-side mesh path exists rather than only the
downstream one, and this module is how that claim is checked rather than
asserted.

Two departures from the naive form, both established by measurement rather than
assumed (see docs/STITCH_CALIBRATION.md 9.2, and the corrections logged there):

  * SSR is computed on the ROW-DETRENDED image. The two sensors run independent
    AE and differ by tens of grey levels. The design calls that step something
    that "swamps the structural signal"; measured here, it does not -- the blend
    ramps it in over 256 px, so its per-column gradient is ~step/256 and on the
    live frame raw and detrended differ by 0.2% (3.633 vs 3.639). Detrending
    stays because it costs two lines and it *does* matter for a hard step, which
    is what a narrower blend or an unblended seam would produce.

  * SSR is reported as |ln SSR|, because it is not one-sided. Blending normally
    suppresses energy, driving SSR below 1 -- but the ratio is against a
    background fitted on the shoulders, so content that is intrinsically busier
    at the seam drives it above 1 (the live frame reads 3.64 at ~0.3 m subject
    distance). A "lower is better" reading would score that as better than
    perfect.

And the property that decides how SSR may be used: it SATURATES. Measured on
synthetic disparity, 0/1/2/3/4 px give 1.011/0.885/0.657/0.557/0.583 and
8..200 px are all flat at ~0.68. So it detects; it does not estimate. Anything
that needs a magnitude must use SCR.

CLI:
    python seam_metric.py <frame.jpg> [--seam-x 3840] [--json]
    python seam_metric.py --compare <before.jpg> <after.jpg>
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field

import cv2
import numpy as np

# Panorama geometry, measured from /proc/hdal/vprc/info on this unit.
SEAM_X = 3840
BLEND_W = 256  # VIDEOPROC 2 out port 1 runs 256x2160 = {blend_w*2, h}
SHOULDER_W = 384  # analysis band on each side, starting at the blend edge

# The photometrically visible transition is much narrower than the configured
# window (~56 px against 256), but gating on the configured window is the
# conservative choice: it is what the hardware actually mixes.


@dataclass
class SsrResult:
    ssr: float = 0.0
    abs_ln_ssr: float = 0.0
    ssr_raw_undetrended: float = 0.0
    background_rms_log: float = 0.0
    band: tuple[int, int] = (0, 0)

    @property
    def noise_floor(self) -> float:
        """|ln SSR| below this is indistinguishable from a perfect seam.

        Measured at ~0.10 on this camera by synthesising zero disparity into a
        clean region of a real frame; no threshold tighter than that means
        anything.
        """
        return 0.10


@dataclass
class ScrObservation:
    y_left: float
    y_right: float
    slope: float
    residual_y: float
    residual_perp: float
    span_px: int
    fit_rms: float


@dataclass
class ScrResult:
    n: int = 0
    p50: float = float("nan")
    p90: float = float("nan")
    max: float = float("nan")
    row_bands_covered: int = 0
    height_coverage: float = 0.0
    implied_dx: float = float("nan")
    implied_dy: float = float("nan")
    slope_spread: float = 0.0
    observations: list[ScrObservation] = field(default_factory=list)


def to_gray(image: np.ndarray) -> np.ndarray:
    if image.ndim == 3:
        return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float64)
    return image.astype(np.float64)


def default_band(height: int) -> tuple[int, int]:
    """Rows to measure over.

    Trimmed top and bottom: on a sideline mount the top is sky/tree line and
    the bottom is the near touchline at a metre or two, where parallax is tens
    of pixels and no single-depth calibration can help (see 12.1).
    """
    return int(height * 0.14), int(height * 0.995)


# -- SSR ---------------------------------------------------------------------


def seam_sharpness_ratio(
    image: np.ndarray,
    seam_x: int = SEAM_X,
    blend_w: int = BLEND_W,
    shoulder_w: int = SHOULDER_W,
    band: tuple[int, int] | None = None,
) -> SsrResult:
    gray = to_gray(image)
    y0, y1 = band or default_band(gray.shape[0])
    strip = gray[y0:y1]

    col_mean = strip.mean(axis=0)
    detrended = strip - col_mean[None, :]

    def energy(a: np.ndarray) -> np.ndarray:
        # gradient energy per column; index i is the step from x=i to x=i+1
        return (np.diff(a, axis=1) ** 2).mean(axis=0)

    e_det = energy(detrended)
    e_raw = energy(strip)

    half = blend_w // 2
    lo, hi = seam_x - half, seam_x + half
    left = np.arange(max(0, lo - shoulder_w), lo)
    right = np.arange(hi, min(len(e_det), hi + shoulder_w))
    shoulders = np.concatenate([left, right])
    if shoulders.size < 32:
        raise ValueError("not enough shoulder columns to fit a background")

    def ratio(e: np.ndarray) -> tuple[float, float]:
        # Fit log-energy quadratically across the shoulders and interpolate it
        # over the window. Fitting in log space keeps the fit from being
        # dragged by a few high-energy columns -- but it needs a floor, because
        # one near-flat column (a blown highlight, a letterboxed edge) would
        # otherwise contribute log(1e-9) and drag the whole quadratic with it.
        # The floor is relative to the frame's own energy, so it scales.
        floor = max(float(np.median(e[shoulders])) * 1e-4, 1e-9)
        ys = np.log(np.maximum(e[shoulders], floor))
        coef = np.polyfit(shoulders.astype(np.float64), ys, 2)
        resid = ys - np.polyval(coef, shoulders.astype(np.float64))
        win = np.arange(lo, min(hi, len(e)))
        bg = np.maximum(np.exp(np.polyval(coef, win.astype(np.float64))), floor)
        return float(np.mean(e[win] / bg)), float(np.sqrt(np.mean(resid**2)))

    ssr, bg_rms = ratio(e_det)
    ssr_raw, _ = ratio(e_raw)
    return SsrResult(
        ssr=ssr,
        abs_ln_ssr=abs(float(np.log(max(ssr, 1e-9)))),
        ssr_raw_undetrended=ssr_raw,
        background_rms_log=bg_rms,
        band=(y0, y1),
    )


# -- SCR ---------------------------------------------------------------------


def _edge_positions(col: np.ndarray, min_strength: float) -> list[tuple[float, float]]:
    """Sub-pixel y of each strong near-horizontal edge in one column.

    `col` is |d/dy| of the smoothed image for that column. Peaks are refined by
    a parabola through the peak and its two neighbours, which is good to well
    under a pixel and costs nothing.
    """
    out: list[tuple[float, float]] = []
    for i in range(1, len(col) - 1):
        a, b, c = col[i - 1], col[i], col[i + 1]
        if b < min_strength or b < a or b < c:
            continue
        denom = a - 2 * b + c
        shift = 0.5 * (a - c) / denom if denom != 0 else 0.0
        if abs(shift) > 1.0:
            shift = 0.0
        out.append((i + shift, b))
    return out


def _chains(
    grad: np.ndarray,
    xs: np.ndarray,
    min_strength: float,
    max_step: float,
    min_len: int,
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Link per-column edge points into near-horizontal chains, inner-to-outer.

    `xs` is walked in the order given, so the caller decides which end is the
    seam side; chains are seeded at the first column and extended outward. A
    chain that cannot be continued dies -- no gap bridging, because a bridged
    gap is how one structure gets fitted as two.
    """
    per_col = [_edge_positions(grad[:, x], min_strength) for x in xs]
    chains: list[list[tuple[float, float]]] = []
    alive: list[list[tuple[float, float]]] = []
    for k, pts in enumerate(per_col):
        used = set()
        still: list[list[tuple[float, float]]] = []
        for ch in alive:
            last_y = ch[-1][1]
            best, best_d = None, max_step
            for j, (y, _s) in enumerate(pts):
                if j in used:
                    continue
                d = abs(y - last_y)
                if d < best_d:
                    best, best_d = j, d
            if best is None:
                if len(ch) >= min_len:
                    chains.append(ch)
                continue
            used.add(best)
            ch.append((float(xs[k]), pts[best][0]))
            still.append(ch)
        for j, (y, _s) in enumerate(pts):
            if j not in used:
                still.append([(float(xs[k]), y)])
        alive = still
    chains += [ch for ch in alive if len(ch) >= min_len]
    return [
        (np.array([p[0] for p in ch]), np.array([p[1] for p in ch])) for ch in chains
    ]


def _fit(xs: np.ndarray, ys: np.ndarray) -> tuple[float, float, float]:
    m, c = np.polyfit(xs, ys, 1)
    rms = float(np.sqrt(np.mean((ys - (m * xs + c)) ** 2)))
    return float(m), float(c), rms


def interp_dx_anchors(anchors: Sequence[tuple[float, float]], y: float) -> float:
    """Linear interpolation of `[y, dx]` anchors, clamped outside their range.

    `np.interp`, deliberately: it is what the shipped downstream corrector uses
    (`stitch_remap.build_dx_lookup`) and what `lut2d.interp_dx` reproduces for
    the camera. One anchor list has to mean one curve on every surface, or the
    number an operator tunes against is not the number that gets applied.
    """
    if not anchors:
        return 0.0
    ys = [float(a[0]) for a in anchors]
    ds = [float(a[1]) for a in anchors]
    return float(np.interp(y, ys, ds))


@dataclass
class ShoulderChains:
    """Structures detected on each shoulder, kept so a *candidate* dx can be
    scored without re-running the detector.

    Detection is the whole cost of SCR -- linking per-column edge points into
    chains is a Python loop over hundreds of columns and, on a noisy frame,
    hundreds of points per column (13 s on a 7680x2160 still). Scoring a fitted
    chain against a candidate dx is arithmetic on a handful of numbers.

    That split is what makes an interactive tool possible: the operator drags,
    and every candidate curve is scored against the SAME objective the solver
    minimises, at interactive rates, because the expensive half was done once
    when the frame was fetched.

    The chains are stored as raw point arrays rather than as fitted lines
    because a dx that varies with y *shears* a structure -- it changes the
    slope, not just the intercept -- so the fit has to be redone against the
    displaced points. `residual_from_chains` does exactly that.

    What this cannot model: re-detection. A shear changes the gradient field
    slightly, so a marginal structure could in principle appear or vanish. The
    displaced-refit path holds the detected set fixed. See
    `tests/test_seam_metric_incremental.py`, which pins the agreement against a
    genuinely shifted image rather than asserting it.
    """

    left: list[tuple[np.ndarray, np.ndarray]] = field(default_factory=list)
    right: list[tuple[np.ndarray, np.ndarray]] = field(default_factory=list)
    y0: int = 0
    y1: int = 0
    seam_x: int = SEAM_X

    @property
    def band(self) -> tuple[int, int]:
        return self.y0, self.y1


def detect_shoulder_chains(
    image: np.ndarray,
    seam_x: int = SEAM_X,
    blend_w: int = BLEND_W,
    shoulder_w: int = SHOULDER_W,
    band: tuple[int, int] | None = None,
    min_strength: float = 3.0,
    max_slope: float = 0.35,
    min_len: int = 60,
) -> ShoulderChains:
    """The expensive half of SCR: find near-horizontal structures on both
    shoulders. Chain x is absolute image column; chain y is relative to `y0`.
    """
    gray = to_gray(image)
    y0, y1 = band or default_band(gray.shape[0])
    strip = cv2.GaussianBlur(gray[y0:y1], (0, 0), 1.4)
    grad = np.abs(cv2.Sobel(strip, cv2.CV_64F, 0, 1, ksize=3))

    half = blend_w // 2
    left_xs = np.arange(seam_x - half - 1, seam_x - half - 1 - shoulder_w, -1)
    right_xs = np.arange(seam_x + half, seam_x + half + shoulder_w)
    left_xs = left_xs[(left_xs >= 0) & (left_xs < gray.shape[1])]
    right_xs = right_xs[(right_xs >= 0) & (right_xs < gray.shape[1])]

    max_step = max_slope * 1.5 + 1.0
    return ShoulderChains(
        left=_chains(grad, left_xs, min_strength, max_step, min_len),
        right=_chains(grad, right_xs, min_strength, max_step, min_len),
        y0=y0,
        y1=y1,
        seam_x=seam_x,
    )


def residual_from_chains(
    chains: ShoulderChains,
    dx_anchors: Sequence[tuple[float, float]] | None = None,
    *,
    max_slope: float = 0.35,
    max_slope_diff: float = 0.06,
    max_gap: float = 40.0,
    max_fit_rms: float = 1.2,
) -> ScrResult:
    """The cheap half of SCR: fit, match across the seam, and summarise.

    `dx_anchors` are `[y, dx]` in the coordinates of the image the chains were
    detected on, with the sign convention of the whole project: **dx is the
    number of pixels the RIGHT half must move right, at row y, to register with
    the left**. Passing them scores the curve *as if* it had been applied,
    which is what lets an operator see the objective move while dragging.

    The displacement is applied to the right-hand chain points and the line is
    refitted, not applied to the fitted intercept: a dx that varies with y is a
    shear, and a shear rotates a sloped structure as well as translating it.
    `y = m*x + c` sheared by `dx(y)` with local ramp `k` becomes
    `m' = m/(1 + m*k)` -- a 1.3% slope change at the extremes of this camera's
    range, small but free to get right.
    """
    y0, y1, seam_x = chains.y0, chains.y1, chains.seam_x

    def fits(
        chs: list[tuple[np.ndarray, np.ndarray]], displace: bool
    ) -> list[tuple[float, float, float, int]]:
        out = []
        for xs, ys in chs:
            if displace and dx_anchors:
                xs = xs + np.array(
                    [interp_dx_anchors(dx_anchors, float(y) + y0) for y in ys]
                )
            m, c, rms = _fit(xs, ys)
            if abs(m) > max_slope or rms > max_fit_rms:
                continue
            out.append((m, c, rms, len(xs)))
        return out

    lfits, rfits = fits(chains.left, False), fits(chains.right, True)

    obs: list[ScrObservation] = []
    taken: set[int] = set()
    for lm, lc, lrms, llen in sorted(lfits, key=lambda f: f[2]):
        yl = lm * seam_x + lc
        best, best_d = None, max_gap
        for j, (rm, rc, _rrms, _rlen) in enumerate(rfits):
            if j in taken or abs(rm - lm) > max_slope_diff:
                continue
            d = abs((rm * seam_x + rc) - yl)
            if d < best_d:
                best, best_d = j, d
        if best is None:
            continue
        taken.add(best)
        rm, rc, rrms, rlen = rfits[best]
        yr = rm * seam_x + rc
        m = 0.5 * (lm + rm)
        ry = yr - yl
        obs.append(
            ScrObservation(
                y_left=yl + y0,
                y_right=yr + y0,
                slope=m,
                residual_y=ry,
                residual_perp=abs(ry) / float(np.hypot(1.0, m)),
                span_px=min(llen, rlen),
                fit_rms=max(lrms, rrms),
            )
        )

    res = ScrResult(n=len(obs), observations=obs)
    if not obs:
        return res
    perp = np.array([o.residual_perp for o in obs])
    res.p50 = float(np.percentile(perp, 50))
    res.p90 = float(np.percentile(perp, 90))
    res.max = float(perp.max())

    ys = np.array([0.5 * (o.y_left + o.y_right) for o in obs])
    height = y1 - y0
    nb = 3
    res.row_bands_covered = len({int((y - y0) / height * nb) for y in ys})
    res.height_coverage = float((ys.max() - ys.min()) / height) if len(ys) > 1 else 0.0

    slopes = np.array([o.slope for o in obs])
    res.slope_spread = float(slopes.max() - slopes.min())
    if len(obs) >= 3 and res.slope_spread > 0.02:
        # r_y = dy - m * dx, solved robustly (IRLS with a Huber weight)
        a = np.column_stack([np.ones_like(slopes), -slopes])
        r = np.array([o.residual_y for o in obs])
        w = np.ones_like(r)
        beta = np.zeros(2)
        for _ in range(12):
            aw = a * w[:, None]
            beta, *_ = np.linalg.lstsq(aw, r * w, rcond=None)
            resid = r - a @ beta
            scale = max(1.4826 * float(np.median(np.abs(resid))), 1e-6)
            w = np.clip(1.345 * scale / np.maximum(np.abs(resid), 1e-9), 0.0, 1.0)
        res.implied_dy, res.implied_dx = float(beta[0]), float(beta[1])
    return res


def seam_continuity_residual(
    image: np.ndarray,
    seam_x: int = SEAM_X,
    blend_w: int = BLEND_W,
    shoulder_w: int = SHOULDER_W,
    band: tuple[int, int] | None = None,
    min_strength: float = 3.0,
    max_slope: float = 0.35,
    max_slope_diff: float = 0.06,
    max_gap: float = 40.0,
    max_fit_rms: float = 1.2,
    min_len: int = 60,
) -> ScrResult:
    """Fit near-horizontal structures on both shoulders, extrapolate, compare.

    Only near-horizontal structures can be used, because a structure has to
    span the whole blend window in x to be extrapolated to the seam from both
    sides. That has a consequence worth stating: such a line is displaced
    *vertically* by a horizontal misregistration, by `r_y = -m * dx` where m is
    its slope. So a single line under-determines dx (a flat line sees nothing),
    and the estimate comes from regressing r_y on m across lines of differing
    slope. `implied_dx` is that regression; it is only meaningful when
    `slope_spread` is non-trivial, which the caller must check.

    SENSE, because it is the opposite of the artifact's and nothing else says
    so. `implied_dx` here is the misregistration the right half currently
    CARRIES -- positive means its content sits that many px too far right. The
    profile's `dx_anchors` are the CORRECTION, "px the right half must move
    right" (`stitch_remap.build_dx_lookup`, STITCH_CALIBRATION.md 4.4), so the
    two are equal and opposite. `stitch_solver` fits the correction directly
    rather than negating this, and `tests/test_stitch_solver.py` pins the
    relationship so the trap stays visible.

    `implied_dx` is also a single whole-frame number, not a curve: it assumes
    one dx for every row. A per-row shear needs the joint fit in
    `stitch_solver.solve`, which this deliberately is not.

    Detection and scoring are split (`detect_shoulder_chains` /
    `residual_from_chains`) so an interactive caller can re-score a candidate
    dx without paying for detection again; this function is the one-shot
    composition of the two and is what every batch caller should use.
    """
    return residual_from_chains(
        detect_shoulder_chains(
            image,
            seam_x=seam_x,
            blend_w=blend_w,
            shoulder_w=shoulder_w,
            band=band,
            min_strength=min_strength,
            max_slope=max_slope,
            min_len=min_len,
        ),
        max_slope=max_slope,
        max_slope_diff=max_slope_diff,
        max_gap=max_gap,
        max_fit_rms=max_fit_rms,
    )


# -- acceptance --------------------------------------------------------------


class SeamGateFailed(Exception):
    """A calibration did not earn the right to be kept."""


def check_acceptance(
    before: dict,
    after: dict,
    *,
    min_scr_improvement: float = 0.50,
    max_scr_p90: float = 2.0,
    min_observations: int = 8,
    min_row_bands: int = 3,
    min_height_coverage: float = 0.60,
    camera_side_owner: bool = True,
) -> None:
    """Raise unless the calibration is demonstrably better, not merely different.

    Every condition refuses. Per the project rule that in an automated chain a
    warning nothing reads is not a guard, this is the function a calibration
    loop calls before it keeps a result -- an under-determined fit that logs a
    warning and writes a profile anyway is the worst outcome available, because
    it is wrong and it looks finished.

    The coverage conditions are the ones that actually bite in practice. The
    seam sits mid-field and mid-field is featureless grass, so "not enough
    structure crossing the seam" is the expected case, not the exotic one.
    """
    fails: list[str] = []
    b, a = before["scr"], after["scr"]

    if a["n"] < min_observations:
        fails.append(f"only {a['n']} accepted structures (need {min_observations})")
    if a["row_bands_covered"] < min_row_bands:
        fails.append(
            f"structures span {a['row_bands_covered']} row band(s), need {min_row_bands}"
        )
    if a["height_coverage"] < min_height_coverage:
        fails.append(
            f"structures cover {a['height_coverage'] * 100:.0f}% of the field band, "
            f"need {min_height_coverage * 100:.0f}%"
        )
    if b["n"] and a["n"]:
        if not (a["p90"] < max_scr_p90):
            fails.append(f"SCR p90 {a['p90']:.2f} px is not below {max_scr_p90:.1f} px")
        if b["p90"] > 0 and (b["p90"] - a["p90"]) / b["p90"] < min_scr_improvement:
            fails.append(
                f"SCR p90 improved {(b['p90'] - a['p90']) / b['p90'] * 100:.0f}%, "
                f"need {min_scr_improvement * 100:.0f}%"
            )

    db = before["ssr"]["abs_ln_ssr"]
    da = after["ssr"]["abs_ln_ssr"]
    if da > db + before["ssr"]["noise_floor"]:
        fails.append(f"|ln SSR| got worse: {db:.3f} -> {da:.3f}")
    elif camera_side_owner and da >= db:
        # A camera-side correction acts BEFORE the blend, so it must restore
        # gradient energy the mixing was destroying. If it did not, the
        # panorama merely moved -- which is all a downstream shift can do, and
        # is not what the mesh path is for.
        fails.append(
            f"|ln SSR| did not improve ({db:.3f} -> {da:.3f}); a camera-side "
            "correction that only moves the panorama has not registered anything"
        )

    if fails:
        raise SeamGateFailed("; ".join(fails))


# -- reporting ---------------------------------------------------------------


def measure(image: np.ndarray, seam_x: int = SEAM_X, blend_w: int = BLEND_W) -> dict:
    ssr = seam_sharpness_ratio(image, seam_x=seam_x, blend_w=blend_w)
    scr = seam_continuity_residual(image, seam_x=seam_x, blend_w=blend_w)
    out = {
        "seam_x": seam_x,
        "blend_window": [seam_x - blend_w // 2, seam_x + blend_w // 2],
        "band": list(ssr.band),
        "ssr": {k: v for k, v in asdict(ssr).items() if k != "band"},
        "scr": {k: v for k, v in asdict(scr).items() if k != "observations"},
        "scr_n_observations": scr.n,
    }
    out["ssr"]["noise_floor"] = ssr.noise_floor
    return out


def _load(path: str) -> np.ndarray:
    img = cv2.imread(path, cv2.IMREAD_COLOR)
    if img is None:
        raise SystemExit(f"cannot read {path}")
    return img


def _print(tag: str, m: dict) -> None:
    s, c = m["ssr"], m["scr"]
    print(f"{tag}")
    print(
        f"  SSR  {s['ssr']:.3f}   |ln SSR| {s['abs_ln_ssr']:.3f}"
        f"  (noise floor {s['noise_floor']:.2f}; raw undetrended"
        f" {s['ssr_raw_undetrended']:.3f})"
    )
    if c["n"]:
        print(
            f"  SCR  n={c['n']}  p50 {c['p50']:.2f} px  p90 {c['p90']:.2f} px"
            f"  max {c['max']:.2f} px"
        )
        print(
            f"       bands {c['row_bands_covered']}/3  coverage"
            f" {c['height_coverage'] * 100:.0f}%  slope spread"
            f" {c['slope_spread']:.3f}"
        )
        if c["slope_spread"] > 0.02:
            print(
                f"       implied dx {c['implied_dx']:+.2f} px   dy"
                f" {c['implied_dy']:+.2f} px"
            )
        else:
            print("       implied dx: under-determined (all slopes alike)")
    else:
        print("  SCR  no structure crossing the seam -- nothing to measure")


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    flags = {a for a in argv[1:] if a.startswith("--")}
    seam = SEAM_X
    for a in argv[1:]:
        if a.startswith("--seam-x="):
            seam = int(a.split("=", 1)[1])
    if "--compare" in flags:
        if len(args) != 2:
            print("usage: seam_metric.py --compare <before.jpg> <after.jpg>")
            return 2
        before = measure(_load(args[0]), seam_x=seam)
        after = measure(_load(args[1]), seam_x=seam)
        if "--json" in flags:
            print(json.dumps({"before": before, "after": after}, indent=2))
            return 0
        _print(f"BEFORE  {args[0]}", before)
        _print(f"AFTER   {args[1]}", after)
        d_ssr = after["ssr"]["abs_ln_ssr"] - before["ssr"]["abs_ln_ssr"]
        print(f"\n  delta |ln SSR| {d_ssr:+.3f}  (negative is better)")
        if before["scr"]["n"] and after["scr"]["n"]:
            d = after["scr"]["p90"] - before["scr"]["p90"]
            print(f"  delta SCR p90  {d:+.2f} px  (negative is better)")
        return 0
    if not args:
        print(__doc__)
        return 2
    m = measure(_load(args[0]), seam_x=seam)
    if "--json" in flags:
        print(json.dumps(m, indent=2))
        return 0
    _print(args[0], m)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
