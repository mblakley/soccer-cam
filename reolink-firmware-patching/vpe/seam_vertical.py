"""Does anything at the seam actually *see* a horizontal misregistration?

`seam_metric.SCR` fits **near-horizontal** structures (`max_slope = 0.35`) on the
two shoulders and compares where they arrive at the seam. Write out what a
horizontal shift does to such a structure and the problem is immediate:

    r_y = -m * dx          (seam_metric.seam_continuity_residual, and section 9.1)

so an observation's sensitivity to `dx`, in px of residual per px of shift, is

    sensitivity = |m| / sqrt(1 + m^2)   ->  0 as the structure flattens.

A perfectly horizontal edge is **invariant** under a horizontal shift. It still
gets detected, still gets paired across the seam, still contributes an
observation and a residual -- and that residual says nothing whatever about
registration. On a soccer field nearly everything that crosses a vertical seam
runs horizontally: the far touchline, the treeline, the grass/track boundary,
painted banners. That is the mechanism behind the measured anomaly recorded in
`video_grouper/web/stitch_calibration.py`: 40-69 observations, every coverage
gate passed, p90 of 27-36 px, and an objective that barely moves across the
whole plausible dx range, on frames whose seams are visibly registered.

Two instruments here, neither of which redefines anything in `seam_metric`:

`split_by_dx_sensitivity` sorts an existing `ScrResult`'s observations into the
ones that can see `dx` and the ones that cannot, so a p90 can be reported over
the steering subset instead of over a set that is mostly blind to the quantity
being tuned.

`vertical_structure` asks the *picture* the prior question: is there any
vertical structure in the blend window at all? It is a presence detector, not a
registration estimator -- it says "there is something upright at rows 900-1100
that a horizontal error would break", or it says there is nothing, in which case
the honest instruction to an operator standing at the pitch is not "this frame
is unusable" but **put a person in the seam**. A person is upright by
construction: torso, legs, head. That is exactly the structure a horizontal
misregistration destroys and exactly what a horizontal edge cannot supply.

**What this module does not do, deliberately.** It does not measure
registration, and it must never be read as doing so. A person standing in the
seam is *inside* the blend window, where the two sensors are already
superposed; there is no separated left/right pair to extrapolate from and
compare, so no shoulder-matching estimator can score them. The automatic solver
established that from the other side, on the same archive: across 12
vertical-structure-rich frames from one fixed camera, `implied_dx` spanned
-55..+141 px and 4 of the 12 produced no observations at all. The instrument
that works on a person in the seam is the operator's eye -- a body torn at the
seam is unmistakable at 4x and invisible to the metric -- and this module's only
job is to tell them whether there is a body there to look at, and where.

A second thing this is not: evidence of a defect. Across the archived set 52 of
96 frames sit below the SSR noise floor and players straddling the seam look
continuous, so it is not established that these placements are misregistered at
all. A high ratio here means "something upright is in the corridor", never
"the corridor is broken".

Validated as a presence detector on real frames, which is the only claim made
for it: see `MIN_VERTICAL_RATIO` for the distribution it was calibrated against
and the one false negative found. `split_by_dx_sensitivity` needs no validation
beyond arithmetic -- it is the derivative of a formula `seam_metric` already
documents -- but the conclusion drawn from it was measured: on three games,
0, 1 and 2 of 11, 61 and 88 accepted structures were steeper than |m| = 0.15.

Per section 12.1 the person has to stand where play happens. One calibration is
exact at one depth, and a target at 2 m carries ~60 px of parallax disparity
that has nothing to do with the lens roll being calibrated.

Note the deliberate difference from section 10, which *rejects* players: the
automatic solver accumulates observations across a whole game, where players
move, straddle unknown depths and are the thing being detected. A cooperating
person standing still at a stated distance, in a frame an operator chose, is
the opposite case -- the depth is known because someone walked to it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import cv2
import numpy as np

SEAM_X = 3840
BLEND_W = 256

#: Below this, an observation is blind to `dx` for practical purposes: at
#: |m| = 0.05 a 20 px misregistration moves the residual by 1 px, which is the
#: acceptance gate's whole budget. Structures flatter than this are reported
#: separately rather than averaged into a number the operator is trying to
#: minimise.
MIN_DX_SENSITIVITY = 0.05


class _Observation(Protocol):
    """Structurally, a `seam_metric.ScrObservation`. Duck-typed on purpose so
    this module never imports the metric it comments on."""

    slope: float
    residual_perp: float


def dx_sensitivity(slope: float) -> float:
    """Px of perpendicular residual per px of horizontal misregistration.

    `r_y = -m*dx`, and `residual_perp = |r_y| / hypot(1, m)`, so the derivative
    of the reported residual with respect to dx is `|m| / hypot(1, m)` -- the
    sine of the structure's angle from horizontal. 1 for a vertical structure,
    0 for a horizontal one.
    """
    return float(abs(slope) / np.hypot(1.0, slope))


@dataclass
class SensitivitySplit:
    """An `ScrResult`'s observations, sorted by whether they can see `dx`."""

    n_total: int = 0
    n_steering: int = 0
    p50_steering: float = float("nan")
    p90_steering: float = float("nan")
    p90_blind: float = float("nan")
    #: Median sensitivity among the steering subset: how many px of residual one
    #: px of misregistration actually buys on this frame.
    median_sensitivity: float = 0.0
    #: Worst-case: even the steepest structure here only converts this fraction
    #: of a shift into a residual.
    max_sensitivity: float = 0.0

    @property
    def blind_fraction(self) -> float:
        if not self.n_total:
            return 0.0
        return 1.0 - self.n_steering / self.n_total


def split_by_dx_sensitivity(
    observations: Sequence[_Observation],
    min_sensitivity: float = MIN_DX_SENSITIVITY,
) -> SensitivitySplit:
    """Summarise an observation set by whether it can steer a dx at all."""
    if not observations:
        return SensitivitySplit()
    sens = np.array([dx_sensitivity(o.slope) for o in observations])
    perp = np.array([float(o.residual_perp) for o in observations])
    steering = sens >= min_sensitivity
    out = SensitivitySplit(
        n_total=len(observations),
        n_steering=int(steering.sum()),
        max_sensitivity=round(float(sens.max()), 4),
    )
    if out.n_steering:
        out.p50_steering = float(np.percentile(perp[steering], 50))
        out.p90_steering = float(np.percentile(perp[steering], 90))
        out.median_sensitivity = round(float(np.median(sens[steering])), 4)
    if out.n_steering < out.n_total:
        out.p90_blind = float(np.percentile(perp[~steering], 90))
    return out


# -- presence of upright structure -------------------------------------------


@dataclass
class VerticalBand:
    y0: int
    y1: int
    #: Vertical-edge energy in the seam corridor over the same statistic taken
    #: across a wide reference slab at the same rows. 1.0 means the corridor is
    #: as featureless as the rest of the row.
    ratio: float
    #: Column of the strongest vertical edge in the corridor, absolute x.
    peak_x: int
    has_structure: bool


@dataclass
class VerticalProfile:
    bands: list[VerticalBand] = field(default_factory=list)
    corridor: tuple[int, int] = (0, 0)
    band: tuple[int, int] = (0, 0)
    min_ratio: float = 0.0

    @property
    def n_with_structure(self) -> int:
        return sum(1 for b in self.bands if b.has_structure)

    @property
    def best(self) -> VerticalBand | None:
        return max(self.bands, key=lambda b: b.ratio) if self.bands else None

    @property
    def rows_with_structure(self) -> list[tuple[int, int]]:
        """Contiguous row ranges that hold upright structure, merged."""
        out: list[list[int]] = []
        for b in self.bands:
            if not b.has_structure:
                continue
            if out and out[-1][1] == b.y0:
                out[-1][1] = b.y1
            else:
                out.append([b.y0, b.y1])
        return [(a, b) for a, b in out]


#: A corridor whose 90th-percentile column energy exceeds the reference slab's
#: by this much holds something upright.
#:
#: Calibrated on the archived Duo 3 frame set rather than chosen: over 87 frames
#: spanning 28 tripod placements the band ratio runs p25 0.94, p50 1.23, p75
#: 4.36, max 59.3, and the threshold sits in the gap. Bare grass reads 0.80-0.82;
#: a player straddling the seam reads 20.6, and the rows the detector returns for
#: that frame (404-609) are the rows the player occupies, checked against the
#: image at 4x. 31 of the 87 frames are flagged, 56 are not.
#:
#: Known false negative: a painted line running near-vertically through the
#: corridor read 1.78 on one placement and was not flagged. It is real upright
#: structure and the detector missed it, which is the conservative direction --
#: the cost is a "put a person in the seam" prompt the operator did not need.
MIN_VERTICAL_RATIO = 2.2


def vertical_structure(
    image: np.ndarray,
    seam_x: int = SEAM_X,
    blend_w: int = BLEND_W,
    band: tuple[int, int] | None = None,
    n_bands: int = 18,
    ref_w: int = 1200,
    min_ratio: float = MIN_VERTICAL_RATIO,
) -> VerticalProfile:
    """Where, if anywhere, upright structure crosses the seam corridor.

    Measured on |dI/dx|^2 -- the gradient a *horizontal* misregistration
    displaces -- per column, then compared against the same statistic over a
    wide slab at the same rows. Normalising against the frame's own texture at
    the same rows is what makes the number mean "unusual here" rather than
    "bright scene": grass reads ~1 whatever the light.

    Deliberately not the blend window's *damage*, which is what
    `seam_metric.seam_sharpness_ratio` measures. This is presence: it answers
    "is there anything here for a misregistration to break", which is the
    question an operator has *before* they trust any registration number.
    """
    gray = image
    if gray.ndim == 3:
        gray = cv2.cvtColor(gray, cv2.COLOR_BGR2GRAY)
    gray = gray.astype(np.float32)
    h, w = gray.shape
    y0, y1 = band or (int(h * 0.14), int(h * 0.995))
    half = blend_w // 2
    c0, c1 = max(0, seam_x - half), min(w, seam_x + half)
    r0, r1 = max(0, seam_x - ref_w), min(w, seam_x + ref_w)

    slab = cv2.GaussianBlur(gray[y0:y1, r0:r1], (0, 0), 1.0)
    energy = cv2.Sobel(slab, cv2.CV_32F, 1, 0, ksize=3) ** 2
    cor_lo, cor_hi = c0 - r0, c1 - r0

    profile = VerticalProfile(corridor=(c0, c1), band=(y0, y1), min_ratio=min_ratio)
    rows = y1 - y0
    if rows < n_bands or cor_hi <= cor_lo:
        return profile
    edges = np.linspace(0, rows, n_bands + 1).astype(int)
    for i in range(n_bands):
        a, b = edges[i], edges[i + 1]
        if b <= a:
            continue
        col = energy[a:b].mean(axis=0)
        corridor = col[cor_lo:cor_hi]
        reference = np.concatenate([col[:cor_lo], col[cor_hi:]])
        if reference.size < 64 or corridor.size < 8:
            continue
        # p90 against p90: the corridor is 256 px of a 2400 px slab, so a person
        # occupying 30 columns is a tenth of the corridor and a hundredth of the
        # reference. Comparing like with like keeps a textured frame at ~1
        # instead of manufacturing a ratio out of the tail of grass noise.
        cor_level = float(np.percentile(corridor, 90))
        ref_level = float(np.percentile(reference, 90))
        ratio = cor_level / max(ref_level, 1e-6)
        profile.bands.append(
            VerticalBand(
                y0=int(y0 + a),
                y1=int(y0 + b),
                ratio=round(ratio, 3),
                peak_x=int(c0 + int(np.argmax(corridor))),
                has_structure=bool(ratio >= min_ratio),
            )
        )
    return profile


def summarise(profile: VerticalProfile, split: SensitivitySplit | None = None) -> dict:
    """JSON for the UI: the verdict first, the numbers behind it after."""
    best = profile.best
    out: dict[str, Any] = {
        "corridor": list(profile.corridor),
        "min_ratio": profile.min_ratio,
        "n_bands": len(profile.bands),
        "n_with_structure": profile.n_with_structure,
        "rows": [list(r) for r in profile.rows_with_structure],
        "best_ratio": None if best is None else best.ratio,
        "best_rows": None if best is None else [best.y0, best.y1],
        "best_x": None if best is None else best.peak_x,
        "bands": [
            {"y0": b.y0, "y1": b.y1, "ratio": b.ratio, "on": b.has_structure}
            for b in profile.bands
        ],
    }
    if split is not None:
        out["scr_split"] = {
            "n_total": split.n_total,
            "n_steering": split.n_steering,
            "blind_fraction": round(split.blind_fraction, 3),
            "p50_steering": None
            if split.p50_steering != split.p50_steering
            else round(split.p50_steering, 3),
            "p90_steering": None
            if split.p90_steering != split.p90_steering
            else round(split.p90_steering, 3),
            "p90_blind": None
            if split.p90_blind != split.p90_blind
            else round(split.p90_blind, 3),
            "median_sensitivity": split.median_sensitivity,
            "max_sensitivity": split.max_sensitivity,
        }
    return out
