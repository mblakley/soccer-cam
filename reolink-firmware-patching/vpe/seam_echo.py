"""Seam echo diagnostics. WITHDRAWN -- does not measure registration.

**Read the WITHDRAWN section below before trusting anything else in this file.**
The design notes that follow it are kept because they are the record of what was
tried and why, but several of their claims were falsified at scale and are
annotated where that happened.

Originally: read `dx` off whatever is standing in the seam.

Five passes have tried to measure this camera's stitch misregistration
automatically. Four built a detector for an **object class** -- a ball, a person
-- and every one of them measured something else: grass texture, a parked car, a
player's shirt, a player's shorts. This module does not detect any object class,
and must not be extended to.

**The physics is class-agnostic.** Inside the blend the output is

    s(x) = a*b(x) + (1-a)*b(x-d)

for *whatever* `b` happens to be there -- a chair, a car, a person, the vertical
stroke of a painted number. The identity of `b` is irrelevant. Only two things
matter: the structure has horizontal gradient (a purely horizontal edge is
invariant under a horizontal shift and carries nothing), and it sits inside the
mixing corridor.

Three things make this work where the earlier attempts failed, and all three are
load-bearing.

**1. Candidate selection is chromatic, not gradient-based.** Grass has enormous
luminance texture and almost no colour distinctiveness, which is why every
gradient-energy gate let it straight through -- Sobel loves mown blades.
Measured on the hand-verified frames, distance in Lab from the local pitch
colour -- **chroma only, excluding lightness** -- separates the ball from
same-row grass by **9.7x** (27.4 vs 2.8) and from the worst foreground grass by
**8.7x** (27.4 vs 3.2), while |dI/dx| p95 separates them by only 8.3x and 1.8x
and puts foreground grass at 118, comfortably inside any gradient threshold that
still admits real targets. Keeping L in the distance would undo most of this
(the same ratios fall to 3.9x and 2.5x), because mown grass *is* a luminance
texture.
A red shirt, a blue chair, a car, a white line and a ball all pass a chromatic
gate; grass does not. `lab` is reported on every candidate so a reviewer can see
the estimator ran on something actually distinct from the pitch. That audit
trail is what the previous four passes lacked.

**2. Measuring in the colour-distance channel, not in grey.** This is not merely
a better gate, it changes the estimator. In grey level a window on the seam is
~33 px of ghosted ball against ~60 px of high-gradient **unghosted** grass, and
the grass wins: a tight window on a visibly-doubled ball reads 0.03-0.06 by
autocorrelation, indistinguishable from control corridors, even though the same
estimator recovers a synthetic ghost injected into the same grass at 0.23-0.57.
In the colour-distance channel the grass falls to ~0 and the object's two copies
are the only signal left. The ghost is then plainly legible: on frame 1104 the
profile shows two lobes, ~45 at -14 px and ~33 at +3 px with a dip between them,
i.e. separation ~17 px at an amplitude ratio near 0.45.

**3. `a` is predicted by geometry, not fitted freely.** The blend weight at
column x is known:

    a(x) = 0.5 - (x - seam_x) / (2 * blend_w)

so mixing is not a free parameter. This is what turns the awkward cases into
principled refusals instead of statistical ones. A ball 106 px from the seam
sits at `a_pred` = 0.086 -- there is only an 8.6% ghost to see there, and no
estimator should be believed on it. An earlier two-copy fit reported "separation
16.1 px" for exactly that ball, where the true answer is zero. Requiring
`a_pred` in [0.29, 0.71] confines the measurement to the region where a ghost
can physically exist, and requiring the *fitted* `a` to agree with the predicted
one was intended as a consistency check that scene structure could not fake.
**That last claim is false.** Ordinary single, unghosted players in *control*
corridors fit `a` = 0.40-0.60 against predictions of 0.32-0.70 and sail through
it, because a step edge is genuinely well described by two partially-overlapping
lobes of comparable weight. The check constrains the *shape* of a fit; it does
not establish that a second copy exists.

**WITHDRAWN 2026-08-19: this estimator does not work, and no longer proposes
anchors.** It is kept because the plumbing, the chromatic gate and the control
machinery are reusable and because the negative result is specific enough to stop
a fifth attempt repeating it. `measure()` reports its numbers and always returns
`verdict="withdrawn"`; `anchors_from_measurement` refuses unconditionally.

What it actually measures is a **step edge**, not an echo. A shirt against grass
is a step, and two lobes fit a step better than one does, so `gain` rises on
exactly the objects the chromatic gate is best at finding. `gain` is therefore
*anti-correlated with truth* on this material. Measured at scale over 7,688
frames / 28 games: seam acceptance 7.4% against control acceptance 6.3%
(**1.18x**, against the 3.0 this module itself demands), `ctl-1200` accepting
*more often* than the seam, d-histograms overlapping 0.85, and seam/control
medians of 31/30 px which stay 31/30 after matching on colour decile and row
band. On the one hand-verified frame with a real 18 px ghost it published
**33.0 px**, because all three accepted candidates sat on a walking player's
torso, shorts and leg 37-47 px from the seam while the ball's own band ranked
15th chromatically and `max_fits=10` never fitted it. Forced onto the ball, the
fit returns d=1.

**Cross-frame agreement cannot rescue it either**: agreement is *better* at the
controls (within-track IQR median 1.0 px, 74% <= 1 px) than at the seam (2.0 px,
38%), and the steadiest landmark in the corpus is a control reading d = 35.0 px
with IQR 0.0 over 25 looks where the truth is exactly 0.

**And the null as written here was not the safeguard it claimed to be.** The
guard reads `if ctl_ok and seam_rate < NULL_MARGIN * ctl_rate`: on a single
frame the controls frequently accept nothing, `ctl_ok` is empty, and the guard is
skipped altogether -- inert at precisely the sample size the button uses. A null
that a small sample can switch off is not a null. That is why the scale run and
the single-frame run disagreed about the same frame.

Any future attempt has to discriminate a **ghost** from a **step**, which is new
work with its own acceptance bar, not a parameter tweak. Per-candidate shards are
at `F:/archive/duo3_stitch/harvest/report_shards_dense/` (78 `.npz`) so it can
be re-analysed without re-decoding.

**The null runs on every call** -- `measure` always fits the control corridors
either side of the seam, *pretending the seam is there*, because otherwise
`a_pred` would refuse them for free and the null would be vacuous. Its numbers
are reported; they are what condemned the estimator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np

SEAM_X = 3840
BLEND_W = 128  # half-width; the mixed window is [seam-128, seam+128]

#: Chroma distance from the local pitch colour that marks a patch as "not
#: grass". Measured on the hand-verified frames: grass reads 1.4-2.8 and 2.8-3.2
#: at its worst (bright foreground), targets read 18.8-27.4. 8.0 sits well
#: inside the gap, on the conservative side -- it costs a "stand something in
#: the seam" prompt on a marginal target rather than admitting a patch of pitch.
MIN_LAB_DISTANCE = 8.0

#: How far from the seam a candidate may sit. +/-55 px keeps `a_pred` inside
#: [0.29, 0.71]: nearer the corridor edge there is too little mixing for a ghost
#: to exist, which is a statement about the blend, not about the estimator.
MAX_OFFSET = 55

#: The two-copy model has to earn its extra parameters against a single lobe.
#: On the hand-verified frames the true ghost gains 1.99 and the three negative
#: cases gain 1.14-1.19; control corridors gain 1.06-1.12.
MIN_GAIN = 1.5

#: Fitted `a` must agree with the geometric prediction this closely.
MAX_A_ERROR = 0.30

#: The seam must produce accepted measurements at this multiple of the control
#: corridors' rate. Measured on the archive, a well-posed frame runs 18-26% at
#: the seam against 0% at the controls, so this is not a tight squeeze.
NULL_MARGIN = 3.0

#: Largest IQR across accepted candidates that still counts as agreement.
#:
#: Deliberately tight, and the reason matters. Disparity scales with height
#: above the ground plane (the factory mesh nulls the ground), so a *walking
#: person* is not one measurement -- their boots, shorts and head sit at
#: different heights and legitimately disagree. Measured on the hand-verified
#: frame, bands on a walking figure return d = 18, 21, 25, 26, 28, 33; a looser
#: gate published their median, 25.5 px, where the hand-verified answer is
#: 17-19. So band agreement is not merely a nice-to-have discriminator here, it
#: is the only thing standing between the operator and a confident wrong number.
#: A target at a single height -- a board, a pole, a cone -- is what this gate
#: is asking for, and the remedy text says so.
MAX_SPREAD = 3.0

D_MIN, D_MAX = 4, 36


def blend_weight(x: float, seam_x: int = SEAM_X, blend_w: int = BLEND_W) -> float:
    """Weight of the LEFT contribution at column x. 1 at the left edge of the
    corridor, 0.5 at the seam, 0 at the right edge."""
    return float(np.clip(0.5 - (x - seam_x) / (2.0 * blend_w), 0.0, 1.0))


def colour_distance(bgr: np.ndarray, band_rows: int = 120) -> np.ndarray:
    """Chroma distance from the *local* pitch colour at the same rows.

    **Chroma only -- L is deliberately excluded.** Including lightness defeats
    the entire point: mown grass is a luminance texture, so an L-bearing
    distance rises with exactly the signal the gate exists to reject. Measured
    on the hand-verified frames, dropping L moves the ball-to-grass separation
    from 3.9x to 9.7x, and the worst case -- bright foreground grass, which is
    what fooled the first candidate ranking -- from an unusable 1.35x to 6.6x.

    Local rather than global: lighting and pitch colour change down the frame,
    and a global median would make the far touchline look distinct everywhere.
    """
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2Lab).astype(np.float32)
    out = np.empty(lab.shape[:2], np.float32)
    for y0 in range(0, lab.shape[0], band_rows):
        y1 = min(lab.shape[0], y0 + band_rows)
        med = np.median(lab[y0:y1].reshape(-1, 3), axis=0)
        out[y0:y1] = np.linalg.norm(lab[y0:y1, :, 1:] - med[1:], axis=2)
    return out


_A_GRID = np.arange(0.1, 0.91, 0.1, dtype=np.float32)
_SIGMAS = np.array([4.0, 7.0, 10.0, 13.0], np.float32)
#: How far the lobe centre may wander from the profile centre. This must stay
#: generous: narrowing it to +/-22 px to buy speed measurably *inflates* `gain`,
#: because a pinned centre handicaps the one-lobe reference fit more than the
#: two-copy one, and control corridors start passing. Speed is bought back on
#: the sigma grid instead, which has no such asymmetry.
_MU_REACH = 42


def _fit_two_copy(profile: np.ndarray) -> tuple[tuple, tuple] | tuple[None, None]:
    """Fit P(x) ~ A*[a*G(x-mu) + (1-a)*G(x-mu-d)] + c by grid search.

    Amplitude and offset are linear given the shape, so only (mu, sigma, d, a)
    are gridded, and the `a` axis is vectorised -- this runs per candidate on an
    operator's button press, so a readable-but-quadratic version is not good
    enough. `mu` is searched only near the profile centre because the chromatic
    scan has already localised the candidate to that column.

    Returns (best_two_copy, best_single_lobe); the caller compares their
    residuals, because a two-copy model that does not beat one lobe has found
    nothing.
    """
    n = len(profile)
    x = np.arange(n, dtype=np.float32)
    c0 = n // 2
    mus = np.arange(
        max(0, c0 - _MU_REACH), min(n, c0 + _MU_REACH) + 1, 1.0, dtype=np.float32
    )
    best: tuple | None = None
    single: tuple | None = None
    total = float(profile.sum())
    prof = profile[None, None, :]
    for sigma in _SIGMAS:
        two_s2 = 2.0 * sigma * sigma
        g1 = np.exp(-((x[None, :] - mus[:, None]) ** 2) / two_s2)
        for d in np.arange(0, D_MAX + 1, 1.0, dtype=np.float32):
            g2 = np.exp(-((x[None, :] - (mus[:, None] + d)) ** 2) / two_s2)
            if d == 0:
                model = g1[None]  # (1, mu, x)
                a_axis = np.array([1.0], np.float32)
            else:
                a = _A_GRID[:, None, None]
                model = a * g1[None] + (1.0 - a) * g2[None]
                a_axis = _A_GRID
            s1 = model.sum(axis=2)
            s2 = (model * model).sum(axis=2)
            sxy = (model * prof).sum(axis=2)
            den = n * s2 - s1 * s1
            den = np.where(np.abs(den) < 1e-9, 1e-9, den)
            amp = (n * sxy - s1 * total) / den
            off = (total - amp * s1) / n
            resid = prof - (amp[..., None] * model + off[..., None])
            rms = np.sqrt((resid * resid).mean(axis=2))
            rms = np.where(amp > 0, rms, np.inf)
            flat = int(np.argmin(rms))
            ai, mi = divmod(flat, rms.shape[1])
            if not np.isfinite(rms[ai, mi]):
                continue
            rec = (float(rms[ai, mi]), float(d), float(a_axis[ai]))
            if d == 0:
                if single is None or rec[0] < single[0]:
                    single = rec
            elif best is None or rec[0] < best[0]:
                best = rec
    return best, single  # type: ignore[return-value]


@dataclass
class Candidate:
    """One chromatically distinct patch, fitted. `lab` is the audit trail."""

    rows: tuple[int, int]
    x: int
    lab: float
    d: float
    a_fit: float
    a_pred: float
    gain: float
    accepted: bool = False
    reason: str = ""

    def to_api(self) -> dict[str, Any]:
        return {
            "rows": list(self.rows),
            "x": self.x,
            "lab": round(self.lab, 1),
            "d": round(self.d, 1),
            "a_fit": round(self.a_fit, 2),
            "a_pred": round(self.a_pred, 3),
            "gain": round(self.gain, 2),
            "accepted": self.accepted,
            "reason": self.reason,
        }


@dataclass
class EchoResult:
    verdict: str = "refused"  # withdrawn | refused | void
    #: What the estimator would have said. Reported as evidence, never as a
    #: proposal -- see the module docstring for why it is not trustworthy.
    provisional_dx: float | None = None
    dx: float | None = None  # always None: this estimator does not propose
    spread: float | None = None
    n_accepted: int = 0
    n_candidates: int = 0
    control_accepted: int = 0
    seam_rate: float = 0.0
    control_rate: float = 0.0
    remedy: str = ""
    candidates: list[Candidate] = field(default_factory=list)
    controls: list[Candidate] = field(default_factory=list)

    def to_api(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "dx": None,  # this estimator does not propose a value
            "provisional_dx": (
                None if self.provisional_dx is None else round(self.provisional_dx, 2)
            ),
            "spread": None if self.spread is None else round(self.spread, 2),
            "n_accepted": self.n_accepted,
            "n_candidates": self.n_candidates,
            "control_accepted": self.control_accepted,
            "seam_rate": round(self.seam_rate, 3),
            "control_rate": round(self.control_rate, 3),
            "remedy": self.remedy,
            "candidates": [c.to_api() for c in self.candidates[:40]],
            "controls": [c.to_api() for c in self.controls[:20]],
        }


def _scan(
    dist: np.ndarray,
    centre: int,
    *,
    seam_x: int,
    blend_w: int,
    band: int,
    half: int,
    min_lab: float,
    max_fits: int,
) -> list[Candidate]:
    """Fit every chromatically distinct band near `centre`.

    `centre` is the column the blend is *assumed* to be centred on. For the real
    measurement that is the seam; for the null it is a control corridor, so that
    the same geometry gate applies there instead of refusing for free.
    """
    out: list[Candidate] = []
    height, width = dist.shape
    c = centre
    lo, hi = c - MAX_OFFSET, c + MAX_OFFSET
    if lo < half or hi > width - half:
        return out
    # Rank the bands chromatically first and fit only the most distinct ones.
    # The fit is the expensive step and this runs on a button press; fitting
    # every band would spend most of the budget on pitch.
    ranked: list[tuple[float, int, int]] = []
    for y0 in range(0, height - band + 1, band):
        strip = dist[y0 : y0 + band, lo:hi]
        lab = float(np.percentile(strip, 98))
        if lab < min_lab:
            continue
        ranked.append((lab, y0, lo + int(np.argmax(strip.mean(axis=0)))))
    ranked.sort(reverse=True)
    for lab, y0, x in ranked[:max_fits]:
        profile = dist[y0 : y0 + band, x - half : x + half].mean(axis=0)
        best, single = _fit_two_copy(profile.astype(np.float32))
        if best is None or single is None:
            continue
        a_pred = blend_weight(x, seam_x=c, blend_w=blend_w)
        gain = single[0] / max(best[0], 1e-9)
        cand = Candidate(
            rows=(y0, y0 + band),
            x=int(x),
            lab=float(lab),
            d=best[1],
            a_fit=best[2],
            a_pred=a_pred,
            gain=gain,
        )
        if gain < MIN_GAIN:
            cand.reason = f"two-copy model gains only {gain:.2f} over one lobe"
        elif not (D_MIN <= cand.d < D_MAX):
            cand.reason = f"d={cand.d:.0f} px is at the edge of the search band"
        elif not 0.29 <= a_pred <= 0.71:
            cand.reason = f"a_pred={a_pred:.2f}: too little mixing this far out"
        elif abs(cand.a_fit - a_pred) > MAX_A_ERROR:
            cand.reason = (
                f"fitted a={cand.a_fit:.2f} disagrees with geometry {a_pred:.2f}"
            )
        else:
            cand.accepted = True
        out.append(cand)
    return out


def measure(
    frames: list[np.ndarray],
    *,
    seam_x: int = SEAM_X,
    blend_w: int = BLEND_W,
    band: int = 26,
    half: int = 70,
    min_lab: float = MIN_LAB_DISTANCE,
    control_offset: int = 500,
    max_fits: int = 10,
) -> EchoResult:
    """Measure `dx` at the seam from N frames, or refuse and say why.

    N frames over a couple of seconds are free -- the camera is on a tripod and
    the operator pressed a button -- and they are what turns a single lucky
    reading into an agreement test. Noise does not survive averaging across
    independent row bands and independent frames; a real ghost does.

    The control corridors are measured on every call, with the same geometry
    gate applied about their own centres. If any of them passes, the instrument
    is finding ghosts where there cannot be one and the result is **void**.
    """
    if not frames:
        raise ValueError("measure() needs at least one frame")
    result = EchoResult()
    for frame in frames:
        if frame.ndim != 3:
            raise ValueError(
                "seam echo needs colour frames; grass is separable "
                "from a target by colour, not by luminance"
            )
        dist = colour_distance(frame)
        kw = {
            "seam_x": seam_x,
            "blend_w": blend_w,
            "band": band,
            "half": half,
            "min_lab": min_lab,
            "max_fits": max_fits,
        }
        result.candidates += _scan(dist, seam_x, **kw)  # type: ignore[arg-type]
        for off in (-control_offset, control_offset):
            result.controls += _scan(dist, seam_x + off, **kw)  # type: ignore[arg-type]

    accepted = [c for c in result.candidates if c.accepted]
    ctl_ok = [c for c in result.controls if c.accepted]
    result.n_candidates = len(result.candidates)
    result.n_accepted = len(accepted)
    result.control_accepted = len(ctl_ok)

    # The null compares *rates*, not raw counts. There are far more control
    # candidates than seam ones (two corridors, and no shortage of structure
    # away from the seam), so a single control false positive would otherwise
    # void every measurement. What must hold is that the seam is clearly
    # distinguishable from a place where a ghost cannot exist.
    seam_rate = len(accepted) / max(result.n_candidates, 1)
    ctl_rate = len(ctl_ok) / max(len(result.controls), 1)
    result.seam_rate = seam_rate
    result.control_rate = ctl_rate
    if ctl_ok and seam_rate < NULL_MARGIN * ctl_rate:
        result.verdict = "void"
        result.remedy = (
            f"Control corridors produced measurements at {ctl_rate:.0%} against "
            f"the seam's {seam_rate:.0%}, where the true answer off the seam is "
            "zero. The scene is defeating the estimator, so no number is "
            "published."
        )
        return result
    if not result.candidates:
        result.verdict = "refused"
        result.remedy = (
            "Nothing in the seam is chromatically distinct from the pitch. "
            "Note that this estimator is withdrawn and never proposes a curve "
            "even when it does find something -- calibrate by hand."
        )
        return result
    if len(accepted) < 3:
        result.verdict = "refused"
        best_lab = max(c.lab for c in result.candidates)
        result.remedy = (
            f"{len(accepted)} of {len(result.candidates)} candidates cleared the "
            f"gates (brightest colour distance {best_lab:.0f}). This estimator is "
            "withdrawn and does not propose a curve in any case: it measures a "
            "step edge rather than a ghost. Calibrate by hand."
        )
        return result

    ds = np.array([c.d for c in accepted])
    spread = float(np.percentile(ds, 75) - np.percentile(ds, 25))
    if spread > MAX_SPREAD:
        result.verdict = "refused"
        result.spread = spread
        result.remedy = (
            f"The {len(accepted)} usable candidates disagree (IQR {spread:.1f} px, "
            f"lags {sorted(round(d) for d in ds)}). Nothing is published -- and "
            "note that agreement would not earn publication either: measured at "
            "scale, agreement is BETTER at the control corridors, where the true "
            "answer is zero. Calibrate by hand."
        )
        return result
    # Everything above this point still runs: the numbers are the evidence, and
    # a future attempt will want them. What does not happen is a proposal.
    result.provisional_dx = float(np.median(ds))
    result.spread = spread
    result.verdict = "withdrawn"
    result.remedy = (
        f"{len(accepted)} candidates agreed on {result.provisional_dx:.1f} px, but "
        "this estimator is withdrawn and does not propose anchors. It measures a "
        "step edge, not an echo: a shirt against grass is a step, two lobes fit a "
        "step better than one, and at scale it accepts off-seam corridors (6.3%) "
        "almost as often as the seam (7.4%) with the same d distribution, where "
        "the off-seam answer is exactly zero. Calibrate by hand."
    )
    return result


def anchors_from_measurement(
    result: EchoResult, rows: tuple[int, ...]
) -> list[tuple[float, float]]:
    """Turn a measurement into `dx_anchors` for the existing apply path.

    A flat curve, deliberately. The measurement establishes one `dx` at the rows
    the target happened to occupy; inventing a slope from that would be reading
    a roll model out of a single band. The operator can shape it by hand
    afterwards -- that is what the curve editor is for.
    """
    raise ValueError(
        "seam_echo is withdrawn and cannot propose anchors: it measures step "
        "edges rather than ghosts, and at scale it accepts control corridors "
        "almost as often as the seam. Calibrate by hand in /stitch. See the "
        "module docstring for the evidence."
    )
