"""Human-in-the-loop stitch-seam calibration, at ``/stitch``.

Workflow B of ``reolink-firmware-patching/docs/STITCH_CALIBRATION.md``: get a
panorama frame, let an operator slide the two halves into registration by eye
while the seam metric scores every candidate curve, and send the result back.
The curve the operator authors **is** the calibration artifact -- there is no
export step and no parameter that lives only in the UI.

**The client is a phone, held at the pitch side.** Mark's plan, in his words:
*"I will connect to the camera from my phone while on the field, so setting
this up in the camera manager app will work."* That is not a nice-to-have
responsive pass over a desktop tool; it decides the layout, the gestures and
the network behaviour. A pair of 640x2160 strips and a tall curve widget do not
fit a 390 px viewport by shrinking, a mouse-only drag is unreachable, and the
link at a field is whatever the field has. So: one primary canvas with pinch
zoom and an axis-locked drag, a thumb-sized roll control, everything else
folded away, every score request bounded by a timeout, and numbers that are
visibly stale the instant the curve moves rather than silently wrong.

Reaching it from a phone at all needs ``[TTT].auth_server_bind`` set to the
machine's LAN address -- the app's host allowlist is loopback plus that value
(``web/auth_server.py``), and ``0.0.0.0`` deliberately does *not* widen it. The
page says so where an operator will read it.

**The live `Snap` is the primary door.** The field loop is: tripod, snap,
adjust, apply, re-snap to confirm. Opening a frame from an archived game stays,
because a calibration can also be recovered from footage after the fact.

Six things about this file are load-bearing and easy to get wrong.

**Only one half moves, and it is the LEFT one.** The camera's warp mesh lives
in VPE 0, which warps the left half; measured on hardware (#135), a requested
+40 px moves the left half 40.13 px far from the seam and 40.94 px near it,
while the right half moves 0.02 px. So the UI draws the right half locked and
dimmed. Two draggable halves would be a nicer toy and a false model of the
hardware. The downstream corrector is the mirror image -- it rolls the *right*
half -- so selecting that surface flips which half is draggable and inverts the
displayed sense. The stored anchors never change; only the presentation does.

**The operator descends the same objective as the solver.** Every drag is
scored with `seam_metric`, not with a lookalike. Detection is the expensive
half -- 37-50 s on a real 7680x2160 game frame -- and runs once per frame in a
background thread; scoring a candidate curve reuses those chains and takes
~0.26 s. That is what makes a hand calibration and an automatic one directly
comparable instead of merely similar.

**A score that does not respond to the curve is not a measurement.** Measured
on three real tripod placements, SCR sits at 27-36 px and moves 1-15% across
the whole plausible dx range while the seam is visibly registered, and every
coverage gate passes while it happens. So each frame is swept once and the
verdict -- can this picture steer the calibration at all -- leads the panel,
ahead of any number. See `_frame_quality`.

**The calibration target is a person standing in the seam.** Mark: *"it's easy
to see misregistration if there's a person in the seam."* That is the answer to
the anomaly above, and the mechanism is in `seam_vertical`: the seam is a
vertical line, SCR is built from *near-horizontal* structures, and a horizontal
edge is invariant under a horizontal shift. Touchlines, treelines and painted
banners therefore supply observation after observation carrying no information
about the quantity being tuned. A person is upright -- torso, legs, head -- and
is exactly what a horizontal error breaks. So "have someone stand in the seam"
is a *step* in this interface, not a tip, the tool checks whether they actually
are in it before believing any number, and the reported residual leads with the
subset of structures that can see `dx` at all.

**Two surfaces, and only one of them is a measurement.**

*The layer pair* (`/stitch/layers`) is the two sensors' own contributions to the
overlap, pulled *before* the camera cross-fades them. Both are already resampled
into the panorama's output frame, so an L-to-R displacement measured there is
residual disparity in output pixels and converts straight to `dx_anchors` -- no
lens model, no homography, no rescale. Overlay, difference, anaglyph and blink on
this pair are **exact**. This is where the gestures bind.

*The fused `Snap`* is the older surface and is still a mixture over the 256-px
blend window, so blink and anaglyph there draw the *fitted* structures -- the
same extrapolation the metric performs -- rather than smearing replicated pixels
across a window that has no second layer in it. That distinction is stated in the
interface, not just here: an honest caveat in a source comment is a caveat nobody
reads.

*The whole-lens views* (`/stitch/sensors`) are a third thing and are **context
only**. They come back at each sensor's native width, which puts them in sensor
coordinates, before the warp; a displacement there is not a seam correction. The
UI refuses to author a curve from them, on the descriptor's `authoritative` flag
rather than on a caption.

What crosses the wire for the fused view is two 640x2160 JPEG strips (~76 KB
each), not the 730 KB panorama: the operator only ever looks at the seam, and the
browser can shear a strip with an affine transform far more smoothly than a
server can re-render one per drag frame. The layer pair is PNG, because the
difference view subtracts one layer from the other and JPEG ringing at the very
edges being aligned would read as residual that no drag can null.
"""

from __future__ import annotations

import importlib
import json
import logging
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response

from video_grouper.utils.config import CameraConfig, load_config
from video_grouper.utils.stitch_remap import (
    SeamCalibrationError,
    build_v2_profile,
    read_dx_anchors,
)

logger = logging.getLogger(__name__)

# Panorama geometry for the Duo 3, measured from /proc/hdal/vprc/info: two
# 3840-wide halves butt-joined at x=3840 with no crop, blended over 2*blend_w.
PANORAMA_W = 7680
PANORAMA_H = 2160
SEAM_X = PANORAMA_W // 2
BLEND_W = 128  # half-width; the mixed window is [SEAM-128, SEAM+128]

# How much of each half to ship to the browser. 640 px reaches well past the
# 384-px shoulder the metric fits on, so the operator sees the structure the
# score is computed from, and costs ~76 KB per half at q78.
STRIP_W = 640
STRIP_QUALITY = 78

# Rows the anchor curve is authored at. Five handles, evenly spread, matching
# the design's worked example. A relative lens roll is linear in y, so five is
# already more freedom than the physics needs -- the extra handles exist to
# show the operator that the model *would* admit more, and to record it if the
# camera turns out not to obey the roll model.
ANCHOR_ROWS = (0, 540, 1080, 1620, 2159)

_MAX_ABS_DX = 64.0  # lut2d refuses beyond this; refuse here too rather than later


def _vpe_dir() -> Path | None:
    """Locate the firmware-side calibration toolkit.

    It lives outside the installed package (``reolink-firmware-patching/`` is
    firmware territory and is excluded from mypy and from the wheel), so it is
    found by path rather than imported as a module. Two layouts: a source
    checkout, and a PyInstaller bundle that carried the directory in as data.
    """
    here = Path(__file__).resolve()
    candidates = [
        here.parents[2] / "reolink-firmware-patching" / "vpe",
        Path(getattr(sys, "_MEIPASS", "")) / "vpe"
        if hasattr(sys, "_MEIPASS")
        else None,
    ]
    for candidate in candidates:
        if candidate is not None and (candidate / "seam_metric.py").exists():
            return candidate
    return None


_toolkit_cache: dict[str, Any] = {}


def load_toolkit() -> dict[str, Any]:
    """Import `seam_metric` / `seam_vertical` / `stitch_apply`, or explain why not.

    Returns a dict with ``metric``, ``vertical`` and ``camera`` (module or None)
    plus ``errors``. The page degrades rather than 500s: the metric and the
    camera surface fail independently, and an operator with neither can still
    read the documentation the page carries.
    """
    if _toolkit_cache:
        return _toolkit_cache
    out: dict[str, Any] = {
        "metric": None,
        "camera": None,
        "vertical": None,
        "echo": None,
        "layers": None,
        "errors": [],
    }
    vpe = _vpe_dir()
    if vpe is None:
        out["errors"].append(
            "the seam-calibration toolkit (reolink-firmware-patching/vpe) is not "
            "on this install -- calibration needs a source checkout"
        )
        _toolkit_cache.update(out)
        return out
    if str(vpe) not in sys.path:
        sys.path.insert(0, str(vpe))
    for key, name in (
        ("metric", "seam_metric"),
        ("vertical", "seam_vertical"),
        ("echo", "seam_echo"),
        ("camera", "stitch_apply"),
        ("layers", "seam_layers"),
    ):
        try:
            out[key] = importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 -- any import failure degrades
            out["errors"].append(f"{name}: {exc}")
    _toolkit_cache.update(out)
    return out


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


@dataclass
class _Session:
    """One operator, one snapshot. Loopback tool; no multi-tenancy.

    Held in memory rather than on disk because the expensive part -- the
    detected shoulder chains -- is only meaningful for the exact frame it came
    from, and a stale cache scored against a fresh frame would silently report
    the wrong numbers.
    """

    camera_name: str = ""
    host: str = ""
    source_path: str = ""
    snapped_at: float = 0.0
    version: int = 0
    frame: Any = None  # np.ndarray, full panorama BGR
    halves: dict[str, bytes] = field(default_factory=dict)
    width: int = 0
    height: int = 0
    scalars_current: dict | None = None
    scalars_factory: dict | None = None
    chains: Any = None
    chains_state: str = "idle"  # idle | running | ready | failed
    chains_error: str = ""
    baseline: dict | None = None
    quality: dict | None = None
    last_apply: dict | None = None
    # The aiming loop, which is cheap and runs ahead of the expensive one: a
    # downscaled whole-panorama JPEG plus "is anything upright in the seam
    # corridor". Kept separate from the calibration frame so pressing Aim
    # repeatedly -- while someone walks into the seam -- never disturbs a
    # session that is already scored.
    scene: bytes = b""
    scene_version: int = 0
    scene_at: float = 0.0
    scene_source: str = ""
    scene_vertical: dict | None = None
    vertical: dict | None = None
    # The automatic seam measurement (`seam_echo`). Kept beside the manual
    # numbers rather than replacing them: the operator reviews a proposal, and
    # a measurement that refuses must still say so on the page.
    auto: dict | None = None
    auto_state: str = "idle"  # idle | running | ready | failed
    auto_error: str = ""
    # What is actually installed on the camera. The editor starts from this
    # instead of from a flat zero curve, so an operator's first adjustment is a
    # delta from reality rather than from nothing.
    camera_cal: dict | None = None
    camera_cal_state: str = "idle"  # idle | running | ready | failed
    camera_cal_error: str = ""
    # The two separated sensor layers over the overlap, and the panorama
    # columns they occupy. Versioned independently of the fused snapshot
    # because they arrive by a different door and neither implies the other:
    # an operator can align layers with no `Snap` at all, and a `Snap` says
    # nothing about the pair.
    layers: dict[str, bytes] = field(default_factory=dict)
    layers_desc: dict | None = None
    layers_version: int = 0
    layers_at: float = 0.0
    layers_error: str = ""
    # Whole-lens context views, in SENSOR coordinates. Held separately from
    # `layers` and never merged into them: these cannot produce a calibration,
    # and one dict holding both kinds is how that distinction would get lost.
    sensors: dict[str, bytes] = field(default_factory=dict)
    sensors_desc: dict | None = None
    sensors_version: int = 0

    lock: threading.Lock = field(default_factory=threading.Lock)


_session = _Session()


def _reolink_camera(config_path: Path) -> CameraConfig:
    config = load_config(config_path)
    if config is None:
        raise HTTPException(500, f"could not load config from {config_path}")
    cameras = [c for c in config.cameras if c.enabled]
    for cam in cameras:
        if cam.type == "reolink":
            return cam
    if cameras:
        raise HTTPException(
            400,
            "seam calibration needs a Reolink dual-lens camera; the configured "
            f"camera(s) are {', '.join(c.type for c in cameras)}",
        )
    raise HTTPException(400, "no enabled camera is configured")


def _camera_host(cam: CameraConfig) -> str:
    return cam.device_ip if cam.http_port == 80 else f"{cam.device_ip}:{cam.http_port}"


def _encode_halves(frame: Any, seam_x: int) -> dict[str, bytes]:
    """Cut the two seam strips and JPEG them.

    Sent as two images rather than one so the browser can shear them
    independently -- which is the whole trick that keeps dragging smooth
    without a round trip per frame.
    """
    import cv2

    lo = max(0, seam_x - STRIP_W)
    hi = min(frame.shape[1], seam_x + STRIP_W)
    params = [cv2.IMWRITE_JPEG_QUALITY, STRIP_QUALITY]
    out = {}
    for side, sl in (("left", slice(lo, seam_x)), ("right", slice(seam_x, hi))):
        ok, buf = cv2.imencode(".jpg", frame[:, sl], params)
        if not ok:
            raise HTTPException(500, f"could not encode the {side} strip")
        out[side] = buf.tobytes()
    return out


#: The whole panorama, small enough to cross a field's worth of link. 960 px of
#: a 7680 px frame is 1/8 scale -- useless for judging registration and exactly
#: right for judging *aim*: where the seam falls in the scene, and whether the
#: person you asked to stand in it is standing in it.
SCENE_W = 960
SCENE_QUALITY = 68


def _encode_scene(frame: Any) -> bytes:
    import cv2

    h, w = frame.shape[:2]
    small = cv2.resize(
        frame, (SCENE_W, max(1, round(h * SCENE_W / w))), interpolation=cv2.INTER_AREA
    )
    ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, SCENE_QUALITY])
    if not ok:
        raise HTTPException(500, "could not encode the scene overview")
    return bytes(buf.tobytes())


#: How much context outside the overlap to ship with each layer.
#:
#: The overlap is the only region where both layers exist, and therefore the
#: only region a registration measure can read -- but an operator dragging a
#: layer needs somewhere for content to come *from*, or the image runs out at
#: the edge of the gesture. With the strip source there is no context to be had
#: (the layers are exactly the overlap) and this clips to nothing. With full
#: per-sensor frames it bounds the payload: 512 px per layer instead of 3840,
#: without the UI knowing which source it got.
LAYER_CONTEXT = 192

#: PNG, not JPEG, and this is not a preference. The difference view subtracts
#: one layer from the other, so JPEG ringing at exactly the high-contrast edges
#: the operator is aligning to would show up as residual that no drag can null.
#: A 128x2160 greyscale PNG is ~60 KB; the honesty is free.
LAYER_MAX_SERVE_W = 1024


def _encode_layers(pair: Any) -> tuple[dict[str, bytes], dict]:
    """PNG each layer over a bounded window, and say which columns were sent.

    Returns (images, descriptor). The descriptor is the whole contract with the
    browser: per side, the panorama columns actually served. Nothing downstream
    may assume those match the layer's full extent, because for a full-frame
    source they will not.
    """
    import cv2
    import numpy as np

    lo, hi = pair.overlap
    want_lo, want_hi = lo - LAYER_CONTEXT, hi + LAYER_CONTEXT
    desc = pair.to_api()
    out: dict[str, bytes] = {}
    for side in ("left", "right"):
        img = pair.left if side == "left" else pair.right
        x0 = pair.left_x0 if side == "left" else pair.right_x0
        sx0 = max(x0, want_lo)
        sx1 = min(x0 + img.shape[1], want_hi)
        if sx1 - sx0 > LAYER_MAX_SERVE_W:  # centre the cap on the overlap
            mid = (lo + hi) // 2
            sx0 = max(sx0, mid - LAYER_MAX_SERVE_W // 2)
            sx1 = min(sx1, sx0 + LAYER_MAX_SERVE_W)
        crop = np.ascontiguousarray(img[:, sx0 - x0 : sx1 - x0])
        ok, buf = cv2.imencode(".png", crop)
        if not ok:
            raise HTTPException(500, f"could not encode the {side} layer")
        out[side] = bytes(buf.tobytes())
        desc[side]["served"] = {"x0": int(sx0), "x1": int(sx1), "w": int(sx1 - sx0)}
    desc["context"] = LAYER_CONTEXT
    return out, desc


def _vertical_profile(frame: Any, seam_x: int) -> dict | None:
    """Where upright structure crosses the seam corridor, if anywhere.

    ~60 ms on a 7680x2160 frame against the 37-50 s SCR detection, which is why
    it can run on every aiming snapshot: the operator finds out whether the
    person is in the seam *before* committing to a measurement.
    """
    vertical = load_toolkit().get("vertical")
    if vertical is None:
        return None
    try:
        profile = vertical.vertical_structure(frame, seam_x=seam_x, blend_w=2 * BLEND_W)
        return vertical.summarise(profile)
    except Exception as exc:  # noqa: BLE001 -- an aiming aid never 500s
        logger.warning("STITCH: vertical-structure scan failed: %s", exc)
        return None


def _target_band(
    upright: dict | None, height: int, pad: int = 40
) -> tuple[int, int] | None:
    """The rows the calibration target actually occupies, padded.

    SSR over the whole field band answers "is the seam damaged on average",
    and mid-field average is grass, which a misregistration cannot damage
    because there is nothing there to break. Measured on a real frame with a
    player straddling the seam: |ln SSR| over the field band reads 0.062 --
    below the 0.10 noise floor, i.e. indistinguishable from a perfect seam --
    while over the 285 rows this function hands back for that player it reads
    1.180, and over the strongest 183-row band alone 2.188. Across 87
    frames sampled from the archived set, the whole-band figure cannot tell the
    two cases apart at all (median 0.095 with upright structure in the corridor
    against 0.088 without), while the banded figure separates them by an order
    of magnitude (1.889 against 0.170).

    One honest limit, and the UI states it: SSR is a ratio against a background
    fitted on the *shoulders*, so an object that exists only inside the window
    raises it whether or not it is misregistered (section 9.2 says the same of
    the live frame at 0.3 m). This is therefore a before/after comparator on a
    fixed scene -- the same person standing in the same place across an Apply
    and a re-snap -- not an absolute measure of damage.
    """
    if not upright or not upright.get("rows"):
        return None
    best = upright.get("best_rows")
    rows = [tuple(r) for r in upright["rows"]]
    chosen = rows[0]
    if best:
        for r in rows:
            if r[0] <= best[0] < r[1]:
                chosen = r
                break
    lo, hi = max(0, chosen[0] - pad), min(height, chosen[1] + pad)
    return (lo, hi) if hi - lo >= 64 else None


def _detect_chains_async(session: _Session, version: int) -> None:
    """Run the expensive SCR detection off the request thread.

    37-50 s on a real 7680x2160 game frame, plus the dx sweep. The operator
    gets the picture immediately and
    the numbers when they are ready; blocking the snapshot on this would make
    the tool feel broken on the one action it always starts with.
    """
    metric = load_toolkit()["metric"]
    if metric is None:
        return
    try:
        with session.lock:
            if session.version != version:
                return
            frame = session.frame
            seam = session.width // 2
        chains = metric.detect_shoulder_chains(
            frame, seam_x=seam, blend_w=2 * BLEND_W, shoulder_w=384
        )
        scr = metric.residual_from_chains(chains)
        ssr = metric.seam_sharpness_ratio(frame, seam_x=seam, blend_w=2 * BLEND_W)
        with session.lock:
            upright = session.vertical
        band = _target_band(upright, frame.shape[0])
        ssr_target = (
            None
            if band is None
            else metric.seam_sharpness_ratio(
                frame, seam_x=seam, blend_w=2 * BLEND_W, band=band
            )
        )
        quality = _frame_quality(metric, chains, frame.shape[0], upright)
    except Exception as exc:  # noqa: BLE001 -- surfaced to the operator instead
        logger.warning("STITCH: baseline detection failed: %s", exc)
        with session.lock:
            if session.version == version:
                session.chains_state = "failed"
                session.chains_error = str(exc)
        return
    with session.lock:
        if session.version != version:
            return
        session.chains = chains
        session.chains_state = "ready"
        session.baseline = _score_payload(scr, ssr, ssr_target, band)
        session.quality = quality


#: `seam_continuity_residual`'s pair-matching tolerance. An observation whose
#: residual sits near it was accepted at the limit of the gate, which on a busy
#: outdoor frame usually means two unrelated edges got paired.
_MATCH_GAP_PX = 40.0

#: Below this, sliding the curve does not move the score enough to steer by.
#: Set against the acceptance gate, which wants SCR p90 halved: a frame that
#: cannot produce even a fifth of that over the whole plausible dx range cannot
#: produce the rest either.
_MIN_USEFUL_GAIN = 0.20


def _frame_quality(
    metric: Any, chains: Any, height: int, upright: dict | None = None
) -> dict:
    """Ask the frame whether it can constrain the calibration at all, and if
    not, say what to change about the picture.

    This exists because of what real material does. On three separate Duo 3
    tripod placements -- an indoor dome and two outdoor pitches -- SCR reports
    p90 between 27 and 36 px and moves by 1-15% as dx sweeps the whole
    plausible range, while the seam is *visibly* well registered. Every
    coverage gate passes (40-69 structures, 3 row bands, 69-79% height), so
    nothing in the acceptance check flags it.

    Two things are happening, and only the second one has a remedy.

    The matcher pairs a left structure with any right structure within 40 px at
    the seam, and a busy scene at mixed depths offers plenty; those pairs are
    unrelated edges whose residuals describe the matching tolerance.

    And underneath that, the structures being paired are the wrong shape. SCR
    fits *near-horizontal* lines, and `r_y = -m*dx` means a horizontal line is
    invariant under the horizontal shift being tuned. Measured across the
    archived Duo 3 frame set on three games (see `seam_vertical`): of 11, 61 and
    88 accepted structures, 0, 1 and 2 respectively were steeper than |m| = 0.15,
    and restricting the dx sweep to the subset that can see dx at all moved the
    p90 by 1.3-2.9% -- no better than the full set. Re-weighting does not rescue
    these frames. Only different structure does, which is why the remedy this
    returns is an instruction about the scene rather than a smaller number:
    **put a person in the seam.** A person is upright, and upright is precisely
    what a horizontal error breaks.
    """
    sweep = []
    for dx in range(-16, 17, 2):
        r = metric.residual_from_chains(
            chains, [(0.0, float(dx)), (height - 1.0, float(dx))]
        )
        if r.n:
            sweep.append({"dx": dx, "p50": round(r.p50, 3), "p90": round(r.p90, 3)})

    base = metric.residual_from_chains(chains)
    split = _sensitivity_split(base.observations)
    has_upright = bool(upright and upright.get("n_with_structure"))
    upright_rows = (upright or {}).get("rows") or []

    if not sweep:
        return {
            "usable": False,
            "reason": "no structure crosses the seam at all",
            "remedy": _PERSON_REMEDY,
            "has_upright": has_upright,
            "upright_rows": upright_rows,
            "split": split,
            "sweep": [],
        }

    at_zero = next((s for s in sweep if s["dx"] == 0), sweep[0])
    best = min(sweep, key=lambda s: s["p90"])
    gain = 0.0 if at_zero["p90"] <= 0 else 1.0 - best["p90"] / at_zero["p90"]

    saturated = [o for o in base.observations if o.residual_perp > 0.8 * _MATCH_GAP_PX]
    sat_frac = len(saturated) / base.n if base.n else 0.0

    usable = gain >= _MIN_USEFUL_GAIN
    remedy = ""
    if usable:
        reason = (
            f"sliding dx across +/-16 px moves SCR p90 by {gain * 100:.0f}%, "
            f"best at dx={best['dx']:+d}"
        )
    elif split["n_steering"] == 0:
        reason = (
            f"every one of the {split['n_total']} structures crossing the seam is "
            "within 3 degrees of horizontal, and a horizontal edge does not move "
            "when the halves shift horizontally -- SCR here is measuring its own "
            "pairing tolerance"
        )
        remedy = _PERSON_REMEDY
    elif sat_frac > 0.4:
        reason = (
            f"{sat_frac * 100:.0f}% of matched structures sit near the "
            f"{_MATCH_GAP_PX:.0f} px pairing limit, so the score is dominated by "
            "unrelated edges paired across the seam, not by misregistration"
        )
        remedy = _PERSON_REMEDY
    else:
        reason = (
            f"sliding dx across the whole plausible range moves SCR p90 by only "
            f"{gain * 100:.0f}%; the steepest structure here converts a shift into "
            f"only {split['max_sensitivity']:.2f} px of residual per px"
        )
        remedy = _PERSON_REMEDY
    return {
        "usable": usable,
        "reason": reason,
        "remedy": remedy,
        "has_upright": has_upright,
        "upright_rows": upright_rows,
        "split": split,
        "best_dx": best["dx"] if usable else None,
        "gain": round(gain, 4),
        "saturated_fraction": round(sat_frac, 3),
        "sweep": sweep,
    }


#: The one instruction that changes the answer. Everything else -- re-weighting,
#: tighter gates, a better regression -- is arithmetic on structure that cannot
#: see the quantity being measured.
_PERSON_REMEDY = (
    "Have someone stand in the seam, out where play actually happens rather "
    "than beside the tripod, and snap again. A person is upright, which is the "
    "one thing a horizontal misregistration visibly breaks."
)


def _sensitivity_split(observations: Any) -> dict:
    """How many of these structures can see a horizontal shift at all."""
    vertical = load_toolkit().get("vertical")
    if vertical is None or not observations:
        return {
            "n_total": len(observations or []),
            "n_steering": 0,
            "blind_fraction": 1.0,
            "p90_steering": None,
            "p90_blind": None,
            "median_sensitivity": 0.0,
            "max_sensitivity": 0.0,
        }
    return vertical.summarise(
        vertical.VerticalProfile(), vertical.split_by_dx_sensitivity(observations)
    )["scr_split"]


def _score_payload(
    scr: Any,
    ssr: Any,
    ssr_target: Any = None,
    target_band: tuple[int, int] | None = None,
) -> dict:
    return {
        "scr": {
            "n": scr.n,
            "p50": None if scr.n == 0 else round(scr.p50, 3),
            "p90": None if scr.n == 0 else round(scr.p90, 3),
            "max": None if scr.n == 0 else round(scr.max, 3),
            "row_bands_covered": scr.row_bands_covered,
            "height_coverage": round(scr.height_coverage, 3),
            "slope_spread": round(scr.slope_spread, 4),
            # Negated: `implied_dx` is the misregistration present in the
            # image, which is the OPPOSITE sense to a dx_anchors value. Handing
            # the raw number to an operator who then types it into the curve is
            # the sign error of section 4.4, applied twice instead of removed.
            "suggested_dx": (
                None
                if scr.n < 3 or scr.slope_spread <= 0.02
                else round(-scr.implied_dx, 3)
            ),
        },
        # How much of that residual comes from structure that can actually see
        # a horizontal shift. Reported beside the headline number rather than
        # folded into it: silently redefining SCR would leave this tool and the
        # automatic solver minimising two different things under one name.
        "split": _sensitivity_split(scr.observations),
        "ssr": {
            "ssr": round(ssr.ssr, 4),
            "abs_ln_ssr": round(ssr.abs_ln_ssr, 4),
            "noise_floor": ssr.noise_floor,
        },
        # The same metric, restricted to the rows the calibration target
        # occupies. Grass cannot be broken by a misregistration, so averaging
        # over a field band of it hides the damage: 0.062 whole-band against
        # 1.180 on the player's rows, on one real frame verified in the UI.
        "ssr_target": (
            None
            if ssr_target is None
            else {
                "abs_ln_ssr": round(ssr_target.abs_ln_ssr, 4),
                "band": list(target_band or ssr_target.band),
            }
        ),
        # The structures the score is computed from, so the picture and the
        # number are the same evidence. Each is one structure fitted
        # independently on the two shoulders and extrapolated to the seam;
        # `y_left` and `y_right` are where those two extrapolations arrive.
        # Drawing them IS the "alternate the two extrapolated shoulders" view,
        # and it is honest in a way extrapolating pixels is not: the metric
        # really does extrapolate lines, and a line drawn on screen cannot be
        # mistaken for photographic evidence the fused frame does not contain.
        # `sens` is px of residual per px of horizontal shift -- the structure's
        # sine from horizontal. The view draws near-zero ones muted, because a
        # bright confident line that cannot respond to the control the operator
        # is holding is a lie told in a picture.
        "observations": [
            {
                "y_left": round(o.y_left, 2),
                "y_right": round(o.y_right, 2),
                "slope": round(o.slope, 5),
                "residual": round(o.residual_perp, 2),
                "sens": round(abs(o.slope) / (1.0 + o.slope * o.slope) ** 0.5, 3),
            }
            for o in sorted(scr.observations, key=lambda o: o.fit_rms)[:60]
        ],
    }


def _anchors_from_body(body: dict) -> list[tuple[float, float]]:
    try:
        anchors = read_dx_anchors(body)
    except SeamCalibrationError as exc:
        raise HTTPException(400, str(exc)) from exc
    worst = max(abs(dx) for _y, dx in anchors)
    if worst > _MAX_ABS_DX:
        # The camera-side composer refuses beyond this and calls it a corrupt
        # file. Refuse at the point the number is authored instead, where the
        # operator can still see what they did.
        raise HTTPException(
            400,
            f"|dx| reaches {worst:.1f} px; nothing physical needs more than "
            f"{_MAX_ABS_DX:.0f} px and the camera-side composer refuses it",
        )
    return anchors


def _frame_from_video(source: Path, seconds: float) -> Any:
    """Pull one frame out of a recording, `seconds` in.

    PyAV rather than a subprocess: it is the project's convention, and it lets
    a 7680x2160 HEVC file be seeked and decoded without a temp file.
    """
    import av
    import numpy as np

    with av.open(str(source)) as container:
        stream = container.streams.video[0]
        if seconds > 0 and stream.time_base:
            container.seek(
                int(seconds / float(stream.time_base)), stream=stream, backward=True
            )
        for frame in container.decode(stream):
            return np.ascontiguousarray(frame.to_ndarray(format="bgr24"))
    raise HTTPException(415, f"no decodable video frame in {source.name}")


def _adopt_frame(
    frame: Any,
    *,
    camera_name: str,
    host: str,
    scalars: tuple[dict | None, dict | None],
    source_path: str,
) -> JSONResponse:
    """Make `frame` the session's subject, and start scoring it.

    The one path by which a frame becomes the thing being calibrated, so a
    live snapshot and a still from a recorded game land in exactly the same
    editor with exactly the same measurement behind them. Detection runs off
    the request thread because it takes 37-50 s on a real 7680x2160 frame and
    the operator should be looking at the picture long before the numbers do.
    """
    metric = load_toolkit()["metric"]
    seam_x = frame.shape[1] // 2
    halves = _encode_halves(frame, seam_x)
    scene = _encode_scene(frame)
    upright = _vertical_profile(frame, seam_x)
    with _session.lock:
        _session.version += 1
        version = _session.version
        _session.camera_name = camera_name
        _session.host = host
        _session.source_path = source_path
        _session.frame = frame
        _session.halves = halves
        _session.scene = scene
        _session.scene_version = version
        _session.scene_at = time.time()
        _session.scene_source = source_path or "camera"
        _session.scene_vertical = upright
        _session.vertical = upright
        _session.height, _session.width = frame.shape[0], frame.shape[1]
        _session.snapped_at = time.time()
        _session.scalars_current, _session.scalars_factory = scalars
        _session.chains = None
        _session.baseline = None
        _session.quality = None
        _session.chains_error = ""
        _session.chains_state = "running" if metric is not None else "failed"
        if metric is None:
            _session.chains_error = "seam_metric is not importable"
        payload = _state_payload()

    if metric is not None:
        threading.Thread(
            target=_detect_chains_async,
            args=(_session, version),
            daemon=True,
            name="stitch-detect",
        ).start()
    return JSONResponse(payload)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def build_router(config_path: Path, storage_path: Path | None = None) -> APIRouter:
    """Build the FastAPI router for the seam-calibration tool.

    Host allowlist and Origin/Referer CSRF defenses come from the parent app's
    middleware, exactly as for the config editor.
    """
    router = APIRouter()
    storage = Path(storage_path) if storage_path else config_path.parent

    @router.get("/stitch", response_class=HTMLResponse)
    def get_page() -> HTMLResponse:
        toolkit = load_toolkit()
        return HTMLResponse(_render_page(toolkit))

    @router.get("/stitch/state")
    def get_state() -> JSONResponse:
        with _session.lock:
            return JSONResponse(_state_payload())

    def _live_frame() -> tuple[Any, CameraConfig, str]:
        """One `Snap` off the camera, decoded. Read-only, every time."""
        toolkit = load_toolkit()
        camera_mod = toolkit["camera"]
        if camera_mod is None:
            raise HTTPException(503, "; ".join(toolkit["errors"]) or "toolkit missing")
        cam = _reolink_camera(config_path)
        host = _camera_host(cam)

        import tempfile

        import cv2

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "snap.jpg"
            try:
                camera_mod.snap(host, cam.username, cam.password, path)
            except Exception as exc:  # noqa: BLE001 -- network/camera errors vary
                raise HTTPException(502, f"Snap failed: {exc}") from exc
            frame = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if frame is None:
            raise HTTPException(502, "the camera returned something that is not a JPEG")
        return frame, cam, host

    @router.post("/stitch/aim")
    def post_aim() -> JSONResponse:
        """Where does the seam fall in the scene, and is anyone standing in it?

        The step before the calibration snapshot, and the reason it is a
        separate endpoint: it must be cheap enough to press repeatedly while
        someone walks into position. A downscaled panorama (~35 KB) plus a
        60 ms upright-structure scan, and nothing that touches the scored
        session -- pressing this cannot lose a calibration in progress.

        A phone at a pitch cannot see a live video stream from a camera it is
        not on the same link as, and does not need to: what the operator has to
        establish before spending 40 s on detection is only *where the seam
        lands in the scene* and *whether the target is in it*.
        """
        frame, cam, host = _live_frame()
        seam_x = frame.shape[1] // 2
        scene = _encode_scene(frame)
        upright = _vertical_profile(frame, seam_x)
        with _session.lock:
            _session.scene = scene
            _session.scene_version += 1
            _session.scene_at = time.time()
            _session.scene_source = f"aim {host}"
            _session.scene_vertical = upright
            if not _session.camera_name:
                _session.camera_name = cam.name
                _session.host = host
            payload = _state_payload()
        payload["aim"] = {
            "width": frame.shape[1],
            "height": frame.shape[0],
            "seam_x": seam_x,
            "vertical": upright,
        }
        return JSONResponse(payload)

    @router.get("/stitch/scene.jpg")
    def get_scene(v: int = 0) -> Response:
        with _session.lock:
            data, version = _session.scene, _session.scene_version
        if not data:
            raise HTTPException(404, "no scene overview yet")
        del v  # cache-buster only
        return Response(
            data,
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store", "X-Stitch-Scene": str(version)},
        )

    @router.post("/stitch/snap")
    def post_snap() -> JSONResponse:
        """Fetch a fresh still and the camera's own scalars, in one action.

        `GetStitch` returns both the live values and the factory `initial`
        block, so "confirm what the camera's auto-adjustment already found" is
        a real comparison rather than a leap of faith.
        """
        frame, cam, host = _live_frame()
        camera_mod = load_toolkit()["camera"]
        try:
            current, factory = camera_mod.get_stitch(host, cam.username, cam.password)
            scalars = (current.to_api(), factory.to_api())
        except Exception as exc:  # noqa: BLE001
            logger.warning("STITCH: GetStitch failed: %s", exc)
            scalars = (None, None)

        return _adopt_frame(
            frame,
            camera_name=cam.name,
            host=host,
            scalars=scalars,
            source_path="",
        )

    @router.post("/stitch/open")
    def post_open(body: dict = Body(default={})) -> JSONResponse:
        """Load a frame from a recorded game instead of from the live camera.

        This is the door most calibrations will actually come through. Nobody
        stands at the touchline with a laptop mid-match, and a camera sitting
        on a bench indoors is looking at a scene 0.3 m away -- where parallax
        is tens of pixels and swamps the lens roll this tool exists to correct
        (see 12.1). A still from the game, at tripod distance and with field
        lines crossing the seam, is the representative input.

        The consequence for the artifact is that the calibration belongs to a
        *deployment* -- one tripod placement -- not to the camera for all time.
        `deployment` is recorded so that stays visible whichever way the
        per-game question falls.
        """
        raw = str(body.get("path") or "").strip()
        if not raw:
            raise HTTPException(400, "no path given")
        source = Path(raw)
        if not source.is_file():
            raise HTTPException(404, f"no such file: {source}")

        import cv2

        suffix = source.suffix.lower()
        if suffix in (".mp4", ".mkv", ".mov", ".dav", ".ts"):
            frame = _frame_from_video(source, float(body.get("seconds") or 0.0))
        else:
            frame = cv2.imread(str(source), cv2.IMREAD_COLOR)
        if frame is None:
            raise HTTPException(415, f"could not decode a frame from {source}")
        if frame.shape[1] < 4 * BLEND_W:
            raise HTTPException(
                415,
                f"{source.name} is {frame.shape[1]} px wide -- too narrow to "
                "carry a seam with shoulders either side of it",
            )
        return _adopt_frame(
            frame,
            camera_name=str(body.get("deployment") or source.stem),
            host=f"file:{source.name}",
            scalars=(None, None),
            source_path=str(source),
        )

    @router.get("/stitch/frames")
    def get_frames(dir: str = "") -> JSONResponse:  # noqa: A002 -- query name
        """List candidate frames in a directory, so the operator picks a name
        rather than typing a path. Read-only, and it only ever lists."""
        if not dir.strip():
            raise HTTPException(400, "no directory given")
        root = Path(dir.strip())
        if not root.is_dir():
            raise HTTPException(404, f"no such directory: {root}")
        exts = {".jpg", ".jpeg", ".png", ".bmp", ".mp4", ".mkv", ".mov", ".dav", ".ts"}
        try:
            names = sorted(
                p.name
                for p in root.iterdir()
                if p.suffix.lower() in exts and p.is_file()
            )
        except OSError as exc:
            raise HTTPException(502, f"could not list {root}: {exc}") from exc
        return JSONResponse({"dir": str(root), "files": names[:500], "n": len(names)})

    @router.get("/stitch/half.jpg")
    def get_half(side: str = "left", v: int = 0) -> Response:
        with _session.lock:
            data = _session.halves.get(side)
            version = _session.version
        if data is None:
            raise HTTPException(404, "no snapshot yet")
        del v  # cache-buster only; the session always serves its current frame
        return Response(
            data,
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store", "X-Stitch-Version": str(version)},
        )

    @router.post("/stitch/layers")
    def post_layers(body: dict = Body(default={})) -> JSONResponse:
        """Fetch the two sensors' un-blended contributions to the overlap.

        THE POINT OF THIS ENDPOINT. Everything else on this page works from the
        fused panorama, where the two sensors are already mixed over the blend
        window and cannot be pulled apart. This returns them *before* the
        cross-fade: two real images of the same output columns, one per sensor.
        Aligning them by hand is therefore direct measurement rather than
        inference from the shoulders either side.

        THE SOURCE IS DELIBERATELY NOT PART OF THE CONTRACT. The reply is
        "a left layer, a right layer, and the panorama columns they correspond
        to" and nothing else. Today `camera` lifts a 128-px pair out of one ISF
        buffer. When the vendor's two-channel full-resolution snap becomes
        reachable it is a third entry in `seam_layers.SOURCES` and *no change
        here or in the UI*: the layers get wider, their origins stop coinciding,
        the overlap stops being the whole image, and every one of those is
        already a field the descriptor carries.

        `auto` tries the camera and falls back to the archived dump, so the tool
        comes up on a desk with no camera on the link.
        """
        toolkit = load_toolkit()
        mod = toolkit["layers"]
        if mod is None:
            raise HTTPException(
                503, "; ".join(toolkit["errors"]) or "seam_layers missing"
            )
        want = str(body.get("source") or "auto").strip().lower()
        path = str(body.get("path") or "").strip()
        attempts: list[str] = []

        def _from_camera() -> Any:
            cam = _reolink_camera(config_path)
            return mod.capture(cam.device_ip)

        def _from_file() -> Any:
            if not path:
                raise mod.LayerCaptureError(
                    "the file source needs `path`: a raw dump of the packed pair"
                )
            return mod.load_file(path)

        order: list[tuple[str, Any]]
        if want == "auto":
            order = [("camera", _from_camera)]
            if path:
                order.append(("file", _from_file))
        elif want == "camera":
            order = [("camera", _from_camera)]
        elif want == "file":
            order = [("file", _from_file)]
        elif want == "synthetic":
            order = [
                (
                    "synthetic",
                    lambda: mod.synthetic(
                        dx=float(body.get("dx", 6.0)),
                        roll=float(body.get("roll", 12.0)),
                    ),
                )
            ]
        else:
            raise HTTPException(400, f"unknown layer source {want!r}")

        pair = None
        for name, fn in order:
            try:
                pair = fn()
                break
            except Exception as exc:  # noqa: BLE001 -- every source fails differently
                attempts.append(f"{name}: {type(exc).__name__}: {exc}")
                logger.warning("STITCH: layer source %s failed: %s", name, exc)
        if pair is None:
            raise HTTPException(
                502,
                "could not obtain a layer pair. " + " | ".join(attempts),
            )

        images, desc = _encode_layers(pair)
        desc["registration"] = pair.registration()
        desc["attempts"] = attempts
        with _session.lock:
            _session.layers = images
            _session.layers_version += 1
            _session.layers_at = time.time()
            _session.layers_desc = desc
            _session.layers_error = " | ".join(attempts)
            desc["version"] = _session.layers_version
            payload = _state_payload()
        payload["layers"] = desc
        return JSONResponse(payload)

    @router.get("/stitch/layer.png")
    def get_layer(side: str = "left", v: int = 0) -> Response:
        with _session.lock:
            data = _session.layers.get(side)
            version = _session.layers_version
        if data is None:
            raise HTTPException(404, "no layer pair pulled yet")
        del v  # cache-buster only
        return Response(
            data,
            media_type="image/png",
            headers={"Cache-Control": "no-store", "X-Stitch-Layers": str(version)},
        )

    @router.post("/stitch/sensors")
    def post_sensors(body: dict = Body(default={})) -> JSONResponse:
        """Two whole-lens stills, so the operator can see what each lens sees.

        CONTEXT ONLY, AND THE ENDPOINT SAYS SO IN ITS PAYLOAD. These come back
        3840x2160 -- each sensor's native output width -- which puts them in
        sensor coordinates, before the warp. A displacement measured here does
        not convert to `dx_anchors`, because the warp between sensor space and
        panorama space is not a thing this tool has.

        They are worth serving anyway: two whole lens views tell an operator at
        a tripod what each camera is actually pointed at, which the 128-px
        overlap strip cannot. Orientation first, then precision work on the
        pair that is pre-rectified. `authoritative: false` travels with every
        response so the UI cannot accidentally treat them as the other thing.
        """
        toolkit = load_toolkit()
        mod = toolkit["layers"]
        if mod is None:
            raise HTTPException(
                503, "; ".join(toolkit["errors"]) or "seam_layers missing"
            )
        left, right = str(body.get("left") or ""), str(body.get("right") or "")
        try:
            if left and right:
                views = mod.load_sensor_views(left, right)
            else:
                cam = _reolink_camera(config_path)
                views = mod.capture_sensor_views(cam.device_ip)
        except Exception as exc:  # noqa: BLE001 -- report, never fabricate
            logger.warning("STITCH: sensor views failed: %s", exc)
            raise HTTPException(502, f"{type(exc).__name__}: {exc}") from exc
        desc = views.to_api()
        with _session.lock:
            _session.sensors = {"left": views.left, "right": views.right}
            _session.sensors_version += 1
            desc["version"] = _session.sensors_version
            _session.sensors_desc = desc
            payload = _state_payload()
        return JSONResponse(payload)

    @router.get("/stitch/sensor.jpg")
    def get_sensor(side: str = "left", v: int = 0) -> Response:
        with _session.lock:
            data = _session.sensors.get(side)
            version = _session.sensors_version
        if data is None:
            raise HTTPException(404, "no sensor views pulled yet")
        del v  # cache-buster only
        return Response(
            data,
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store", "X-Stitch-Sensors": str(version)},
        )

    @router.post("/stitch/measure")
    def post_measure(body: dict = Body(default={})) -> JSONResponse:
        """Score a candidate curve against the metric the solver minimises.

        SCR is re-scored from the cached chains, which is exact enough to be
        the same objective (see tests/test_seam_metric_incremental.py). SSR is
        recomputed on a genuinely shifted crop, because gradient energy is not
        recoverable from fitted lines.
        """
        anchors = _anchors_from_body(body)
        metric = load_toolkit()["metric"]
        if metric is None:
            raise HTTPException(503, "seam_metric is not importable")
        with _session.lock:
            chains, frame = _session.chains, _session.frame
            width, height = _session.width, _session.height
            baseline = _session.baseline
            upright = _session.vertical
        if chains is None or frame is None:
            raise HTTPException(409, "no measured snapshot yet")

        seam = width // 2
        band = _target_band(upright, height)
        scr = metric.residual_from_chains(chains, anchors)
        ssr, ssr_target = _ssr_for_anchors(metric, frame, seam, height, anchors, band)
        payload = _score_payload(scr, ssr, ssr_target, band)
        payload["baseline"] = baseline
        return JSONResponse(payload)

    @router.post("/stitch/save")
    def post_save(body: dict = Body(default={})) -> JSONResponse:
        """Write the calibration artifact. The curve *is* the artifact."""
        anchors = _anchors_from_body(body)
        owner = str(body.get("correction_owner") or "downstream")
        with _session.lock:
            current, factory = _session.scalars_current, _session.scalars_factory
            camera_name = _session.camera_name or "camera"
            validation = {"after": _session.baseline} if _session.baseline else {}
            # Geometry of the frame this was actually measured on, not a
            # constant. `build_dx_lookup` rescales anchors by source_width /
            # source_height, so a profile that lies about its own frame size
            # applies a proportionally wrong correction and nothing complains.
            width = _session.width or PANORAMA_W
            height = _session.height or PANORAMA_H
            source_path = _session.source_path
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        deployment = str(body.get("deployment") or camera_name)
        try:
            profile = build_v2_profile(
                anchors,
                correction_owner=owner,
                calibration_id=f"{deployment}-{stamp}",
                source_width=width,
                source_height=height,
                seam_x=width // 2,
                blend_w=BLEND_W,
                scalars=current,
                factory_scalars=factory,
                validation=validation,
                calibrated_for={
                    "subject_distance_m": body.get("subject_distance_m"),
                    "basis": body.get("basis") or "operator judgement",
                    "fb_px_m": None,
                    "residual_px_at": {},
                },
                provenance={
                    "workflow": "operator",
                    # One tripod placement is one deployment. Whether the seam
                    # offset is a property of the camera or of the placement is
                    # an open measurement, so the artifact records which frame
                    # and which placement it came from either way -- a global
                    # profile that turns out to be per-deployment is
                    # recoverable from this; one that recorded nothing is not.
                    "deployment": deployment,
                    "source": source_path or f"camera snapshot {stamp}",
                    "created_utc": datetime.now(UTC).isoformat(),
                    "tool_version": "stitch_calibration/1",
                },
            )
        except SeamCalibrationError as exc:
            raise HTTPException(400, str(exc)) from exc

        body_text = json.dumps(profile, indent=2)
        # Two writes, on purpose. The first is the path the pipeline already
        # reads (`seam_realign_profile_path`), so a calibration takes effect.
        # The second is an append-only history keyed by deployment, so if the
        # seam turns out to differ per tripod placement the evidence is already
        # collected instead of having been overwritten one calibration at a
        # time.
        target = storage / "stitch_profile.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(body_text, encoding="utf-8")
        tmp.replace(target)

        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in deployment)
        archive = storage / "stitch_calibrations" / f"{safe}-{stamp}.json"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_text(body_text, encoding="utf-8")

        logger.info("STITCH: wrote %s and %s (owner=%s)", target, archive, owner)
        return JSONResponse(
            {"path": str(target), "archived": str(archive), "profile": profile}
        )

    def _auto_measure_async(version: int, frames: list[Any], seam_x: int) -> None:
        """Run `seam_echo.measure` off the request thread.

        Comparable in cost to the SCR detection this page already runs in the
        background (37-50 s), for the same reason: the operator should be
        looking at the picture long before the numbers arrive.
        """
        echo = load_toolkit()["echo"]
        try:
            result = echo.measure(frames, seam_x=seam_x, blend_w=BLEND_W)
            payload = result.to_api()
            with _session.lock:
                if _session.version != version:
                    return
                _session.auto = payload
                _session.auto_state = "ready"
                _session.auto_error = ""
            logger.info(
                "STITCH: auto measure %s dx=%s (seam %d/%d, control %d/%d)",
                payload["verdict"],
                payload["dx"],
                payload["n_accepted"],
                payload["n_candidates"],
                payload["control_accepted"],
                len(result.controls),
            )
        except Exception as exc:  # noqa: BLE001 -- report, never publish a guess
            logger.error("STITCH: auto measure failed: %s", exc)
            with _session.lock:
                if _session.version == version:
                    _session.auto_state = "failed"
                    _session.auto_error = f"{type(exc).__name__}: {exc}"

    @router.post("/stitch/auto")
    def post_auto(body: dict = Body(default={})) -> JSONResponse:
        """Report what the seam echo estimator sees. It does NOT propose a curve.

        **The estimator is withdrawn** (see `seam_echo`): it measures a step
        edge rather than a ghost, and at scale it accepts control corridors --
        where the true answer is exactly zero -- almost as often as the seam
        (6.3% against 7.4%), with the same `d` distribution. On the one
        hand-verified frame carrying a real 18 px ghost it reported 33 px. The
        endpoint and its numbers are kept because they are the evidence and
        because the plumbing is reusable; the curve stays hand-authored.

        Several frames are used when the source is the live camera -- the
        camera is on a tripod and `Snap` is read-only.
        """
        echo = load_toolkit()["echo"]
        if echo is None:
            raise HTTPException(503, "seam_echo is not importable")
        n = max(1, min(int(body.get("frames") or 3), 8))
        with _session.lock:
            base, seam_x = _session.frame, _session.width // 2
            live = bool(_session.frame is not None and not _session.source_path)
            version = _session.version
        if base is None:
            raise HTTPException(409, "take a snapshot first")
        if base.ndim != 3:
            raise HTTPException(
                415,
                "the seam measurement needs a colour frame: a target is "
                "separable from grass by colour, not by luminance",
            )

        frames = [base]
        if live:
            for _ in range(n - 1):
                try:
                    extra, _cam, _host = _live_frame()
                except HTTPException:
                    break  # a short series still measures; a failed one does not
                frames.append(extra)

        with _session.lock:
            if _session.version != version:
                raise HTTPException(409, "the snapshot changed; press Auto again")
            _session.auto = None
            _session.auto_state = "running"
            _session.auto_error = ""
        threading.Thread(
            target=_auto_measure_async,
            args=(version, frames, seam_x),
            daemon=True,
            name="stitch-auto",
        ).start()
        with _session.lock:
            return JSONResponse(_state_payload())

    def _read_camera_cal_async(host: str) -> None:
        """Read the camera's installed calibration off the request thread.

        A mesh dump plus two 267 KB SD reads; seconds, not milliseconds, and it
        must not sit inside `/stitch/state`, which the page polls.
        """
        camera_mod = load_toolkit()["camera"]
        try:
            state = camera_mod.read_calibration(host)
            payload = state.to_api()
            payload["anchors_at_rows"] = (
                None
                if state.anchors is None
                else [list(a) for a in state.anchors_at(ANCHOR_ROWS)]
            )
            with _session.lock:
                _session.camera_cal = payload
                _session.camera_cal_state = "ready"
                _session.camera_cal_error = ""
            logger.info(
                "STITCH: camera calibration live=%s factory=%s at_factory=%s anchors=%s",
                payload["live_crc32"],
                payload["factory_crc32"],
                payload["at_factory"],
                0 if state.anchors is None else len(state.anchors),
            )
        except Exception as exc:  # noqa: BLE001 -- an unreadable camera is a state
            logger.warning("STITCH: could not read camera calibration: %s", exc)
            with _session.lock:
                _session.camera_cal_state = "failed"
                _session.camera_cal_error = f"{type(exc).__name__}: {exc}"

    @router.post("/stitch/camera")
    def post_camera_cal() -> JSONResponse:
        """Read what is installed on the camera: mesh, factory copy, anchors.

        Read-only. Dumping the mesh reads the live VPE state into a file on the
        SD card; nothing here calls `SetStitch` or writes a mesh.

        Note what this deliberately does not do: it does not warp the snapshot.
        `cmd=Snap` already returns the *fused* panorama -- the warp and the
        stitcher have both run before the JPEG exists, which is why the blend
        corridor is visible in it -- so applying the mesh to that image would
        apply it twice. The mesh's honest contribution is its own per-row shape,
        which is what `profile` carries.
        """
        camera_mod = load_toolkit()["camera"]
        if camera_mod is None:
            raise HTTPException(503, "stitch_apply is not importable")
        cam = _reolink_camera(config_path)
        host = _camera_host(cam)
        with _session.lock:
            if _session.camera_cal_state == "running":
                return JSONResponse(_state_payload())
            _session.camera_cal = None
            _session.camera_cal_state = "running"
            _session.camera_cal_error = ""
        threading.Thread(
            target=_read_camera_cal_async,
            args=(host,),
            daemon=True,
            name="stitch-camera-cal",
        ).start()
        with _session.lock:
            return JSONResponse(_state_payload())

    @router.post("/stitch/apply")
    def post_apply(body: dict = Body(default={})) -> JSONResponse:
        """Send the calibration to the camera, in the one order that is correct.

        Everything about sequencing lives in `apply_calibration`: scalars
        first, because `SetStitch` re-runs the vendor's mesh optimiser and
        destroys anything written before it; then a mesh composed onto the
        baseline those scalars produced, with the baseline re-checked
        immediately before the write. This endpoint does not reimplement any
        of that, and deliberately has no path that writes a mesh on its own.
        """
        anchors = _anchors_from_body(body)
        owner = str(body.get("correction_owner") or "downstream")
        if owner == "downstream":
            raise HTTPException(
                400,
                "correction_owner is 'downstream': that surface is applied by "
                "the pipeline from the saved profile, and sending it to the "
                "camera as well is the double-correction. Save, don't apply.",
            )
        camera_mod = load_toolkit()["camera"]
        if camera_mod is None:
            raise HTTPException(503, "stitch_apply is not importable")
        cam = _reolink_camera(config_path)
        with _session.lock:
            camera_name = _session.camera_name or cam.name
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        try:
            report = camera_mod.apply_calibration(
                anchors,
                host=_camera_host(cam),
                user=cam.username,
                password=cam.password,
                scalars=None,
                calibration_id=f"{camera_name}-{stamp}",
                dry_run=bool(body.get("dry_run", False)),
            )
        except Exception as exc:  # noqa: BLE001 -- report, never half-apply
            logger.error("STITCH: apply failed: %s", exc)
            raise HTTPException(502, f"{type(exc).__name__}: {exc}") from exc
        with _session.lock:
            _session.last_apply = report
        return JSONResponse(report)

    return router


def _ssr_for_anchors(
    metric: Any,
    frame: Any,
    seam: int,
    height: int,
    anchors: list[tuple[float, float]],
    band: tuple[int, int] | None = None,
) -> tuple[Any, Any]:
    """SSR of the frame as the candidate curve would leave it, twice.

    Once over the whole field band and once over the rows the calibration
    target occupies, because those are different questions and on a real frame
    they differ by two orders of magnitude.

    Computed on a 1280-px crop around the seam rather than the whole panorama:
    SSR only ever reads the blend window and the two shoulders, so the rest of
    a 7680-px frame is 200 ms of arithmetic on pixels the metric discards.
    """
    import numpy as np

    from video_grouper.utils.stitch_remap import apply_shift_to_frame_rgb

    lo, hi = max(0, seam - STRIP_W), min(frame.shape[1], seam + STRIP_W)
    crop = np.ascontiguousarray(frame[:, lo:hi])
    ys = [a[0] for a in anchors]
    ds = [a[1] for a in anchors]
    lut = np.round(np.interp(np.arange(height), ys, ds)).astype(np.int32)
    shifted = apply_shift_to_frame_rgb(crop, lut, seam - lo)
    whole = metric.seam_sharpness_ratio(shifted, seam_x=seam - lo, blend_w=2 * BLEND_W)
    target = (
        None
        if band is None
        else metric.seam_sharpness_ratio(
            shifted, seam_x=seam - lo, blend_w=2 * BLEND_W, band=band
        )
    )
    return whole, target


def _state_payload() -> dict:
    """Caller must hold the session lock."""
    return {
        "has_snapshot": _session.frame is not None,
        "camera": _session.camera_name,
        "host": _session.host,
        "source_path": _session.source_path,
        "version": _session.version,
        "snapped_at": _session.snapped_at,
        "width": _session.width,
        "height": _session.height,
        "seam_x": _session.width // 2,
        "strip_w": STRIP_W,
        "blend_w": BLEND_W,
        "anchor_rows": list(ANCHOR_ROWS),
        "scalars": {
            "current": _session.scalars_current,
            "factory": _session.scalars_factory,
        },
        "metric_state": _session.chains_state,
        "metric_error": _session.chains_error,
        "baseline": _session.baseline,
        "quality": _session.quality,
        "vertical": _session.vertical,
        "scene": {
            "has": bool(_session.scene),
            "version": _session.scene_version,
            "at": _session.scene_at,
            "source": _session.scene_source,
            "vertical": _session.scene_vertical,
            "width": SCENE_W,
        },
        "last_apply": _session.last_apply,
        "auto": _session.auto,
        "auto_state": _session.auto_state,
        "auto_error": _session.auto_error,
        "camera_cal": _session.camera_cal,
        "camera_cal_state": _session.camera_cal_state,
        "camera_cal_error": _session.camera_cal_error,
        "layers": _session.layers_desc,
        "layers_version": _session.layers_version,
        "layers_at": _session.layers_at,
        "sensors": _session.sensors_desc,
        "sensors_version": _session.sensors_version,
    }


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


def _render_page(toolkit: dict) -> str:
    banner = ""
    if toolkit["errors"]:
        items = "".join(f"<li>{e}</li>" for e in toolkit["errors"])
        banner = (
            '<div class="flash flash-err"><strong>Calibration toolkit '
            f"unavailable</strong><ul>{items}</ul></div>"
        )
    return _PAGE.replace("__BANNER__", banner)


_STYLE = """
:root {
  --bg-base:#0a0b0f; --bg-surface:#13141a; --bg-elev:#181a22; --bg-input:#0f1015;
  --rule:#2a2c34; --rule-strong:#3b3e48; --text:#e6e7ec; --text-mute:#94969f;
  --text-faint:#5e616b; --accent:#fb923c; --signal-on:#22c55e; --signal-off:#6b7280;
  --signal-warn:#fbbf24; --signal-bad:#f43f5e;
  /* System stacks, no webfont link. A phone at a pitch should not be waiting on
     a webfont CDN over whatever link the field has. */
  --display:'Bahnschrift','DIN Alternate','Roboto Condensed',system-ui,sans-serif;
  --body:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;
  --mono:ui-monospace,'SF Mono','Cascadia Mono','Segoe UI Mono',monospace;
}
* { box-sizing:border-box; }
html { -webkit-text-size-adjust:100%; }
body {
  margin:0; font-family:var(--body); font-size:15px; line-height:1.5;
  color:var(--text); background:var(--bg-base); padding-bottom:76px;
  overscroll-behavior-y:contain;
}
.topbar { border-bottom:1px solid var(--rule); background:rgba(10,11,15,.94);
  position:sticky; top:0; z-index:30; backdrop-filter:blur(6px); }
.topbar-inner {
  max-width:1400px; margin:0 auto; padding:9px 12px;
  display:flex; align-items:center; justify-content:space-between; gap:10px;
}
.brand {
  font-family:var(--display); font-weight:700; letter-spacing:.16em;
  font-size:14px; text-transform:uppercase; white-space:nowrap;
}
.brand .dot { color:var(--accent); }
.crumb {
  font-family:var(--mono); font-size:11px; letter-spacing:.1em;
  text-transform:uppercase; color:var(--text-mute); text-align:right;
}
.crumb a { color:var(--text-mute); text-decoration:none; }
.shell { max-width:1400px; margin:0 auto; padding:12px 12px 24px; }
.headline {
  font-family:var(--display); font-weight:700; text-transform:uppercase;
  letter-spacing:.03em; font-size:clamp(21px,5.5vw,44px); line-height:1; margin:0 0 4px;
}
.lede { color:var(--text-mute); max-width:70ch; margin:0 0 12px; font-size:13.5px; }
h2.sec {
  font-family:var(--display); font-weight:700; text-transform:uppercase;
  letter-spacing:.05em; font-size:16px; margin:0 0 9px;
}
h2.sec .accent { color:var(--accent); }
.grid { display:flex; flex-direction:column; gap:12px; }
.col { display:contents; }
.panel {
  background:var(--bg-surface); border:1px solid var(--rule);
  padding:12px 13px; margin:0;
}
details.panel > summary {
  cursor:pointer; list-style:none; font-family:var(--display); font-weight:700;
  text-transform:uppercase; letter-spacing:.05em; font-size:16px;
  display:flex; justify-content:space-between; align-items:center; gap:8px;
}
details.panel > summary::-webkit-details-marker { display:none; }
details.panel > summary::after { content:'+'; color:var(--accent); font-size:19px; }
details.panel[open] > summary::after { content:'\\2212'; }
details.panel > summary .accent { color:var(--accent); }
details.panel[open] > summary { margin-bottom:10px; }
.mono { font-family:var(--mono); font-size:12px; }
.muted { color:var(--text-mute); }
.faint { color:var(--text-faint); font-size:12.5px; }
.btn {
  font-family:var(--display); text-transform:uppercase; letter-spacing:.07em;
  font-weight:600; font-size:15px; padding:11px 15px; cursor:pointer;
  background:var(--accent); color:#141414; border:0; min-height:44px;
  touch-action:manipulation;
}
.btn:active { filter:brightness(1.2); }
.btn[disabled] { background:var(--rule-strong); color:var(--text-faint); cursor:not-allowed; }
.btn-ghost { background:transparent; color:var(--accent); border:1px solid var(--accent); }
.btn-danger { background:transparent; color:var(--signal-bad); border:1px solid var(--signal-bad); }
.btn-sm { font-size:12px; padding:7px 10px; min-height:36px; }
.btn-row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }

/* The procedure. It is the spine of the page, not a tip box. */
ol.steps { list-style:none; margin:0 0 12px; padding:0; border:1px solid var(--rule);
  background:var(--bg-surface); }
ol.steps li { display:flex; gap:9px; padding:8px 11px; border-bottom:1px solid var(--rule);
  color:var(--text-faint); font-size:13px; align-items:flex-start; }
ol.steps li:last-child { border-bottom:0; }
ol.steps li .n { font-family:var(--mono); font-size:11px; width:17px; flex:none;
  border:1px solid var(--rule-strong); text-align:center; line-height:16px; height:18px; }
ol.steps li .t { font-weight:600; color:var(--text-mute); }
ol.steps li .d { display:none; }
ol.steps li.on { color:var(--text); background:var(--bg-elev);
  border-left:3px solid var(--accent); padding-left:8px; }
ol.steps li.on .t { color:var(--accent); }
ol.steps li.on .d { display:block; color:var(--text-mute); font-size:12.5px; }
ol.steps li.done .n { border-color:var(--signal-on); color:var(--signal-on); }

/* Bottom action bar: the two live-camera doors, in thumb reach. */
.dock { position:fixed; left:0; right:0; bottom:0; z-index:40;
  background:rgba(10,11,15,.96); border-top:1px solid var(--rule);
  padding:8px 10px calc(8px + env(safe-area-inset-bottom)); display:flex; gap:8px;
  align-items:center; backdrop-filter:blur(6px); }
.dock .btn { flex:1 1 0; }
.dock .st { font-family:var(--mono); font-size:11px; color:var(--text-mute);
  flex:2 1 0; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }

canvas { display:block; background:#000; image-rendering:pixelated; width:100%;
  touch-action:none; -webkit-user-select:none; user-select:none; }
#zoomwrap, #viewwrap { border:1px solid var(--rule); position:relative; }
#zoomwrap { margin-bottom:8px; }

/* Two-layer alignment. The canvas inherits touch-action:none above, which is
   what stops the browser claiming the drag and the two-finger twist for its
   own scroll and zoom -- without it the primary gesture of this tool reaches
   the page about one time in three. */
#layercvwrap { border:1px solid var(--rule); position:relative; margin-bottom:6px; }
#layercvwrap.locked { outline:2px solid var(--signal-warn); outline-offset:-2px; }
/* 44 px, not the 38 the secondary mode buttons use: these are pressed with a
   thumb, mid-task, while the other hand holds the phone. */
#layermodes button, #layerpanel .btn-sm { min-height:44px; }
.layerread { display:flex; flex-wrap:wrap; gap:6px; margin:10px 0 2px; }
.lnum { flex:1 1 44%; min-width:120px; background:var(--bg-elev);
  border:1px solid var(--rule); padding:8px 10px; }
.lnum b { display:block; font-family:var(--display); font-size:23px; line-height:1.1;
  font-weight:700; }
.lnum span { font-family:var(--mono); font-size:10.5px; color:var(--text-mute);
  letter-spacing:.03em; }
.lnum.good b { color:var(--signal-on); }
.lnum.good { border-color:var(--signal-on); }
img.lens { display:block; width:100%; border:1px solid var(--rule); margin-bottom:8px;
  background:#000; }
.lenscap { font-family:var(--mono); font-size:10.5px; color:var(--text-mute);
  letter-spacing:.06em; text-transform:uppercase; margin:2px 0 3px; }
.modes { display:flex; gap:5px; flex-wrap:wrap; margin:9px 0 6px; }
.modes button, .seg button {
  font-family:var(--mono); font-size:11px; letter-spacing:.06em; text-transform:uppercase;
  padding:8px 9px; background:var(--bg-elev); color:var(--text-mute); min-height:38px;
  border:1px solid var(--rule); cursor:pointer; touch-action:manipulation;
}
.modes button.on, .seg button.on { color:var(--accent); border-color:var(--accent); }
.seg { display:flex; gap:5px; }
.seg button { flex:1; }
.caption {
  font-family:var(--mono); font-size:12px; letter-spacing:.02em;
  padding:8px 10px; margin-top:8px;
  border-left:3px solid var(--accent); background:var(--bg-elev); color:var(--text);
}
.caveat {
  font-size:12.5px; padding:9px 11px; margin-top:8px;
  border-left:3px solid var(--signal-warn); background:rgba(251,191,36,.07);
  color:var(--text-mute);
}
.caveat strong { color:var(--signal-warn); }
.do { font-size:13.5px; padding:10px 12px; margin-top:8px;
  border-left:3px solid var(--signal-on); background:rgba(34,197,94,.09); color:var(--text); }
.do strong { color:var(--signal-on); }
label.row { display:block; margin:11px 0 3px; }
label.row .lbl {
  font-family:var(--mono); font-size:11px; letter-spacing:.09em;
  text-transform:uppercase; color:var(--text-mute);
  display:flex; justify-content:space-between; align-items:baseline; gap:8px;
}
label.row .val { color:var(--accent); font-weight:600; }
/* 44 px: the slider is dragged with a thumb on a phone held one-handed, and
   38 put it under the minimum target size at a 390 px viewport. */
input[type=range] { width:100%; margin:2px 0 0; accent-color:var(--accent); height:44px; }
input[type=number], input[type=text], select {
  background:var(--bg-input); color:var(--text); border:1px solid var(--rule);
  padding:9px 8px; font-family:var(--mono); font-size:15px; width:100%; min-height:42px;
}
select { -webkit-appearance:none; appearance:none; }
table { border-collapse:collapse; width:100%; font-family:var(--mono); font-size:12px; }
th, td { padding:5px 6px; border-bottom:1px solid var(--rule); text-align:left; }
th { color:var(--text-faint); font-weight:600; text-transform:uppercase; letter-spacing:.08em; font-size:10px; }
td.num { text-align:right; }
.kpi { display:grid; grid-template-columns:repeat(2,1fr); gap:8px; margin-bottom:8px; }
.kpi div { background:var(--bg-elev); border:1px solid var(--rule); padding:7px 9px; }
.kpi .k {
  font-family:var(--mono); font-size:10px; letter-spacing:.1em;
  text-transform:uppercase; color:var(--text-faint);
}
.kpi .v { font-family:var(--display); font-size:25px; line-height:1.1; }
.kpi.stale .v, .kpi.stale .d { opacity:.42; }
.better { color:var(--signal-on); } .worse { color:var(--signal-bad); }
.flat { color:var(--text-faint); }
.flash { padding:9px 12px; margin:0 0 12px; border-left:3px solid var(--signal-bad);
  background:rgba(244,63,94,.08); font-size:13px; }
.flash ul { margin:5px 0 0 16px; padding:0; }
.dot { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:5px; }
.dot.on { background:var(--signal-on); } .dot.off { background:var(--signal-off); }
.dot.warn { background:var(--signal-warn); } .dot.bad { background:var(--signal-bad); }
.badge { font-family:var(--mono); font-size:10.5px; letter-spacing:.08em; padding:2px 6px;
  border:1px solid var(--rule-strong); color:var(--text-mute); text-transform:uppercase; }
.badge.live { border-color:var(--signal-on); color:var(--signal-on); }
.badge.stale { border-color:var(--signal-warn); color:var(--signal-warn); }
.badge.off { border-color:var(--signal-bad); color:var(--signal-bad); }
#scenewrap { position:relative; border:1px solid var(--rule); background:#000; }
#scene { display:block; width:100%; height:auto; }
#sceneseam { position:absolute; top:0; bottom:0; left:50%; width:2px; margin-left:-1px;
  background:var(--accent); box-shadow:0 0 6px rgba(251,146,60,.9); pointer-events:none; }
#scenehint { position:absolute; left:0; right:0; bottom:0; padding:3px 6px;
  font-family:var(--mono); font-size:10.5px; background:rgba(10,11,15,.72); color:var(--text-mute); }
.nudge { display:flex; gap:6px; margin:8px 0 0; }
.nudge button { flex:1; font-family:var(--mono); font-size:14px; min-height:46px;
  background:var(--bg-elev); color:var(--text); border:1px solid var(--rule-strong);
  cursor:pointer; touch-action:manipulation; }
.nudge button:active { border-color:var(--accent); color:var(--accent); }
.dxread { font-family:var(--display); font-size:30px; line-height:1.05; }
.dxread span { font-family:var(--mono); font-size:12px; color:var(--text-mute); }

@media (min-width:980px) {
  body { padding-bottom:0; font-size:14px; }
  .shell { padding:20px 24px 60px; }
  .grid { display:grid; grid-template-columns:minmax(0,1fr) 400px; gap:18px;
    align-items:start; }
  .col { display:flex; flex-direction:column; gap:14px; }
  .dock { position:static; border-top:0; border:1px solid var(--rule);
    background:var(--bg-surface); margin-bottom:12px; padding:10px 12px; }
  .dock .btn { flex:0 0 auto; }
  ol.steps { display:flex; }
  ol.steps li { flex:1; border-bottom:0; border-right:1px solid var(--rule);
    flex-direction:column; }
  ol.steps li:last-child { border-right:0; }
}
"""


_SCRIPT = r"""
'use strict';
// ---------------------------------------------------------------------------
// Model. The anchor curve is the ONLY authored state: the translate and roll
// controls are a decomposition of it (mean, best-fit ramp), recomputed whenever
// the curve changes by any route. Nothing the operator can move exists outside
// the artifact.
// ---------------------------------------------------------------------------
var S = {
  st: null, anchors: [], surface: 'camera_mesh', mode: 'blink',
  imgs: {}, blink: 0, cursorRow: 1080, ready: false,
  metrics: null, baseline: null, quality: null, vertical: null,
  busy: false, pending: false, stale: false, scoredAt: null, netFails: 0,
  spanX: 256, grab: 'curve', narrow: true, retry: null,
  auto: null, cameraCal: null,
  // Two-layer alignment. `layers` is the descriptor the server sent -- the
  // panorama columns each layer occupies -- and everything drawn is derived
  // from it, so a source with different geometry needs no change here.
  layers: null, layerImgs: {}, layerMode: 'alpha', layerAlpha: 0.5,
  layerGain: 4, layerSpan: 128, layerCx: 3840, layerCy: 1080,
  layerReg: null, layerBest: null, layerBusy: false, layerPath: ''
};
// Geometry is taken from the frame the server actually fetched, never assumed.
// These are only the pre-snapshot defaults.
var H = 2160, STRIP = 640, BLENDH = 128;
var MEASURE_TIMEOUT_MS = 9000;

function $(id) { return document.getElementById(id); }
function fmt(v, d) { return (v === null || v === undefined || isNaN(v)) ? '--' : v.toFixed(d === undefined ? 2 : d); }
function clamp(v, a, b) { return v < a ? a : (v > b ? b : v); }

function dxAt(y) {
  var A = S.anchors;
  if (!A.length) return 0;
  if (y <= A[0][0]) return A[0][1];
  if (y >= A[A.length - 1][0]) return A[A.length - 1][1];
  for (var i = 1; i < A.length; i++) {
    if (y <= A[i][0]) {
      var y0 = A[i - 1][0], d0 = A[i - 1][1], y1 = A[i][0], d1 = A[i][1];
      return y1 === y0 ? d1 : d0 + (d1 - d0) * (y - y0) / (y1 - y0);
    }
  }
  return A[A.length - 1][1];
}

// Piecewise-linear segments of the curve, as (y0, y1, slope, intercept). A
// linear-in-y horizontal offset is exactly a canvas shear, so each segment is
// one GPU-composited draw call instead of 2160 per-row blits -- which is what
// keeps a drag smooth on a phone with no round trip to the server.
function bands() {
  var A = S.anchors, out = [];
  if (A[0][0] > 0) out.push([0, A[0][0], 0, A[0][1]]);
  for (var i = 0; i < A.length - 1; i++) {
    var y0 = A[i][0], d0 = A[i][1], y1 = A[i + 1][0], d1 = A[i + 1][1];
    var a = (y1 === y0) ? 0 : (d1 - d0) / (y1 - y0);
    out.push([y0, y1, a, d0 - a * y0]);
  }
  var last = A[A.length - 1];
  if (last[0] < H) out.push([last[0], H, 0, last[1]]);
  return out;
}

// Which half the operator sees move. The camera can only warp the LEFT half;
// the downstream corrector only rolls the RIGHT half. Same stored dx either
// way -- the relative registration is identical, so the picture is too.
function movingSide() { return S.surface === 'downstream' ? 'right' : 'left'; }
function signFor(side) {
  // dx is "px the RIGHT half must move right". The left half realises the same
  // relative displacement by moving LEFT, hence the negation.
  if (side !== movingSide()) return 0;
  return side === 'right' ? 1 : -1;
}

// ---------------------------------------------------------------------------
// Drawing
// ---------------------------------------------------------------------------
function View(canvas, ox, oy, sx, sy) {
  this.c = canvas; this.ctx = canvas.getContext('2d');
  this.ox = ox; this.oy = oy; this.sx = sx; this.sy = sy;
}
// Draw one half, sheared by its share of the curve. `emit` issues the actual
// drawImage under the transform so the same shear serves the strip, the
// stretched shoulder extrapolation, and the mirrored overlay alike.
View.prototype.half = function (side, emit, opt) {
  opt = opt || {};
  var img = S.imgs[side]; if (!img) return;
  var ctx = this.ctx, sgn = signFor(side);
  var base = side === 'left' ? 0 : STRIP;   // strip-space x of this half
  var bs = bands(), mir = !!opt.mirror;
  ctx.save();
  ctx.globalAlpha = opt.alpha === undefined ? 1 : opt.alpha;
  for (var i = 0; i < bs.length; i++) {
    var y0 = bs[i][0], y1 = bs[i][1], a = bs[i][2], b = bs[i][3];
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.beginPath();
    ctx.rect(0, (y0 - this.oy) * this.sy, this.c.width, (y1 - y0) * this.sy);
    ctx.clip();
    // source (x,y) -> strip x = base + x + sgn*(a*y+b); mirror reflects about
    // the seam at strip x = STRIP.
    var m11 = this.sx, m21 = this.sx * sgn * a, tx = this.sx * (base + sgn * b);
    if (mir) { m11 = -this.sx; m21 = -this.sx * sgn * a; tx = this.sx * (2 * STRIP - base - sgn * b); }
    ctx.setTransform(m11, 0, m21, this.sy, tx - this.sx * this.ox, -this.sy * this.oy);
    emit(ctx, img);
    ctx.restore(); ctx.save();
    ctx.globalAlpha = opt.alpha === undefined ? 1 : opt.alpha;
  }
  ctx.restore();
  ctx.setTransform(1, 0, 0, 1, 0, 0);
};
// The half at its true position.
function emitStrip(ctx, img) { ctx.drawImage(img, 0, 0); }
// The two extrapolated shoulders, drawn as the lines they actually are.
//
// The blend window's pixels are a mixture of both sensors, so there is no
// second layer to reveal there and no honest way to paint one. What the metric
// does instead is fit each structure independently on the two shoulders and
// extrapolate both to the seam column -- so drawing those extrapolations is
// both the most sensitive comparator available and exactly the evidence the
// score is computed from.
//
// Drawn by sensitivity, not uniformly. `sens` is px of residual per px of
// horizontal shift: a structure at 0.02 is within a degree of horizontal and
// will not move whatever the operator does with the control in their hand.
// Painting it as brightly as one that responds would be a lie told in a
// picture, so it is drawn as a faint grey hint instead.
function drawShoulders(view, which) {
  var obs = (S.metrics && S.metrics.observations) || [];
  if (!obs.length) return;
  var ctx = view.ctx;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.lineWidth = Math.max(1, view.sy * 1.5);
  var seam = STRIP, reach = BLENDH * 2;
  for (var i = 0; i < obs.length; i++) {
    var o = obs[i], blind = (o.sens !== undefined && o.sens < 0.05);
    [['left', o.y_left, blind ? '#6b7280' : '#ff5a5a'],
     ['right', o.y_right, blind ? '#4b5563' : '#48e0ff']].forEach(function (t) {
      if (which && which !== t[0]) return;
      var side = t[0], yAt = t[1];
      var x0 = side === 'left' ? seam - reach : seam;
      var x1 = side === 'left' ? seam : seam + reach;
      var X = function (x) { return (x - view.ox) * view.sx; };
      var Y = function (x) { return (yAt + o.slope * (x - seam) - view.oy) * view.sy; };
      ctx.strokeStyle = t[2];
      ctx.globalAlpha = blind ? 0.30 : 0.9;
      ctx.beginPath(); ctx.moveTo(X(x0), Y(x0)); ctx.lineTo(X(x1), Y(x1)); ctx.stroke();
      ctx.setLineDash([4, 4]); ctx.globalAlpha = blind ? 0.18 : 0.55;
      var x2 = side === 'left' ? seam + reach : seam - reach;
      ctx.beginPath(); ctx.moveTo(X(seam), Y(seam));
      ctx.lineTo(X(x2), Y(x2)); ctx.stroke();
      ctx.setLineDash([]);
    });
  }
  ctx.globalAlpha = 1;
}

function paint(view, grid, locator) {
  var ctx = view.ctx;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, view.c.width, view.c.height);
  ctx.fillStyle = '#000'; ctx.fillRect(0, 0, view.c.width, view.c.height);
  var sxOf = function (x) { return (x - view.ox) * view.sx; };
  if (!S.imgs.left || !S.imgs.right) {
    ctx.fillStyle = '#5e616b'; ctx.font = '13px ui-monospace, monospace';
    ctx.fillText(S.st && S.st.has_snapshot ? 'strips still loading' : 'no frame yet', 10, 22);
    return;
  }

  // Both halves are always drawn at their true positions. Only the blend
  // window changes between views -- blanking a whole half would be a flash,
  // and what a blink has to reveal is a few pixels of jump.
  view.half('left', emitStrip, {});
  view.half('right', emitStrip, {});

  if (S.mode === 'anaglyph') {
    drawShoulders(view, null);            // both at once; they meet or they don't
  } else if (S.mode === 'mirror') {
    view.half('right', emitStrip, { mirror: true, alpha: 0.5 });
  } else if (S.mode === 'blink') {
    drawShoulders(view, S.blink ? 'right' : 'left');
  }

  // seam + blend window
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = 'rgba(251,146,60,.10)';
  ctx.fillRect(sxOf(STRIP - BLENDH), 0, 2 * BLENDH * view.sx, view.c.height);
  ctx.strokeStyle = 'rgba(251,146,60,.85)'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(sxOf(STRIP) + .5, 0); ctx.lineTo(sxOf(STRIP) + .5, view.c.height); ctx.stroke();

  if (grid) {
    ctx.strokeStyle = 'rgba(255,255,255,.13)';
    for (var x = Math.ceil(view.ox); x <= view.ox + view.c.width / view.sx; x++) {
      var px = Math.round(sxOf(x)) + .5;
      ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, view.c.height); ctx.stroke();
    }
  }
  // the locked half is dimmed, always, so the hardware constraint is visible
  var lockedLeft = movingSide() === 'right';
  ctx.fillStyle = 'rgba(0,0,0,.42)';
  ctx.fillRect(lockedLeft ? 0 : sxOf(STRIP), 0,
    lockedLeft ? sxOf(STRIP) : view.c.width - sxOf(STRIP), view.c.height);

  if (locator) {
    // Rows that hold upright structure -- the only rows where a horizontal
    // error is visible at all. Marked so the operator can jump to the person
    // they just put in the seam instead of scrubbing 2160 rows for them.
    var rows = (S.vertical && S.vertical.rows) || [];
    ctx.fillStyle = 'rgba(34,197,94,.30)';
    for (var r = 0; r < rows.length; r++) {
      ctx.fillRect(0, (rows[r][0] - view.oy) * view.sy, view.c.width,
        (rows[r][1] - rows[r][0]) * view.sy);
    }
    // what the zoom view is currently showing
    var span = zoomRows();
    ctx.strokeStyle = 'rgba(255,255,255,.55)'; ctx.lineWidth = 1;
    ctx.strokeRect(0.5, (S.cursorRow - span / 2 - view.oy) * view.sy + .5,
      view.c.width - 1, span * view.sy);
  }
  ctx.strokeStyle = 'rgba(34,197,94,.8)'; ctx.lineWidth = 1;
  var cy = (S.cursorRow - view.oy) * view.sy;
  ctx.beginPath(); ctx.moveTo(0, cy); ctx.lineTo(view.c.width, cy); ctx.stroke();
}

function zoomCssH() {
  return S.narrow ? Math.round(clamp(window.innerHeight * 0.44, 210, 430)) : 430;
}
function zoomScale() {           // CSS px per source px
  var z = $('zoom');
  return (z.clientWidth || 360) / S.spanX;
}
function zoomRows() { return zoomCssH() / zoomScale(); }
function zoomTop() {
  return clamp(S.cursorRow - zoomRows() / 2, 0, Math.max(0, H - zoomRows()));
}

function fitCanvas(c, cssH) {
  var dpr = Math.min(window.devicePixelRatio || 1, 2);
  c.width = Math.max(1, Math.round((c.clientWidth || 360) * dpr));
  c.height = Math.max(1, Math.round(cssH * dpr));
  c.style.height = cssH + 'px';
  return dpr;
}

function draw() {
  var v = $('view'), z = $('zoom');
  var zh = zoomCssH();
  var dpr = fitCanvas(z, zh);
  var scale = z.width / S.spanX;             // device px per source px
  var top = zoomTop();
  paint(new View(z, STRIP - S.spanX / 2, top, scale, scale), scale / dpr >= 2.2, false);

  var lh = S.narrow ? 120 : 430;
  fitCanvas(v, lh);
  paint(new View(v, 0, 0, v.width / (2 * STRIP), v.height / H), false, true);

  $('zoomlabel').textContent = 'rows ' + Math.round(top) + '-' + Math.round(top + zoomRows())
    + ' \u00b7 ' + (zoomScale()).toFixed(1) + 'x \u00b7 ' + Math.round(S.spanX) + ' px wide';

  // The layer view is repainted from the SAME entry point, so it tracks the
  // curve however the curve changed -- a drag here, a nudge button, the roll
  // slider, a typed anchor, or a reset. One artifact, several lenses onto it.
  var lc = $('layercv');
  if (lc) { fitCanvas(lc, layerCssH()); drawLayers(); renderLayerLabel(); }
}

function layerCssH() {
  return S.narrow ? Math.round(clamp(window.innerHeight * 0.42, 240, 460)) : 460;
}

// ---------------------------------------------------------------------------
// Curve editing
// ---------------------------------------------------------------------------
function setAnchors(a, why) {
  S.anchors = a.map(function (p) { return [p[0], clamp(p[1], -64, 64)]; });
  S.stale = true;
  syncControls(); draw(); scheduleMeasure();
  if (why) $('curvewhy').textContent = why;
}
function meanDx() {
  var s = 0; for (var i = 0; i < S.anchors.length; i++) s += S.anchors[i][1];
  return s / S.anchors.length;
}
// Best-fit ramp, expressed as the top-to-bottom amplitude. This is a *view* of
// the curve, recomputed from it after every edit -- never a stored parameter.
function rollAmp() {
  var n = S.anchors.length, sy = 0, syy = 0, sd = 0, syd = 0;
  for (var i = 0; i < n; i++) {
    var y = S.anchors[i][0], d = S.anchors[i][1];
    sy += y; syy += y * y; sd += d; syd += y * d;
  }
  var den = n * syy - sy * sy;
  if (!den) return 0;
  return ((n * syd - sy * sd) / den) * (H - 1);
}
function syncControls() {
  var t = meanDx(), r = rollAmp();
  $('roll').value = r.toFixed(2); $('rollV').textContent = fmt(r) + ' px';
  $('dxnow').innerHTML = (t >= 0 ? '+' : '') + t.toFixed(2)
    + ' <span>px mean \u00b7 dx at row ' + Math.round(S.cursorRow) + ' = '
    + fmt(dxAt(S.cursorRow)) + '</span>';
  var html = '';
  for (var i = 0; i < S.anchors.length; i++) {
    html += '<tr><td class="muted">y ' + S.anchors[i][0] + '</td><td>'
      + '<input type="number" step="0.25" inputmode="decimal" data-i="' + i + '" class="anch" value="'
      + S.anchors[i][1].toFixed(2) + '"></td></tr>';
  }
  $('curverows').innerHTML = html;
  var els = document.querySelectorAll('.anch');
  for (var j = 0; j < els.length; j++) {
    els[j].addEventListener('change', function (e) {
      var i = +e.target.getAttribute('data-i');
      var a = S.anchors.slice(); a[i] = [a[i][0], parseFloat(e.target.value) || 0];
      setAnchors(a, 'anchor ' + i + ' set by hand');
    });
  }
  $('curvejson').textContent = JSON.stringify(
    S.anchors.map(function (p) { return [p[0], Math.round(p[1] * 1000) / 1000]; }));
}

function nudge(delta) {
  setAnchors(S.anchors.map(function (p) { return [p[0], p[1] + delta]; }),
    'whole curve ' + (delta > 0 ? '+' : '') + delta.toFixed(2) + ' px');
}
function setRoll(amp) {
  var cur = rollAmp(), d = amp - cur, mid = (H - 1) / 2;
  setAnchors(S.anchors.map(function (p) {
    return [p[0], p[1] + d * (p[0] - mid) / (H - 1)];
  }), 'roll set to ' + fmt(amp) + ' px top-to-bottom');
}
function nearestAnchor(row) {
  var best = 0, bd = 1e9;
  for (var i = 0; i < S.anchors.length; i++) {
    var d = Math.abs(S.anchors[i][0] - row);
    if (d < bd) { bd = d; best = i; }
  }
  return best;
}

// ---------------------------------------------------------------------------
// Metrics. Every request is bounded and every number says which curve it
// belongs to: on a field link a request that never returns must degrade into a
// visible "stale", never into numbers that quietly stop moving.
// ---------------------------------------------------------------------------
var measureTimer = null;
function scheduleMeasure() {
  if (measureTimer) clearTimeout(measureTimer);
  measureTimer = setTimeout(measure, 180);
  renderFreshness();
}
function measure() {
  if (!S.ready) { renderFreshness(); return; }
  if (S.busy) { S.pending = true; return; }
  S.busy = true;
  var sent = JSON.stringify(S.anchors), sentMean = meanDx();
  var ctl = ('AbortController' in window) ? new AbortController() : null;
  var timer = setTimeout(function () { if (ctl) ctl.abort(); }, MEASURE_TIMEOUT_MS);
  renderFreshness();
  fetch('/stitch/measure', {
    method: 'POST', credentials: 'same-origin',
    headers: { 'content-type': 'application/json' },
    body: sent === '[]' ? '{}' : '{"dx_anchors":' + sent + '}',
    signal: ctl ? ctl.signal : undefined
  }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
    .then(function (res) {
      clearTimeout(timer); S.busy = false;
      if (!res.ok) { S.netFails++; renderFreshness(res.j.detail || 'scoring refused'); return; }
      S.netFails = 0;
      S.metrics = res.j; S.baseline = res.j.baseline || S.baseline;
      S.scoredAt = sentMean;
      S.stale = (sent !== JSON.stringify(S.anchors));
      renderMetrics(); renderFreshness();
      if (S.pending) { S.pending = false; measure(); }
    }).catch(function (e) {
      clearTimeout(timer); S.busy = false; S.netFails++;
      renderFreshness(String(e && e.name === 'AbortError'
        ? 'no answer in ' + (MEASURE_TIMEOUT_MS / 1000) + ' s' : e));
      // Keep trying, with backoff: a phone at a pitch loses the link for a few
      // seconds at a time, and the operator should not have to know to poke it.
      if (S.retry) clearTimeout(S.retry);
      S.retry = setTimeout(measure, Math.min(8000, 1000 * Math.pow(2, Math.min(3, S.netFails))));
    });
}
// One line that says whether the numbers on screen describe the curve on
// screen. Everything else about the network is noise to an operator.
function renderFreshness(err) {
  var b = $('fresh');
  if (!S.ready) {
    b.className = 'badge'; b.textContent = S.st && S.st.has_snapshot ? 'measuring frame' : 'no frame';
  } else if (S.netFails) {
    b.className = 'badge off';
    b.textContent = 'offline \u00d7' + S.netFails;
  } else if (S.busy || S.stale) {
    b.className = 'badge stale'; b.textContent = 'scoring\u2026';
  } else {
    b.className = 'badge live'; b.textContent = 'live';
  }
  $('kpis').className = 'kpi' + ((S.stale || S.busy || S.netFails) ? ' stale' : '');
  $('scoredat').textContent = S.scoredAt === null ? ''
    : ('numbers are for dx ' + (S.scoredAt >= 0 ? '+' : '') + S.scoredAt.toFixed(2) + ' px'
      + (S.stale || S.netFails ? ' \u2014 the curve has moved since' : ''));
  $('neterr').textContent = err || '';
}
function delta(now, before, unit) {
  if (now === null || before === null || now === undefined || before === undefined) return '';
  var d = now - before;
  var cls = Math.abs(d) < 1e-6 ? 'flat' : (d < 0 ? 'better' : 'worse');
  return '<span class="' + cls + '">' + (d >= 0 ? '+' : '') + d.toFixed(2) + ' ' + unit + '</span>';
}

// Whether this frame can constrain the calibration at all, stated before any
// number is, and -- when it cannot -- what to do about it. Real tripod frames
// routinely produce a large SCR that barely responds to dx, because SCR is
// built from near-horizontal structures and a horizontal edge does not move
// when the halves shift horizontally. The remedy is not a better number. It is
// a person standing in the seam.
function renderQuality(q) {
  var el = $('quality');
  if (!q) { el.style.display = 'none'; return; }
  el.style.display = '';
  S.quality = q;
  var sp = q.split || {};
  var blind = (sp.n_total && sp.n_steering !== undefined)
    ? (sp.n_total - sp.n_steering) : 0;
  var blindLine = sp.n_total
    ? ' <span class="faint">' + sp.n_total + ' structures crossing the seam, ' + blind
      + ' of them within 3&deg; of horizontal and therefore blind to a horizontal shift'
      + (sp.max_sensitivity ? '; the steepest converts a shift into '
        + sp.max_sensitivity.toFixed(2) + ' px of residual per px' : '') + '.</span>'
    : '';
  if (q.usable) {
    el.className = 'caption';
    el.innerHTML = '<b>This frame can steer the calibration.</b> ' + q.reason
      + '. Drag until SCR p90 stops falling.' + blindLine;
  } else {
    el.className = 'do';
    el.innerHTML = '<strong>The picture cannot steer this calibration.</strong> '
      + q.reason + '.' + (q.remedy ? ' <b>' + q.remedy + '</b>' : '') + blindLine;
  }
  $('suggest').disabled = (q.best_dx === null || q.best_dx === undefined);
}

// The prior question, answered from the picture rather than from the metric:
// is there anything upright in the blend window for a horizontal error to
// break? Cheap enough (~60 ms) to run on every aiming snapshot.
function renderVertical(v, where) {
  var el = $(where);
  if (!el) return;
  if (!v) { el.style.display = 'none'; return; }
  el.style.display = '';
  if (v.n_with_structure) {
    var r = v.rows && v.rows.length ? v.rows[0] : null;
    el.className = 'do';
    el.innerHTML = '<strong>Upright structure is in the seam</strong> at '
      + (v.rows || []).map(function (x) { return 'rows ' + x[0] + '\u2013' + x[1]; }).join(', ')
      + ' (peak ' + fmt(v.best_ratio, 1) + '\u00d7 the surrounding texture). That is what a '
      + 'horizontal misregistration breaks. <b>Go and look at it</b>: pinch to 4&times; and ask '
      + 'whether the body is continuous across the seam. This says only that something is '
      + 'there \u2014 it is not a registration measurement, and nothing here can score a body '
      + 'that sits inside the blend window, because both sensors are already mixed there.'
      + (r ? ' <button class="btn btn-ghost btn-sm" id="jumpv">Show me</button>' : '');
    if (r) {
      var b = $('jumpv');
      if (b) b.addEventListener('click', function () {
        S.cursorRow = (r[0] + r[1]) / 2; S.spanX = Math.min(S.spanX, 320); draw(); syncControls();
        $('zoomwrap').scrollIntoView({ block: 'center' });
      });
    }
  } else {
    el.className = 'caveat';
    el.innerHTML = '<strong>Nothing upright crosses the seam.</strong> Everything at this seam '
      + 'runs horizontally \u2014 the far touchline, the treeline, the painted banners \u2014 and a '
      + 'horizontal edge does not move when the halves shift horizontally. '
      + '<b>Have someone stand on the orange line</b>, out where play actually happens rather than '
      + 'beside the tripod, and snap again.';
  }
}

// "Nothing to change here" is an outcome, not a failure, and it is the common
// one: across the archived games 52 of 96 frames sit below the SSR noise floor
// and players straddling the seam are continuous. A tool that can only ever say
// "here is a correction" will manufacture one.
function renderFloor(ssr) {
  var el = $('floor');
  if (!ssr || ssr.abs_ln_ssr === undefined) { el.style.display = 'none'; return; }
  var atFloor = ssr.abs_ln_ssr <= ssr.noise_floor;
  el.style.display = '';
  el.className = atFloor ? 'do' : 'caption';
  el.innerHTML = atFloor
    ? '<strong>This seam is already at the measurement floor.</strong> |ln SSR| '
      + fmt(ssr.abs_ln_ssr, 3) + ' is at or below the ' + ssr.noise_floor.toFixed(2)
      + ' floor, which is where a seam with nothing wrong with it reads. Unless the body '
      + 'in the seam looks torn to you, the right answer here is to change nothing.'
    : '|ln SSR| ' + fmt(ssr.abs_ln_ssr, 3) + ' is above the ' + ssr.noise_floor.toFixed(2)
      + ' floor, so there is something to see &mdash; but this metric saturates and cannot '
      + 'tell you how much. Go and look at the person in the seam.';
}

function renderMetrics() {
  var m = S.metrics, b = S.baseline;
  if (!m) return;
  var scr = m.scr, ssr = m.ssr, sp = m.split || {}, tg = m.ssr_target;
  $('scrn').textContent = sp.n_steering === undefined
    ? scr.n : (sp.n_steering + '/' + scr.n);
  if (!scr.n) {
    $('scrp90').textContent = '--';
    $('scrnote').innerHTML =
      'No structure crosses the seam in this frame. That is the expected case mid-field: '
      + '<b>have someone stand in the seam</b> at the depth play happens, then snap again.';
  } else {
    var p90 = (sp.p90_steering === null || sp.p90_steering === undefined)
      ? scr.p90 : sp.p90_steering;
    $('scrp90').textContent = fmt(p90);
    $('scrnote').innerHTML =
      'p90 shown is over the <b>' + (sp.n_steering || 0) + '</b> structures steep enough to see a '
      + 'horizontal shift; over all ' + scr.n + ' it is ' + fmt(scr.p90) + ' px '
      + delta(scr.p90, b && b.scr ? b.scr.p90 : null, 'px')
      + ', p50 ' + fmt(scr.p50) + ' px. ' + scr.row_bands_covered + '/3 row bands, '
      + Math.round(scr.height_coverage * 100) + '% height'
      + (S.quality && !S.quality.usable ? ''
        : scr.suggested_dx === null ? ''
          : ' &middot; measurement suggests dx &asymp; <b>' + fmt(scr.suggested_dx) + ' px</b>');
  }
  // The number the person in the seam exists to produce: seam damage measured
  // over the rows they occupy rather than averaged across a field band of
  // grass, which a misregistration cannot damage because there is nothing
  // there to break.
  $('ssrtarget').textContent = tg ? fmt(tg.abs_ln_ssr, 2) : '--';
  $('ssrv').textContent = fmt(ssr.abs_ln_ssr, 3);
  renderFloor(ssr);
  $('ssrnote').innerHTML = tg
    ? '<b>|ln SSR| on the target</b> is the same metric over rows ' + tg.band[0] + '-'
      + tg.band[1] + ' only &mdash; the rows something upright occupies '
      + delta(tg.abs_ln_ssr, b && b.ssr_target ? b.ssr_target.abs_ln_ssr : null, '')
      + '. Over the whole field band the same frame reads ' + fmt(ssr.abs_ln_ssr, 3)
      + ', because most of a field band is grass and a misregistration cannot break grass. '
      + 'Read it as a <b>before/after</b> number, not as an absolute: SSR is a ratio against a '
      + 'background fitted on the shoulders, so an object that exists only inside the window '
      + 'raises it even when registration is perfect. Keep the same person standing in the same '
      + 'place, Apply, snap again, and a drop is registration improving. Noise floor '
      + ssr.noise_floor.toFixed(2) + '. Dragging cannot move either number: only a camera-side '
      + 'correction applied before the blend can.'
    : delta(ssr.abs_ln_ssr, b && b.ssr ? b.ssr.abs_ln_ssr : null, '')
      + ' &middot; noise floor ' + ssr.noise_floor.toFixed(2)
      + ' &mdash; averaged over the whole field band, which is mostly grass. Put something '
      + 'upright in the seam and this is measured where it stands instead. A post-fusion shift '
      + 'cannot move it either way; only a camera-side correction can.';
  if (!S.quality) {
    $('suggest').disabled = (scr.suggested_dx === null || scr.suggested_dx === undefined);
  }
}

// ---------------------------------------------------------------------------
// The procedure. Five steps, and which one you are on is derived from evidence
// -- a frame exists, something upright is in the seam, the curve has moved --
// never from a button someone pressed.
// ---------------------------------------------------------------------------
function currentStep() {
  var st = S.st || {};
  if (!st.has_snapshot) return st.scene && st.scene.has ? 'stand' : 'aim';
  var v = S.vertical;
  if (v && !v.n_with_structure) return 'stand';
  if (Math.abs(meanDx()) < 0.001 && Math.abs(rollAmp()) < 0.001) return 'adjust';
  return 'ship';
}
function renderSteps() {
  var order = ['aim', 'stand', 'snap', 'adjust', 'ship'], cur = currentStep();
  var at = order.indexOf(cur);
  for (var i = 0; i < order.length; i++) {
    var li = $('step-' + order[i]);
    if (!li) continue;
    li.className = (i === at ? 'on' : (i < at ? 'done' : ''));
  }
}

// ---------------------------------------------------------------------------
// Session
// ---------------------------------------------------------------------------
function applyState(st) {
  S.st = st;
  if (st.height) {
    H = st.height; STRIP = st.strip_w; BLENDH = st.blend_w;
    if (st.anchor_rows && st.anchor_rows.length && !S.anchorsPinned) {
      var rows = st.anchor_rows.filter(function (r) { return r < H; });
      if (rows[rows.length - 1] !== H - 1) rows.push(H - 1);
      S.anchors = rows.map(function (r) { return [r, dxAt(r)]; });
      S.anchorsPinned = true;
      syncControls();
    }
    S.cursorRow = Math.min(S.cursorRow, H - 1);
  }
  $('camname').textContent = st.camera || '--';
  $('camhost').textContent = st.host || '--';
  var sc = st.scalars || {};
  renderScalars(sc.current, sc.factory);
  var ms = st.metric_state;
  var dot = ms === 'ready' ? 'on' : (ms === 'running' ? 'warn' : (ms === 'failed' ? 'bad' : 'off'));
  $('metricdot').className = 'dot ' + dot;
  $('metriclabel').textContent =
    ms === 'ready' ? 'chains detected; scoring is live'
      : ms === 'running' ? 'detecting structures and sweeping dx (25-60 s at 7680x2160)...'
        : ms === 'failed' ? ('detection failed: ' + (st.metric_error || '?'))
          : 'no snapshot yet';
  S.vertical = st.vertical || (st.scene ? st.scene.vertical : null) || null;
  renderVertical(S.vertical, 'vertical');
  renderVertical((st.scene && st.scene.vertical) || S.vertical, 'aimverdict');
  renderQuality(st.quality);
  if (st.scene && st.scene.has) {
    var img = $('scene');
    if (img.getAttribute('data-v') !== String(st.scene.version)) {
      img.setAttribute('data-v', String(st.scene.version));
      img.src = '/stitch/scene.jpg?v=' + st.scene.version;
      $('scenewrap').style.display = '';
    }
  }
  if (st.baseline) { S.baseline = st.baseline; S.metrics = S.metrics || st.baseline; renderMetrics(); }
  S.ready = (ms === 'ready');
  if (ms === 'running') setTimeout(pollState, 2000);
  $('apply').disabled = !st.has_snapshot;
  $('save').disabled = !st.has_snapshot;
  renderSteps(); renderFreshness(); draw();
}
function pollState() {
  fetch('/stitch/state', { credentials: 'same-origin' })
    .then(function (r) { return r.json(); })
    .then(function (st) { applyState(st); if (st.metric_state === 'ready') measure(); })
    .catch(function (e) { S.netFails++; renderFreshness(String(e)); });
}
function renderScalars(cur, fac) {
  var t = $('scalars');
  if (!cur) { t.innerHTML = '<tr><td class="muted">not read</td></tr>'; return; }
  var keys = ['distance', 'stitchXMove', 'stitchYMove'], html = '', same = true;
  for (var i = 0; i < keys.length; i++) {
    var k = keys[i], c = cur[k], f = fac ? fac[k] : null;
    if (f !== null && c !== f) same = false;
    html += '<tr><td>' + k + '</td><td class="num">' + c + '</td><td class="num '
      + (f !== null && c !== f ? 'worse' : 'muted') + '">' + (f === null ? '--' : f) + '</td></tr>';
  }
  t.innerHTML = html;
  $('scalarnote').textContent = same
    ? 'Live values match the factory block: the camera is at its own auto-adjustment, '
    + 'and nothing here has moved it. Accepting that is a real choice -- if the seam '
    + 'measures well, confirm it and calibrate only the residual shear.'
    : 'Live values differ from the factory block: someone has already overridden the '
    + 'auto-adjustment. The factory column is recoverable from the camera itself.';
}

// The aiming loop: cheap, repeatable, and the step where the operator finds out
// where the seam actually falls in the scene before spending 40 s measuring one.
function aim() {
  $('aim').disabled = true; $('dockstate').textContent = 'aiming\u2026';
  fetch('/stitch/aim', { method: 'POST', credentials: 'same-origin' })
    .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
    .then(function (res) {
      $('aim').disabled = false;
      if (!res.ok) { $('dockstate').textContent = res.j.detail || 'aim failed'; return; }
      $('dockstate').textContent = 'aimed ' + new Date().toLocaleTimeString();
      applyState(res.j);
    }).catch(function (e) { $('aim').disabled = false; $('dockstate').textContent = String(e); });
}

function snap() {
  $('snap').disabled = true; $('dockstate').textContent = 'fetching 7680x2160 still\u2026';
  fetch('/stitch/snap', { method: 'POST', credentials: 'same-origin' })
    .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
    .then(function (res) {
      $('snap').disabled = false;
      if (!res.ok) { $('dockstate').textContent = res.j.detail || 'snapshot failed'; return; }
      $('dockstate').textContent = 'live snapshot ' + new Date().toLocaleTimeString();
      loadFrame(res.j);
      // Start the session from the camera's real calibration, not from zero.
      if (!S.cameraCal) readCamera(true);
    }).catch(function (e) { $('snap').disabled = false; $('dockstate').textContent = String(e); });
}

// The camera's installed state. Read once per session so the curve starts from
// what is really on the unit; `dx = 0` means the factory mesh untouched, because
// the boot hook composes anchors onto the mesh the firmware just generated.
function readCamera(thenInit) {
  $('camread').disabled = true;
  $('camstate').textContent = 'reading mesh and anchors…';
  fetch('/stitch/camera', { method: 'POST', credentials: 'same-origin' })
    .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
    .then(function (res) {
      if (!res.ok) {
        $('camread').disabled = false;
        $('camstate').textContent = res.j.detail || 'could not read the camera';
        return;
      }
      pollCamera(thenInit);
    }).catch(function (e) { $('camread').disabled = false; $('camstate').textContent = String(e); });
}

function pollCamera(thenInit) {
  fetch('/stitch/state', { credentials: 'same-origin' })
    .then(function (r) { return r.json(); })
    .then(function (st) {
      if (st.camera_cal_state === 'running') { setTimeout(function () { pollCamera(thenInit); }, 2000); return; }
      $('camread').disabled = false;
      if (st.camera_cal_state === 'failed') {
        $('camstate').textContent = st.camera_cal_error || 'could not read the camera';
        return;
      }
      showCamera(st.camera_cal, thenInit);
    }).catch(function (e) { $('camread').disabled = false; $('camstate').textContent = String(e); });
}

function showCamera(c, thenInit) {
  if (!c) { $('camstate').textContent = 'no camera state'; return; }
  S.cameraCal = c;
  var installed = c.anchors_at_rows;
  $('camstate').textContent = installed
    ? (installed.length + ' anchors installed — live mesh ' + c.live_crc32)
    : ('at factory — live mesh ' + c.live_crc32 +
       (c.at_factory ? ' matches ' + c.factory_name : ''));
  $('camnote').textContent = c.note || '';
  $('camcurrent').disabled = !installed;
  if ($('lcamcurrent')) $('lcamcurrent').disabled = !installed;
  drawCamProfile(c.profile);
  if (thenInit) {
    // Start from the camera, labelled as the camera's state rather than as an
    // edit the operator made.
    setAnchors((installed || S.anchors.map(function (p) { return [p[0], 0]; })),
      installed ? 'loaded from the calibration installed on the camera'
                : 'camera is at factory — curve starts at zero (factory mesh untouched)');
  }
}

function drawCamProfile(profile) {
  var cv = $('camprofile');
  if (!profile || !profile.length) { cv.style.display = 'none'; return; }
  cv.style.display = ''; $('camprofilecap').style.display = '';
  var g = cv.getContext('2d'), W = cv.width, H = cv.height, pad = 30;
  g.clearRect(0, 0, W, H);
  var offs = profile.map(function (r) { return r.offset_px; });
  var lo = Math.min.apply(null, offs), hi = Math.max.apply(null, offs);
  if (hi - lo < 1e-6) hi = lo + 1;
  g.strokeStyle = '#999'; g.lineWidth = 1;
  g.strokeRect(pad, 6, W - pad - 6, H - pad - 6);
  g.beginPath();
  profile.forEach(function (r, i) {
    var x = pad + (r.offset_px - lo) / (hi - lo) * (W - pad - 6);
    var y = 6 + i / (profile.length - 1) * (H - pad - 6);
    if (i === 0) g.moveTo(x, y); else g.lineTo(x, y);
  });
  g.strokeStyle = '#e08a2e'; g.lineWidth = 2; g.stroke();
  g.fillStyle = '#777'; g.font = '10px sans-serif';
  g.fillText(lo.toFixed(0) + ' px', pad, H - 16);
  g.fillText(hi.toFixed(0) + ' px', W - 52, H - 16);
  g.fillText('row 0', 2, 12);
  g.fillText('row ' + profile[profile.length - 1].y.toFixed(0), 2, H - pad + 2);
}

// Seam echo diagnostics. WITHDRAWN as a measurement: it reports what the
// estimator sees and never proposes a curve -- see `seam_echo` for the evidence.
function autoMeasure() {
  $('auto').disabled = true;
  $('autopanel').style.display = '';
  $('autoverdict').textContent = 'measuring…';
  $('autodetail').textContent = '';
  fetch('/stitch/auto', {
    method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ frames: 3 })
  }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
    .then(function (res) {
      if (!res.ok) {
        $('auto').disabled = false;
        $('autoverdict').textContent = res.j.detail || 'measurement failed';
        return;
      }
      pollAuto();
    }).catch(function (e) { $('auto').disabled = false; $('autoverdict').textContent = String(e); });
}

function pollAuto() {
  fetch('/stitch/state', { credentials: 'same-origin' })
    .then(function (r) { return r.json(); })
    .then(function (st) {
      if (st.auto_state === 'running') { setTimeout(pollAuto, 1500); return; }
      $('auto').disabled = false;
      if (st.auto_state === 'failed') {
        $('autoverdict').textContent = st.auto_error || 'measurement failed';
        return;
      }
      showAuto(st.auto);
    }).catch(function (e) { $('auto').disabled = false; $('autoverdict').textContent = String(e); });
}

function showAuto(a) {
  if (!a) { $('autoverdict').textContent = 'no measurement'; return; }
  S.auto = a;
  // No branch here sets a curve. The estimator is withdrawn: it measures step
  // edges rather than ghosts, so its numbers are shown as evidence and never as
  // a proposal. The adopt control is gone rather than merely disabled.
  $('autoverdict').textContent =
    'not measurable automatically — nothing proposed'
    + (a.provisional_dx !== null && a.provisional_dx !== undefined
        ? ' (estimator said ' + a.provisional_dx + ' px; not trustworthy)' : '');
  $('autodetail').textContent = (a.remedy || '') +
    ' [seam ' + a.n_accepted + '/' + a.n_candidates + ' accepted, control ' +
    a.control_accepted + '/' + (a.controls ? a.controls.length : '?') +
    ' — off the seam the true answer is zero]';
}

function loadFrame(res) {
  var v = res.version, n = 0;
  S.imgs = {};
  ['left', 'right'].forEach(function (side) {
    var im = new Image();
    im.onload = function () { S.imgs[side] = im; if (++n === 2) draw(); };
    im.onerror = function () {
      $('dockstate').textContent = 'the ' + side + ' strip did not load \u2014 snap again';
    };
    im.src = '/stitch/half.jpg?side=' + side + '&v=' + v;
  });
  applyState(res);
}

function browse() {
  var d = $('framedir').value.trim();
  if (!d) return;
  $('openstate').textContent = 'listing...';
  fetch('/stitch/frames?dir=' + encodeURIComponent(d), { credentials: 'same-origin' })
    .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
    .then(function (res) {
      if (!res.ok) { $('openstate').textContent = res.j.detail || 'listing failed'; return; }
      var sel = $('framelist'); sel.innerHTML = '';
      res.j.files.forEach(function (f) {
        var o = document.createElement('option'); o.value = f; o.textContent = f; sel.appendChild(o);
      });
      $('openstate').textContent = res.j.n + ' file(s)';
    });
}

function openFrame() {
  var d = $('framedir').value.trim(), f = $('framelist').value;
  if (!d || !f) { $('openstate').textContent = 'pick a folder and a file first'; return; }
  var sep = d.indexOf('\\') >= 0 ? '\\' : '/';
  $('openstate').textContent = 'decoding...';
  fetch('/stitch/open', {
    method: 'POST', credentials: 'same-origin',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      path: d.replace(/[\\/]+$/, '') + sep + f,
      seconds: parseFloat($('atsec').value) || 0,
      deployment: $('deployment').value.trim() || null
    })
  }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
    .then(function (res) {
      if (!res.ok) { $('openstate').textContent = res.j.detail || 'open failed'; return; }
      $('openstate').textContent = 'loaded ' + f;
      $('dockstate').textContent = 'frame from file';
      loadFrame(res.j);
    }).catch(function (e) { $('openstate').textContent = String(e); });
}

function save() {
  fetch('/stitch/save', {
    method: 'POST', credentials: 'same-origin',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      dx_anchors: S.anchors,
      correction_owner: $('owner').value,
      deployment: $('deployment').value.trim() || null,
      subject_distance_m: parseFloat($('distance_m').value) || null,
      basis: $('basis').value || null
    })
  }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
    .then(function (res) {
      $('savestate').innerHTML = res.ok
        ? '<span class="better">written to ' + res.j.path + '</span>'
        : '<span class="worse">' + (res.j.detail || 'save failed') + '</span>';
    }).catch(function (e) { $('savestate').innerHTML = '<span class="worse">' + e + '</span>'; });
}

function applyToCamera(dry) {
  if (!dry && !window.confirm(
    'This writes to the camera: vendor scalars first, then a warp mesh composed '
    + 'onto the baseline they produce. Continue?')) return;
  $('applystate').textContent = dry ? 'dry run...' : 'applying (this takes ~1 min)...';
  fetch('/stitch/apply', {
    method: 'POST', credentials: 'same-origin',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      dx_anchors: S.anchors, correction_owner: $('owner').value, dry_run: !!dry
    })
  }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
    .then(function (res) {
      $('applystate').innerHTML = res.ok ? '<span class="better">done</span>'
        : '<span class="worse">' + (res.j.detail || 'apply failed') + '</span>';
      $('applyreport').textContent = JSON.stringify(res.j, null, 2);
      if (res.ok && !dry) snap();   // re-score against a fresh frame, not a promise
    }).catch(function (e) { $('applystate').innerHTML = '<span class="worse">' + e + '</span>'; });
}

// ---------------------------------------------------------------------------
// Two-layer alignment.
//
// This is the only view on the page that shows the two sensors SEPARATELY. The
// rest of the page works from the fused panorama, where the blend window is
// already a mixture and blink/anaglyph can only draw extrapolations from the
// shoulders. Here both layers are real images of the same panorama columns,
// captured before the cross-fade, so overlaying them IS the measurement.
//
// It authors nothing of its own. Every gesture lands in `S.anchors` through the
// same `setAnchors` / `nudge` / `setRoll` the sliders use, so the curve stays
// the single artifact and this view stays a lens onto it. Drag a layer here and
// the numeric anchors below change; type an anchor below and the layers move.
// ---------------------------------------------------------------------------

// Geometry of the inspection view, in PANORAMA columns and rows. Isotropic on
// purpose: a rotation gesture only reads correctly if x and y are at the same
// scale, and this view exists to judge rotation.
function layerGeo(c) {
  var W = c.width, Hc = c.height;
  var scale = W / S.layerSpan;
  return {
    left: S.layerCx - S.layerSpan / 2,
    top: S.layerCy - (Hc / scale) / 2,
    scale: scale, scaleY: scale,
    rows: Hc / scale
  };
}

// One layer, sheared by its share of the curve, in panorama coordinates.
//
// Same piecewise-linear decomposition as the fused view: each straight segment
// of the anchor curve is one affine draw rather than 2160 per-row blits, which
// is what keeps a drag at 60 fps on a phone with no round trip.
function drawLayerInto(ctx, side, geo, opt) {
  opt = opt || {};
  var img = S.layerImgs[side], desc = S.layers && S.layers[side];
  if (!img || !desc || !desc.served) return;
  var sgn = signFor(side), x0 = desc.served.x0, bs = bands();
  // Only the rows on screen. At a useful inspection zoom this view shows tens
  // of rows out of 2160, and handing the whole image to drawImage asks the
  // compositor to rasterise something like 900x15000 px per band -- which does
  // not show up in the drawImage call's own timing (it returned in 5 ms) and
  // then wedges the compositor. Clipping alone is not enough: the clip bounds
  // the output, the source rectangle is what bounds the work.
  var vTop = Math.max(0, Math.floor(geo.top) - 1);
  var vBot = Math.min(img.height, Math.ceil(geo.top + geo.rows) + 1);
  if (vBot <= vTop) return;
  ctx.save();
  ctx.globalAlpha = opt.alpha === undefined ? 1 : opt.alpha;
  if (opt.composite) ctx.globalCompositeOperation = opt.composite;
  for (var i = 0; i < bs.length; i++) {
    var y0 = Math.max(bs[i][0], vTop), y1 = Math.min(bs[i][1], vBot);
    if (y1 <= y0) continue;                     // band entirely off screen
    var a = bs[i][2], b = bs[i][3];
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.beginPath();
    ctx.rect(0, (y0 - geo.top) * geo.scaleY, ctx.canvas.width, (y1 - y0) * geo.scaleY);
    ctx.clip();
    // image column u, row v  ->  panorama x = x0 + u + sgn*(a*v + b)
    //                       ->  canvas  x = (panx - geo.left) * geo.scale
    ctx.setTransform(
      geo.scale, 0,
      geo.scale * sgn * a, geo.scaleY,
      geo.scale * (x0 + sgn * b - geo.left), -geo.scaleY * geo.top
    );
    // Source and destination are the same rectangle in image space; the
    // transform above puts it on screen.
    ctx.drawImage(img, 0, y0, img.width, y1 - y0, 0, y0, img.width, y1 - y0);
    ctx.restore(); ctx.save();
    ctx.globalAlpha = opt.alpha === undefined ? 1 : opt.alpha;
    if (opt.composite) ctx.globalCompositeOperation = opt.composite;
  }
  ctx.restore();
  ctx.setTransform(1, 0, 0, 1, 0, 0);
}

// Tint one layer into a single colour channel, for the anaglyph. Done on an
// offscreen canvas because 'multiply' against the main canvas would pick up
// whatever is already drawn there.
var _tintCv = {};
function tinted(side, colour, w, h, geo) {
  var cv = _tintCv[side] || (_tintCv[side] = document.createElement('canvas'));
  cv.width = w; cv.height = h;
  var cx = cv.getContext('2d');
  cx.setTransform(1, 0, 0, 1, 0, 0);
  cx.clearRect(0, 0, w, h);
  drawLayerInto(cx, side, geo, {});
  cx.setTransform(1, 0, 0, 1, 0, 0);
  cx.globalCompositeOperation = 'multiply';
  cx.fillStyle = colour;
  cx.fillRect(0, 0, w, h);
  // keep the layer's own alpha, so outside-the-image stays transparent
  cx.globalCompositeOperation = 'destination-in';
  drawLayerInto(cx, side, geo, {});
  cx.globalCompositeOperation = 'source-over';
  return cv;
}

function paintLayers() {
  var c = $('layercv');
  if (!c) return;
  var ctx = c.getContext('2d');
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, c.width, c.height);
  ctx.fillStyle = '#000';
  ctx.fillRect(0, 0, c.width, c.height);
  if (!S.layers || !S.layerImgs.left || !S.layerImgs.right) {
    ctx.fillStyle = '#5e616b';
    ctx.font = '13px ui-monospace, monospace';
    ctx.fillText(S.layerBusy ? 'pulling the layer pair…'
      : 'no layer pair yet — press Pull layers', 10, 22);
    return;
  }
  var geo = layerGeo(c), m = S.layerMode;

  if (m === 'diff') {
    // |L - R|. Registered content goes black; every misregistered edge lights
    // up. The gain is there because a 1 px residual on a soft edge is a few
    // grey levels and would otherwise be invisible on a phone in daylight.
    drawLayerInto(ctx, 'left', geo, {});
    drawLayerInto(ctx, 'right', geo, { composite: 'difference' });
    if (S.layerGain > 1 && ('filter' in ctx)) {
      var snap = document.createElement('canvas');
      snap.width = c.width; snap.height = c.height;
      snap.getContext('2d').drawImage(c, 0, 0);
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.fillStyle = '#000'; ctx.fillRect(0, 0, c.width, c.height);
      ctx.filter = 'brightness(' + S.layerGain + ')';
      ctx.drawImage(snap, 0, 0);
      ctx.filter = 'none';
    }
  } else if (m === 'anaglyph') {
    // Left red, right cyan. Registered content reads neutral grey; anything
    // out of register fringes red on one side and cyan on the other, and the
    // direction of the fringe tells you which way to drag.
    ctx.drawImage(tinted('left', '#ff0000', c.width, c.height, geo), 0, 0);
    ctx.globalCompositeOperation = 'lighter';
    ctx.drawImage(tinted('right', '#00ffff', c.width, c.height, geo), 0, 0);
    ctx.globalCompositeOperation = 'source-over';
  } else if (m === 'blink') {
    // Motion is the most sensitive misalignment detector a human has, and
    // unlike on the fused frame these really are the two layers alternating.
    drawLayerInto(ctx, S.blink ? 'right' : 'left', geo, {});
  } else {
    // alpha: both at once, the baseline view. The layer ON TOP is the one the
    // operator is moving -- fading the thing under your finger over a fixed
    // reference is what makes a misregistration readable. So the top layer is
    // `movingSide()`, which is the left half for the camera mesh and flips to
    // the right for the downstream corrector, and the other half sits beneath
    // it fully opaque. Only the top layer takes the opacity control; a second
    // slider on the reference would only dim the thing you are aligning to.
    var top = movingSide(), base = top === 'left' ? 'right' : 'left';
    drawLayerInto(ctx, base, geo, {});
    drawLayerInto(ctx, top, geo, { alpha: S.layerAlpha });
  }

  // Overlap bounds and the cross-fade centre, in panorama columns. Drawn from
  // the descriptor, never from a constant -- with full-resolution layers these
  // move and the drawing has to follow.
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  var X = function (px) { return (px - geo.left) * geo.scale; };
  var ov = S.layers.overlap;
  if (ov && ov.w > 0) {
    ctx.strokeStyle = 'rgba(148,163,184,.45)'; ctx.lineWidth = 1;
    ctx.setLineDash([5, 5]);
    [ov.x0, ov.x1].forEach(function (px) {
      ctx.beginPath(); ctx.moveTo(X(px) + .5, 0); ctx.lineTo(X(px) + .5, c.height); ctx.stroke();
    });
    ctx.setLineDash([]);
  }
  ctx.strokeStyle = 'rgba(251,146,60,.85)'; ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(X(S.layers.seam_x) + .5, 0);
  ctx.lineTo(X(S.layers.seam_x) + .5, c.height);
  ctx.stroke();

  // Where in the 2160 rows this window is sitting.
  var track = 4, ty = (geo.top / H) * c.height, th = (geo.rows / H) * c.height;
  ctx.fillStyle = 'rgba(255,255,255,.10)';
  ctx.fillRect(c.width - track, 0, track, c.height);
  ctx.fillStyle = 'rgba(34,197,94,.75)';
  ctx.fillRect(c.width - track, ty, track, Math.max(3, th));
}

// The number that turns dragging into a task with an end.
//
// Computed in the browser, on the pixels actually on screen, so it responds to
// the drag itself rather than to a round trip. Both layers are rendered into
// offscreen buffers over the OVERLAP ONLY -- the one region where both have
// content and a comparison means anything -- and compared where both are
// opaque, so content sliding out of the window stops counting instead of
// counting as black.
var _regCv = {};
function computeRegistration() {
  if (!S.layers || !S.layerImgs.left || !S.layerImgs.right) return null;
  var ov = S.layers.overlap;
  if (!ov || ov.w <= 0) return null;
  var W = Math.min(ov.w, 192), ROWS = 240;
  var geo = { left: ov.x0, top: 0, scale: W / ov.w, scaleY: ROWS / H, rows: H };
  var data = {};
  ['left', 'right'].forEach(function (side) {
    var cv = _regCv[side] || (_regCv[side] = document.createElement('canvas'));
    cv.width = W; cv.height = ROWS;
    var cx = cv.getContext('2d', { willReadFrequently: true });
    cx.setTransform(1, 0, 0, 1, 0, 0);
    cx.clearRect(0, 0, W, ROWS);
    drawLayerInto(cx, side, geo, {});
    data[side] = cx.getImageData(0, 0, W, ROWS).data;
  });
  var a = data.left, b = data.right;
  var n = 0, sa = 0, sb = 0, sad = 0;
  for (var i = 0; i < a.length; i += 4) {
    if (a[i + 3] < 250 || b[i + 3] < 250) continue;   // one layer absent here
    n++; sa += a[i]; sb += b[i]; sad += Math.abs(a[i] - b[i]);
  }
  if (n < 64) return { mad: null, ncc: null, n: n };
  var ma = sa / n, mb = sb / n, num = 0, va = 0, vb = 0;
  for (var j = 0; j < a.length; j += 4) {
    if (a[j + 3] < 250 || b[j + 3] < 250) continue;
    var da = a[j] - ma, db = b[j] - mb;
    num += da * db; va += da * da; vb += db * db;
  }
  var den = Math.sqrt(va * vb);
  return { mad: sad / n, ncc: den > 0 ? num / den : null, n: n };
}

function renderLayerReadout() {
  if (!$('layerread')) return;
  var t = meanDx(), r = rollAmp();
  var reg = S.layerReg;
  var best = S.layerBest;
  var html = '<div class="lnum"><b>' + (t >= 0 ? '+' : '') + t.toFixed(2)
    + '</b><span>px translate</span></div>'
    + '<div class="lnum"><b>' + (r >= 0 ? '+' : '') + r.toFixed(2)
    + '</b><span>px rotate, top&rarr;bottom</span></div>';
  if (reg && reg.mad !== null && reg.mad !== undefined) {
    var better = (best !== null && best !== undefined && reg.mad <= best + 1e-9);
    html += '<div class="lnum' + (better ? ' good' : '') + '"><b>' + reg.mad.toFixed(2)
      + '</b><span>mean |L&minus;R|' + (better ? ' &mdash; best yet' : '') + '</span></div>'
      + '<div class="lnum"><b>' + (reg.ncc === null ? '--' : reg.ncc.toFixed(4))
      + '</b><span>correlation, 1.0 is perfect</span></div>';
  }
  $('layerread').innerHTML = html;
}

function refreshRegistration() {
  S.layerReg = computeRegistration();
  if (S.layerReg && S.layerReg.mad !== null) {
    if (S.layerBest === null || S.layerReg.mad < S.layerBest) S.layerBest = S.layerReg.mad;
  }
  renderLayerReadout();
}

function drawLayers() {
  paintLayers();
  refreshRegistration();
}

function setLayerSpan(v) {
  S.layerSpan = clamp(v, 16, 4096);
  drawLayers();
  renderLayerLabel();
}
function renderLayerLabel() {
  var c = $('layercv'); if (!c || !S.layers) return;
  var geo = layerGeo(c);
  $('layerlabel').textContent =
    'panorama x ' + Math.round(geo.left) + '-' + Math.round(geo.left + S.layerSpan)
    + ' · rows ' + Math.round(geo.top) + '-' + Math.round(geo.top + geo.rows)
    + ' · ' + geo.scale.toFixed(1) + '×';
}

function pullLayers(source) {
  var body = { source: source || 'auto' };
  if (S.layerPath) body.path = S.layerPath;
  S.layerBusy = true;
  $('layerstate').textContent = 'pulling the pre-blend pair…';
  drawLayers();
  fetch('/stitch/layers', {
    method: 'POST', credentials: 'same-origin',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body)
  }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
    .then(function (res) {
      S.layerBusy = false;
      if (!res.ok) {
        $('layerstate').innerHTML = '<span class="worse">'
          + (res.j.detail || 'the pull failed') + '</span>';
        drawLayers(); return;
      }
      applyState(res.j);
      var d = res.j.layers;
      S.layers = d;
      S.layerBest = null;
      // The curve's row space must match the layers' height. With no fused
      // snapshot to take it from, the pair is the authority -- and a source
      // whose layers are not 2160 rows tall must not silently author a curve
      // addressing rows that do not exist.
      if (!res.j.has_snapshot && d.height && d.height !== H) {
        H = d.height;
        S.anchors = S.anchors.map(function (p, i, all) {
          return [i === all.length - 1 ? H - 1 : Math.round(p[0] * (H - 1) / 2159), p[1]];
        });
        S.cursorRow = Math.min(S.cursorRow, H - 1);
        syncControls();
      }
      // Frame the overlap, centred on the cross-fade, at a zoom where a
      // single pixel of misregistration is a visible step.
      S.layerSpan = Math.max(32, d.overlap.w);
      S.layerCx = d.seam_x;
      S.layerCy = Math.round(d.height / 2);
      var pending = 2;
      ['left', 'right'].forEach(function (side) {
        var img = new Image();
        img.onload = function () {
          S.layerImgs[side] = img;
          if (--pending === 0) { drawLayers(); renderLayerLabel(); }
        };
        img.onerror = function () { if (--pending === 0) drawLayers(); };
        img.src = '/stitch/layer.png?side=' + side + '&v=' + d.version;
      });
      var truth = d.truth
        ? ' · built-in answer dx=' + d.truth.dx.toFixed(2)
          + ' roll=' + d.truth.roll.toFixed(2)
        : '';
      $('layerstate').innerHTML = '<b>' + d.source + '</b> · ' + d.detail
        + ' · overlap = panorama x ' + d.overlap.x0 + '-' + d.overlap.x1
        + ' (' + d.overlap.w + ' px), cross-fade at ' + d.seam_x + truth;
      $('layerattempts').textContent = (d.attempts && d.attempts.length)
        ? 'fell back after: ' + d.attempts.join(' | ') : '';
    }).catch(function (e) {
      S.layerBusy = false;
      $('layerstate').innerHTML = '<span class="worse">' + e + '</span>';
      drawLayers();
    });
}

// Whole-lens context. Two <img> and a caption -- no canvas, no gestures, no
// curve. It exists so an operator can see what each lens is pointed at before
// working a 128-px strip, and it is drawn plainly on purpose: anything that
// looked like the alignment surface would invite a drag that cannot mean
// anything in sensor coordinates.
function pullSensors() {
  $('sensorstate').textContent = 'asking the camera for a per-sensor pair…';
  fetch('/stitch/sensors', {
    method: 'POST', credentials: 'same-origin',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(S.sensorPaths || {})
  }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
    .then(function (res) {
      if (!res.ok) {
        $('sensorstate').innerHTML = '<span class="worse">'
          + (res.j.detail || 'the per-sensor snap failed') + '</span>';
        return;
      }
      applyState(res.j);
      renderSensors(res.j.sensors);
    }).catch(function (e) {
      $('sensorstate').innerHTML = '<span class="worse">' + e + '</span>';
    });
}

function renderSensors(d) {
  if (!d) return;
  $('sensorwrap').style.display = '';
  $('sensorL').src = '/stitch/sensor.jpg?side=left&v=' + d.version;
  $('sensorR').src = '/stitch/sensor.jpg?side=right&v=' + d.version;
  $('sensorstate').innerHTML = '<b>' + d.width + '&times;' + d.height + '</b> per sensor · '
    + 'matched at ' + d.stamp + ' · <b class="worse">context only</b> — ' + d.why;
}

// ---------------------------------------------------------------------------
// Gestures. Pointer Events, so one implementation serves a thumb and a mouse.
// One finger is axis-locked -- across is the calibration, down is navigation --
// and two fingers zoom. Nothing here touches the network.
// ---------------------------------------------------------------------------
function geomFor(kind, c) {
  if (kind === 'zoom') {
    return { scale: zoomScale(), left: STRIP - S.spanX / 2, top: zoomTop(), rows: zoomRows() };
  }
  var s = (c.clientWidth || 360) / (2 * STRIP);
  return { scale: s, left: 0, top: 0, rows: H };
}
function pdist(pts) {
  var a = [];
  pts.forEach(function (v) { a.push(v); });
  return Math.hypot(a[0].x - a[1].x, a[0].y - a[1].y);
}
function setSpan(v) {
  S.spanX = clamp(v, 48, 2 * STRIP);
  draw();
}
// Translate and rotate, both written into the anchor curve.
//
// A constant offset added to every anchor is a translation. A ramp that is
// linear in y is a rotation -- `dx = -theta*y` is exactly the shape a relative
// lens roll produces, which is why this tool models roll and not an arbitrary
// warp. So a two-finger twist and the roll slider are the same edit arriving by
// different routes, and both leave the curve as the only stored thing.
function applyToCurve(before, dTranslate, dRoll, why) {
  // A gesture may only author against a pair that is in PANORAMA output
  // coordinates, because only there is a measured displacement already the
  // correction. The whole-lens context views are in sensor coordinates, before
  // the warp, and a drag on them would convert to nothing. Enforced rather
  // than captioned: `authoritative` comes off the server's descriptor.
  if (!S.layers || !S.layers.authoritative) {
    $('curvewhy').textContent =
      'That surface is context only — it is in sensor coordinates, before the '
      + 'warp, so a displacement there is not a seam correction.';
    return;
  }
  var mid = (H - 1) / 2;
  setAnchors(before.map(function (p) {
    return [p[0], p[1] + dTranslate + dRoll * (p[0] - mid) / (H - 1)];
  }), why);
}

function pang(pts) {
  var a = [];
  pts.forEach(function (v) { a.push(v); });
  return Math.atan2(a[1].y - a[0].y, a[1].x - a[0].x);
}
function angdiff(b, a) {
  var d = b - a;
  while (d > Math.PI) d -= 2 * Math.PI;
  while (d < -Math.PI) d += 2 * Math.PI;
  return d;
}

function bindLayerGestures(c) {
  var pts = new Map(), one = null, two = null;
  c.addEventListener('pointerdown', function (ev) {
    try { c.setPointerCapture(ev.pointerId); } catch (e) { void e; }
    pts.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
    if (pts.size === 2) {
      two = {
        d: pdist(pts), ang: pang(pts), span: S.layerSpan,
        before: S.anchors.slice()
      };
      one = null;
      return;
    }
    if (pts.size !== 1) return;
    one = {
      x: ev.clientX, y: ev.clientY, mode: null,
      before: S.anchors.slice(), cy: S.layerCy
    };
  });
  c.addEventListener('pointermove', function (ev) {
    if (!pts.has(ev.pointerId)) return;
    pts.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
    if (two && pts.size >= 2) {
      // Pinch and twist come off the same two fingers, because on a
      // touchscreen they are one motion and forcing them into separate modes
      // makes both feel broken.
      var d = pdist(pts);
      if (d > 6 && two.d > 6) S.layerSpan = clamp(two.span * two.d / d, 16, 4096);
      var dphi = angdiff(pang(pts), two.ang);
      // Screen y runs downward, so a clockwise twist is +dphi, and a rotation
      // by phi displaces row y by -phi*(y-mid). The moving layer realises that
      // through `sgn`, so the stored roll carries the sign back out.
      var sgn = signFor(movingSide()) || 1;
      applyToCurve(two.before, 0, -dphi * (H - 1) * sgn,
        'rotated ' + (dphi * 180 / Math.PI).toFixed(2) + '°');
      renderLayerLabel();
      return;
    }
    if (!one) return;
    var dx = ev.clientX - one.x, dy = ev.clientY - one.y;
    if (!one.mode) {
      if (Math.hypot(dx, dy) < 7) return;
      one.mode = Math.abs(dx) >= Math.abs(dy) ? 'shift' : 'row';
    }
    var geo = layerGeo(c), rect = c.getBoundingClientRect();
    var cssScale = geo.scale * (rect.width / c.width);   // CSS px per panorama px
    if (one.mode === 'row') {
      S.layerCy = clamp(one.cy - dy / cssScale, 0, H - 1);
      drawLayers(); renderLayerLabel();
    } else {
      var sgn2 = signFor(movingSide()) || 1;
      var moved = (dx / cssScale) * sgn2;
      applyToCurve(one.before, moved, 0,
        'layers dragged ' + (moved >= 0 ? '+' : '') + moved.toFixed(2) + ' px');
    }
  });
  function release(ev) {
    pts.delete(ev.pointerId);
    if (pts.size < 2) two = null;
    if (pts.size === 0) one = null;
  }
  c.addEventListener('pointerup', release);
  c.addEventListener('pointercancel', release);
  c.addEventListener('lostpointercapture', release);
  c.addEventListener('wheel', function (ev) {
    ev.preventDefault();
    setLayerSpan(S.layerSpan * (ev.deltaY > 0 ? 1.12 : 0.89));
  }, { passive: false });
}

function bindGestures(c, kind) {
  var pts = new Map(), start = null, pinch = null;
  function rowAt(ev) {
    var g = geomFor(kind, c), rect = c.getBoundingClientRect();
    return clamp((ev.clientY - rect.top) / rect.height * g.rows + g.top, 0, H - 1);
  }
  function srcXAt(ev) {
    var g = geomFor(kind, c), rect = c.getBoundingClientRect();
    return (ev.clientX - rect.left) / rect.width * (rect.width / g.scale) + g.left;
  }
  c.addEventListener('pointerdown', function (ev) {
    try { c.setPointerCapture(ev.pointerId); } catch (e) { void e; }
    pts.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
    if (pts.size === 2) { pinch = { d: pdist(pts), span: S.spanX }; start = null; return; }
    if (pts.size !== 1) return;
    var row = rowAt(ev);
    S.cursorRow = row;
    start = {
      x: ev.clientX, y: ev.clientY, row: row, mode: null,
      onMoving: (srcXAt(ev) < STRIP) === (movingSide() === 'left'),
      i: nearestAnchor(row), before: S.anchors.slice()
    };
    syncControls(); draw();
  });
  c.addEventListener('pointermove', function (ev) {
    if (!pts.has(ev.pointerId)) return;
    pts.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
    if (pinch && pts.size >= 2) {
      var d = pdist(pts);
      if (d > 6 && pinch.d > 6) setSpan(pinch.span * pinch.d / d);
      return;
    }
    if (!start) return;
    var dx = ev.clientX - start.x, dy = ev.clientY - start.y;
    if (!start.mode) {
      if (Math.hypot(dx, dy) < 7) return;
      start.mode = Math.abs(dx) >= Math.abs(dy) ? 'shift' : 'row';
      if (start.mode === 'shift' && !start.onMoving) {
        // A grab only moves the half that can actually move. The other one is
        // drawn dimmed and labelled locked; letting it be dragged anyway would
        // make "locked" a decoration rather than a statement about hardware.
        start.mode = 'blocked';
        $('curvewhy').textContent = 'That half is locked. ' + (movingSide() === 'left'
          ? 'The camera can only warp the left half -- drag that one.'
          : 'The downstream corrector only rolls the right half -- drag that one.');
      }
    }
    var g = geomFor(kind, c);
    if (start.mode === 'row') {
      S.cursorRow = clamp(start.row - dy / g.scale, 0, H - 1);
      syncControls(); draw();
    } else if (start.mode === 'shift') {
      var sgn = signFor(movingSide()) || 1;
      var moved = (dx / g.scale) * sgn;
      if (S.grab === 'curve') {
        setAnchors(start.before.map(function (p) { return [p[0], p[1] + moved]; }),
          'whole curve dragged ' + (moved >= 0 ? '+' : '') + moved.toFixed(2) + ' px');
      } else {
        var a = start.before.slice(), i = start.i;
        a[i] = [a[i][0], a[i][1] + moved];
        setAnchors(a, 'anchor at y=' + a[i][0] + ' dragged directly');
      }
    }
  });
  function release(ev) {
    pts.delete(ev.pointerId);
    if (pts.size < 2) pinch = null;
    if (pts.size === 0) start = null;
  }
  c.addEventListener('pointerup', release);
  c.addEventListener('pointercancel', release);
  c.addEventListener('lostpointercapture', release);
  // Desktop convenience only; the primary path is the pinch.
  c.addEventListener('wheel', function (ev) {
    if (kind !== 'zoom') return;
    ev.preventDefault(); setSpan(S.spanX * (ev.deltaY > 0 ? 1.12 : 0.89));
  }, { passive: false });
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------
function surfaceChanged() {
  S.surface = $('owner').value === 'camera_mesh' ? 'camera_mesh' : 'downstream';
  var moving = movingSide();
  $('caption').innerHTML = moving === 'left'
    ? 'You are moving the <b>LEFT</b> image relative to the right. '
    + 'The camera can only move the left: the warp mesh lives in VPE&nbsp;0, and a '
    + 'measured +40&nbsp;px request moved the left half 40.1-40.9&nbsp;px while the '
    + 'right half moved 0.02&nbsp;px.'
    : 'You are moving the <b>RIGHT</b> image relative to the left, because the '
    + 'downstream corrector rolls the right half. The stored curve is unchanged &mdash; '
    + 'only which half you see move, and the direction it moves, have flipped.';
  $('applywrap').style.display = $('owner').value === 'downstream' ? 'none' : '';
  // The opacity slider always names the half it acts on, because which half is
  // on top flips with the surface and a slider labelled for the wrong layer is
  // worse than an unlabelled one.
  var al = $('alphaLbl');
  if (al) al.textContent = '(' + moving + ')';
  draw();
  drawLayers();
}

function onResize() {
  S.narrow = window.innerWidth < 980;
  draw();
}

function init() {
  S.narrow = window.innerWidth < 980;
  S.anchors = [[0, 0], [540, 0], [1080, 0], [1620, 0], [2159, 0]];
  syncControls();
  $('aim').addEventListener('click', aim);
  $('snap').addEventListener('click', snap);
  $('auto').addEventListener('click', autoMeasure);
  $('browse').addEventListener('click', browse);
  $('open').addEventListener('click', openFrame);
  $('save').addEventListener('click', save);
  $('apply').addEventListener('click', function () { applyToCamera(false); });
  $('dryrun').addEventListener('click', function () { applyToCamera(true); });
  $('camread').addEventListener('click', function () { readCamera(true); });
  // The two resets are one behaviour each, reachable from both the curve panel
  // and the layer panel -- an operator working the layers with a thumb should
  // not have to scroll back up to undo.
  function resetFactory() {
    // dx = 0 IS the factory mesh: the boot hook composes anchors onto the mesh
    // the firmware generates at boot, so an all-zero curve leaves it untouched.
    // "reset to zero" and "back to factory" were the same button, so there is
    // only one of them.
    setAnchors(S.anchors.map(function (p) { return [p[0], 0]; }),
      'back to factory — zero correction on the vendor mesh');
    S.layerBest = null;
  }
  function resetCameraCurrent() {
    var c = S.cameraCal;
    if (!c || !c.anchors_at_rows) return;
    setAnchors(c.anchors_at_rows, 'back to the calibration installed on the camera');
    S.layerBest = null;
  }
  $('reset').addEventListener('click', resetFactory);
  $('camcurrent').addEventListener('click', resetCameraCurrent);
  $('lreset').addEventListener('click', resetFactory);
  $('lcamcurrent').addEventListener('click', resetCameraCurrent);

  $('pulllayers').addEventListener('click', function () { pullLayers('auto'); });
  $('pullsynth').addEventListener('click', function () { pullLayers('synthetic'); });
  $('pullsensors').addEventListener('click', pullSensors);
  $('alpha').addEventListener('input', function (e) {
    S.layerAlpha = parseFloat(e.target.value) / 100;
    $('alphaV').textContent = Math.round(S.layerAlpha * 100) + '%';
    drawLayers();
  });
  var lmodes = document.querySelectorAll('#layermodes button');
  for (var lm = 0; lm < lmodes.length; lm++) {
    lmodes[lm].addEventListener('click', function (e) {
      S.layerMode = e.currentTarget.getAttribute('data-lmode');
      for (var k = 0; k < lmodes.length; k++) lmodes[k].classList.remove('on');
      e.currentTarget.classList.add('on');
      $('alpharow').style.display = S.layerMode === 'alpha' ? '' : 'none';
      drawLayers();
    });
  }
  var lnudges = document.querySelectorAll('#layernudge button');
  for (var ln = 0; ln < lnudges.length; ln++) {
    lnudges[ln].addEventListener('click', function (e) {
      applyToCurve(S.anchors.slice(),
        parseFloat(e.currentTarget.getAttribute('data-d')), 0, 'nudged from the layer view');
    });
  }
  bindLayerGestures($('layercv'));
  $('suggest').addEventListener('click', function () {
    // The swept minimum, not `implied_dx`. The sweep descends the objective
    // itself; the regression behind `implied_dx` returned -17, +159 and -7 px
    // on three real frames whose seams were all visibly fine.
    var q = S.quality;
    var s = (q && q.best_dx !== null && q.best_dx !== undefined) ? q.best_dx
      : (S.metrics && S.metrics.scr ? S.metrics.scr.suggested_dx : null);
    if (s === null || s === undefined) return;
    setAnchors(S.anchors.map(function (p) { return [p[0], s]; }),
      'set to the swept minimum, dx=' + s + ' px');
  });
  $('owner').addEventListener('change', surfaceChanged);
  $('roll').addEventListener('input', function (e) { setRoll(parseFloat(e.target.value)); });
  var nudges = document.querySelectorAll('.nudge button');
  for (var n = 0; n < nudges.length; n++) {
    nudges[n].addEventListener('click', function (e) {
      nudge(parseFloat(e.currentTarget.getAttribute('data-d')));
    });
  }
  var segs = document.querySelectorAll('#grabseg button');
  for (var s2 = 0; s2 < segs.length; s2++) {
    segs[s2].addEventListener('click', function (e) {
      S.grab = e.currentTarget.getAttribute('data-grab');
      for (var k = 0; k < segs.length; k++) segs[k].classList.remove('on');
      e.currentTarget.classList.add('on');
    });
  }
  var modes = document.querySelectorAll('.modes button');
  for (var i = 0; i < modes.length; i++) {
    modes[i].addEventListener('click', function (e) {
      S.mode = e.currentTarget.getAttribute('data-mode');
      for (var k = 0; k < modes.length; k++) modes[k].classList.remove('on');
      e.currentTarget.classList.add('on');
      draw();
    });
  }
  bindGestures($('zoom'), 'zoom');
  bindGestures($('view'), 'view');
  document.addEventListener('keydown', function (ev) {
    if (/^(INPUT|SELECT|TEXTAREA)$/.test((ev.target || {}).tagName || '')) return;
    var step = ev.shiftKey ? 1 : 0.25;
    if (ev.key === 'ArrowLeft') { nudge(-step); ev.preventDefault(); }
    if (ev.key === 'ArrowRight') { nudge(step); ev.preventDefault(); }
    if (ev.key === 'ArrowUp') { S.cursorRow = clamp(S.cursorRow - 20, 0, H - 1); draw(); ev.preventDefault(); }
    if (ev.key === 'ArrowDown') { S.cursorRow = clamp(S.cursorRow + 20, 0, H - 1); draw(); ev.preventDefault(); }
  });
  // ~2 Hz. Drives the fused view's blink and the layer view's, so the two
  // never disagree about which side is showing.
  setInterval(function () {
    var fused = (S.mode === 'blink' && S.imgs.left);
    var layers = (S.layerMode === 'blink' && S.layerImgs.left);
    if (!fused && !layers) return;
    S.blink ^= 1;
    if (fused) draw(); else drawLayers();
  }, 250);
  window.addEventListener('resize', onResize);
  window.addEventListener('orientationchange', onResize);
  if (!S.narrow) {
    var ds = document.querySelectorAll('details.panel');
    for (var d = 0; d < ds.length; d++) ds[d].open = true;
  }
  surfaceChanged();
  renderFreshness();
  pollState();
}
document.addEventListener('DOMContentLoaded', init);
"""


_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark">
<title>Soccer-Cam &middot; Seam Calibration</title>
<style>__STYLE__</style>
</head>
<body>
<div class="topbar"><div class="topbar-inner">
<div class="brand">SOCCER<span class="dot">&middot;</span>CAM</div>
<div class="crumb"><a href="/">Dashboard</a> / <a href="/config">Config</a> / Seam</div>
</div></div>

<div class="shell">
<h1 class="headline">Stitch seam calibration</h1>
<p class="lede">Built for a phone at the touchline: aim, put someone in the seam, snap,
judge the seam by eye, send the geometry back. The curve you author <em>is</em> the
calibration artifact &mdash; there is no separate export. This is a <b>check</b>-and-correct
tool for after a knock or a remount: on the archived games 52 of 96 frames sit below the
measurement floor and players straddling the seam are continuous, so
<b>&ldquo;nothing to change here&rdquo; is a common and perfectly good answer</b>.</p>

__BANNER__

<ol class="steps" id="steps">
  <li id="step-aim"><span class="n">1</span><span><span class="t">Aim</span>
    <span class="d">Tap Aim. The orange line is the seam. It is free to repeat, so use it
    to see where the seam lands in the scene.</span></span></li>
  <li id="step-stand"><span class="n">2</span><span><span class="t">Put someone in the seam</span>
    <span class="d">Stand them on the orange line, out where play actually happens &mdash;
    not beside the tripod. One calibration is exact at one depth, and a target at 2&nbsp;m
    carries tens of pixels of parallax that have nothing to do with the lens roll.</span></span></li>
  <li id="step-snap"><span class="n">3</span><span><span class="t">Snap</span>
    <span class="d">The full 7680&times;2160 still. Detection takes 25-60 s; the picture
    arrives first.</span></span></li>
  <li id="step-adjust"><span class="n">4</span><span><span class="t">Look at the person, then slide</span>
    <span class="d">Pinch to 4&times; and ask one question: is the body continuous across the
    seam? A tear is unmistakable. Drag across the picture to move the half; drag up and
    down to travel the rows. Your eye is the instrument here &mdash; the numbers beside it
    are aids, and none of them can see a person in the blend window.</span></span></li>
  <li id="step-ship"><span class="n">5</span><span><span class="t">Apply only if you saw a tear</span>
    <span class="d">Then snap again and confirm on a fresh frame rather than on a promise.
    If the body was continuous, leave the camera alone.</span></span></li>
</ol>

<div class="dock">
  <button class="btn btn-ghost" id="aim">Aim</button>
  <button class="btn" id="snap">Snap</button>
  <button class="btn btn-ghost" id="auto">Auto</button>
  <span class="st" id="dockstate">no frame loaded</span>
</div>

<div class="grid">
<div class="col">

  <div class="panel" id="campanel">
    <h2 class="sec">What the camera <span class="accent">already has</span></h2>
    <p class="hint">The snapshot is the mesh's <em>output</em> &mdash;
      <span class="mono">Snap</span> returns the fused panorama, after the warp and the
      stitcher &mdash; so it is not re-warped here. What is shown instead is the shape the
      camera's own optimiser chose, and the calibration installed on top of it.</p>
    <div id="camstate" class="st">not read yet</div>
    <div id="camnote" class="hint"></div>
    <canvas id="camprofile" width="320" height="150"
            style="width:100%;max-width:340px;display:none"></canvas>
    <p class="faint" id="camprofilecap" style="display:none">Source-x displacement the factory
      mesh applies down the seam column, per row. This is the vendor's stitch solution; your
      curve is composed on top of it.</p>
    <div class="btn-row">
      <button class="btn btn-ghost btn-sm" id="camread">Read camera</button>
    </div>
  </div>

  <div class="panel" id="autopanel" style="display:none">
    <h2 class="sec">Automatic <span class="accent">measurement</span></h2>
    <p class="hint"><strong>This does not propose a curve.</strong> The estimator is
      withdrawn: it turns out to measure a <em>step edge</em> &mdash; a shirt against
      grass &mdash; rather than a ghost, and at scale it accepts control corridors,
      where the true answer is exactly zero, almost as often as the seam. On the one
      hand-verified frame with a real 18&nbsp;px ghost it said 33&nbsp;px. The numbers
      are shown as evidence; calibrate by hand below.</p>
    <div id="autoverdict" class="st"></div>
    <div id="autodetail" class="hint"></div>
  </div>

  <div class="panel" id="scenepanel">
    <h2 class="sec">Where the seam <span class="accent">falls</span></h2>
    <div id="scenewrap" style="display:none">
      <img id="scene" alt="scene overview with the seam marked">
      <div id="sceneseam"></div>
      <div id="scenehint">the orange line is the seam &mdash; stand your target on it</div>
    </div>
    <div id="aimverdict" style="display:none"></div>
    <p class="faint">Aim re-snaps the camera and redraws this at 1/8 scale (~35&nbsp;KB), which is
      useless for judging registration and exactly right for judging aim. It never disturbs a
      measured session, so press it as often as you like while someone walks into position.</p>
  </div>

  <div class="panel" id="layerpanel">
    <h2 class="sec">Line up the <span class="accent">two layers</span></h2>
    <p class="hint">These are the two sensors' <em>own</em> contributions to the
      overlap, pulled before the camera cross-fades them &mdash; two real images of the
      same panorama columns. Overlay them and drag until they coincide. Unlike every
      other view on this page, nothing here is extrapolated.</p>
    <div class="btn-row">
      <button class="btn btn-sm" id="pulllayers">Pull layers</button>
      <button class="btn btn-ghost btn-sm" id="pullsynth">Self-test pair</button>
    </div>
    <div id="layerstate" class="st">no layer pair yet</div>
    <div id="layerattempts" class="faint mono"></div>

    <div id="layercvwrap"><canvas id="layercv"></canvas></div>
    <p class="faint mono" id="layerlabel"></p>

    <div class="modes" id="layermodes">
      <button data-lmode="alpha" class="on">Overlay</button>
      <button data-lmode="diff">Difference</button>
      <button data-lmode="anaglyph">Anaglyph</button>
      <button data-lmode="blink">Blink</button>
    </div>
    <label class="row" id="alpharow"><span class="lbl">Opacity of the moving layer
      <span class="val" id="alphaLbl">(left)</span>
      <span class="val" id="alphaV">50%</span></span>
      <input type="range" id="alpha" min="0" max="100" step="1" value="50"></label>

    <div class="layerread" id="layerread"></div>
    <p class="faint">Mean |L&minus;R| falls as the layers coincide and correlation rises
      toward 1.0 &mdash; both are computed in your browser on the pixels on screen, over
      the overlap only, so they answer the drag itself. They are aids: the picture is
      the instrument.</p>

    <div class="nudge" id="layernudge">
      <button data-d="-1">&minus;1</button>
      <button data-d="-0.25">&minus;&frac14;</button>
      <button data-d="0.25">+&frac14;</button>
      <button data-d="1">+1</button>
    </div>
    <p class="faint"><b>One finger across</b> translates &middot; <b>one finger up/down</b>
      travels the rows &middot; <b>two fingers</b> pinch to zoom and twist to rotate.
      Rotation is a ramp linear in the row &mdash; <span class="mono">dx = &minus;&theta;&middot;y</span>,
      the shape a relative lens roll actually makes.</p>
    <div class="btn-row">
      <button class="btn btn-ghost btn-sm" id="lreset">Back to factory</button>
      <button class="btn btn-ghost btn-sm" id="lcamcurrent" disabled>Back to camera current</button>
    </div>
  </div>

  <div class="panel" id="sensorpanel">
    <h2 class="sec">What each lens <span class="accent">sees</span></h2>
    <p class="hint">Two whole-lens stills, for orientation before precision work &mdash; what each
      lens is actually pointed at, which a 128&nbsp;px strip cannot show you.
      <b>Context, not the alignment surface.</b> They arrive at each sensor's native width,
      which puts them in sensor coordinates, before the warp; the mapping into panorama
      columns has been measured (to a few grey levels) but is not wired into this build, so
      the tool will not let you author a correction from them. Use the layer panel above &mdash;
      it is pre-rectified by the camera itself, so it carries no fitted-warp error at all.</p>
    <div class="btn-row">
      <button class="btn btn-ghost btn-sm" id="pullsensors">Show both lenses</button>
    </div>
    <div id="sensorstate" class="st">not pulled</div>
    <div id="sensorwrap" style="display:none">
      <div class="lenscap">sensor 0 &mdash; left</div>
      <img id="sensorL" class="lens" alt="whole view from sensor 0">
      <div class="lenscap">sensor 1 &mdash; right</div>
      <img id="sensorR" class="lens" alt="whole view from sensor 1">
    </div>
  </div>

  <div class="panel">
    <h2 class="sec">The <span class="accent">seam</span></h2>
    <div id="zoomwrap"><canvas id="zoom"></canvas></div>
    <p class="faint mono" id="zoomlabel"></p>
    <div class="modes">
      <button data-mode="blink" class="on">Blink</button>
      <button data-mode="anaglyph">Anaglyph</button>
      <button data-mode="mirror">Mirror</button>
      <button data-mode="split">Plain</button>
    </div>
    <p class="faint">Drag <b>across</b> to move the half you are allowed to move; drag
      <b>up and down</b> to travel the rows; <b>pinch</b> to zoom. Arrow keys nudge by
      0.25&nbsp;px (shift = 1&nbsp;px) if you are on a laptop.</p>
    <div id="viewwrap"><canvas id="view"></canvas></div>
    <p class="faint mono">Whole frame, squashed vertically: a locator, not a judge. Green bands
      are rows where something upright crosses the seam; the white box is what the view above is
      showing.</p>
    <div class="caption" id="caption"></div>
    <div id="vertical" style="display:none"></div>
    <div class="caveat"><strong>These views are inference; the layer panel above is not.</strong>
      A <span class="mono">Snap</span> is already fused &mdash; every pixel in the shaded window
      is a mixture of both sensors &mdash; so blink and anaglyph <em>here</em> cannot reveal a
      second layer and do not pretend to. They draw the <em>fitted</em> structures instead: each
      measured independently on the left shoulder (red) and the right shoulder (cyan) and
      extrapolated to the seam, solid where there is real image behind it and dashed where it is
      extrapolation. A gap between a red line and its cyan partner is that structure's residual.
      Structures too near horizontal to respond to a horizontal shift are drawn grey, because
      they cannot tell you anything about what you are adjusting.
      <br><br>The <b>Line up the two layers</b> panel above does not have this limitation: it
      pulls the two sensors' contributions <em>before</em> the cross-fade, so its overlay,
      difference, anaglyph and blink are the true separated layers rather than an
      extrapolation. Prefer it. This view remains useful for the shoulders either side of the
      window, which are real image in both.</div>
  </div>

  <div class="panel">
    <h2 class="sec">The <span class="accent">curve</span></h2>
    <div class="dxread" id="dxnow"></div>
    <div class="nudge">
      <button data-d="-1">&minus;1</button>
      <button data-d="-0.25">&minus;&frac14;</button>
      <button data-d="0.25">+&frac14;</button>
      <button data-d="1">+1</button>
    </div>
    <label class="row"><span class="lbl">Roll &mdash; linear in y, top to bottom
      <span class="val" id="rollV"></span></span>
      <input type="range" id="roll" min="-40" max="40" step="0.25" value="0"></label>
    <div class="seg" id="grabseg">
      <button data-grab="curve" class="on">Drag moves the whole curve</button>
      <button data-grab="anchor">Drag moves this row</button>
    </div>
    <p class="faint">Roll is the shape the physics predicts: two lenses rotated relative to each
      other about their optical axes displace the seam by
      <span class="mono">dx = -&theta;&middot;y</span>, linear in the row. With one person at one
      depth you have one observation and can only honestly move the whole curve; a second person
      at a different height is what earns a roll.</p>
    <p class="faint" id="curvewhy"></p>
    <div class="btn-row">
      <button class="btn btn-ghost btn-sm" id="reset">Back to factory</button>
      <button class="btn btn-ghost btn-sm" id="camcurrent" disabled>Back to camera current</button>
      <button class="btn btn-ghost btn-sm" id="suggest" disabled>Use measured dx</button>
    </div>
  </div>

  <div class="panel">
    <h2 class="sec">Live <span class="accent">score</span>
      <span class="badge" id="fresh" style="float:right">no frame</span></h2>
    <p class="mono faint"><span class="dot off" id="metricdot"></span><span id="metriclabel"></span></p>
    <div id="floor" style="display:none"></div>
    <div id="quality" style="display:none"></div>
    <div class="kpi" id="kpis">
      <div><div class="k">|ln SSR| on the target</div><div class="v" id="ssrtarget">--</div></div>
      <div><div class="k">|ln SSR| whole frame</div><div class="v" id="ssrv">--</div></div>
      <div><div class="k">SCR p90, steerable</div><div class="v" id="scrp90">--</div></div>
      <div><div class="k">steerable / all</div><div class="v" id="scrn">0</div></div>
    </div>
    <p class="mono faint" id="scoredat"></p>
    <p class="mono worse" id="neterr"></p>
    <p class="faint" id="scrnote"></p>
    <p class="faint" id="ssrnote"></p>
    <p class="faint">These are the same numbers the automatic solver minimises, computed by the
      same code &mdash; and that solver <b>refuses on all 27 archived games</b>, because the
      structures able to span a vertical seam are the near-horizontal ones and their median
      |slope| is 0.034: a 10&nbsp;px error moves the median observation 0.34&nbsp;px, under its
      own noise. So treat every number on this panel as a secondary aid. The picture is the
      evidence, and it should overrule them. The headline p90 is taken over the structures
      steep enough to see a horizontal shift; the whole-set p90 is printed beside it, and
      neither is redefined anywhere the solver would read.</p>
  </div>

</div>
<div class="col">

  <details class="panel">
    <summary>Correction <span class="accent">surface</span></summary>
    <select id="owner">
      <option value="camera_mesh">Camera warp mesh &mdash; sub-pixel, before the blend</option>
      <option value="camera_scalars+downstream">Camera scalars + downstream residual</option>
      <option value="downstream">Downstream corrector only &mdash; whole-pixel, after the blend</option>
    </select>
    <p class="faint">Exactly one surface owns the correction. Splitting it across two is how a
      reboot silently turns a full correction into half of one: the camera's mesh is runtime
      state and does not survive a power cycle, while the downstream profile always does.</p>
  </details>

  <details class="panel">
    <summary>Ship <span class="accent">it</span></summary>
    <label class="row"><span class="lbl">Deployment / tripod placement</span>
      <input type="text" id="deployment" placeholder="fairport-north-2026.08"></label>
    <label class="row"><span class="lbl">Calibrated for distance (m)</span>
      <input type="number" id="distance_m" step="1" inputmode="decimal" placeholder="45"></label>
    <label class="row"><span class="lbl">Basis</span>
      <input type="text" id="basis" placeholder="player standing at the far touchline"></label>
    <p class="faint">One calibration is correct at one depth &mdash; whichever depth the person you
      used was standing at. Calibrating for the far field costs a couple of pixels across the far
      half of the pitch and tens of pixels at the near touchline: the right trade here, but a
      stated one. Every save is also filed under the deployment label in
      <span class="mono">stitch_calibrations/</span>.</p>
    <div class="btn-row">
      <button class="btn" id="save">Save profile</button>
      <span class="mono" id="savestate"></span>
    </div>
    <div id="applywrap" style="margin-top:12px">
      <div class="btn-row">
        <button class="btn btn-ghost" id="dryrun">Dry run</button>
        <button class="btn btn-danger" id="apply">Apply to camera</button>
        <span class="mono" id="applystate"></span>
      </div>
      <p class="faint">Apply sets the vendor scalars first and only then writes a mesh composed
        onto the baseline they produce &mdash; <span class="mono">SetStitch</span> re-runs the
        camera's own mesh optimiser and destroys anything written before it. Afterwards a fresh
        snapshot is pulled and re-scored, so you see numbers rather than a promise.</p>
      <pre class="mono faint" id="applyreport" style="white-space:pre-wrap;max-height:200px;overflow:auto"></pre>
    </div>
  </details>

  <details class="panel">
    <summary>The <span class="accent">anchors</span></summary>
    <table><thead><tr><th>row</th><th>dx (px, right half moves right)</th></tr></thead>
      <tbody id="curverows"></tbody></table>
    <p class="mono faint" style="word-break:break-all" id="curvejson"></p>
  </details>

  <details class="panel">
    <summary>What the camera <span class="accent">already decided</span></summary>
    <table><thead><tr><th>scalar</th><th class="num">live</th><th class="num">factory</th></tr></thead>
      <tbody id="scalars"></tbody></table>
    <p class="faint" id="scalarnote"></p>
    <p class="mono faint">source <b id="camname">--</b> &middot; <b id="camhost">--</b></p>
  </details>

  <details class="panel">
    <summary>Calibrate from a <span class="accent">recorded game</span></summary>
    <p class="faint">The other door. A calibration can be recovered after the fact from an
      archived game &mdash; useful for footage already shot, and for checking whether the seam
      differs between tripod placements.</p>
    <label class="row"><span class="lbl">Folder of frames or recordings</span>
      <input type="text" id="framedir" placeholder="F:\\archive\\duo3_stitch\\frames\\..."></label>
    <div class="btn-row" style="margin-top:8px">
      <button class="btn btn-ghost btn-sm" id="browse">List</button>
      <select id="framelist" style="flex:2;min-width:160px"></select>
      <input type="number" id="atsec" placeholder="sec" step="1" inputmode="numeric"
        style="width:88px" title="seconds into a video file">
      <button class="btn btn-sm" id="open">Open frame</button>
    </div>
    <p class="mono faint" id="openstate"></p>
  </details>

  <details class="panel">
    <summary>Reaching this <span class="accent">from a phone</span></summary>
    <p class="faint">The app answers on loopback by default and rejects any other
      <span class="mono">Host</span> header, which is a deliberate DNS-rebinding defence. To use it
      from a phone on the same network, set <span class="mono">[TTT] auth_server_bind</span> in
      <span class="mono">config.ini</span> to this machine's LAN address and browse to
      <span class="mono">http://&lt;that address&gt;:8765/stitch</span>. Setting it to
      <span class="mono">0.0.0.0</span> binds everywhere but does <em>not</em> widen the allowlist,
      so it will still refuse &mdash; name the address.</p>
  </details>

</div>
</div>
</div>
<script>__SCRIPT__</script>
</body>
</html>
""".replace("__STYLE__", _STYLE).replace("__SCRIPT__", _SCRIPT)
