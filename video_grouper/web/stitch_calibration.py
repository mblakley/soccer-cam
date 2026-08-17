"""Human-in-the-loop stitch-seam calibration, at ``/stitch``.

Workflow B of ``reolink-firmware-patching/docs/STITCH_CALIBRATION.md``: get a
panorama frame, let an operator slide the two halves into registration by eye
while the seam metric scores every candidate curve, and send the result back.
The curve the operator authors **is** the calibration artifact -- there is no
export step and no parameter that lives only in the UI.

Two doors, one editor: a live `Snap` over HTTP, or a frame opened from a file
or seeked out of a recording. The second is the one most calibrations will come
through. Nobody stands at the touchline with a laptop mid-match, and a camera
indoors on a bench is looking at a scene 0.3 m away, where parallax runs to
tens of pixels and swamps the lens roll this exists to correct.

Four things about this file are load-bearing and easy to get wrong.

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

**The picture is a fused JPEG, so parts of it are already a mixture.** The
camera blends the two sensors over a 256-px window, so nothing inside it is
evidence about registration and there is no second layer to reveal. Blink and
anaglyph therefore draw the *fitted* structures -- the same extrapolation the
metric performs -- rather than smearing replicated pixels across the window.
That is stated in the interface, not just here: an honest caveat in a source
comment is a caveat nobody reads.

What crosses the wire is two 640x2160 JPEG strips (~76 KB each), not the
730 KB panorama: the operator only ever looks at the seam, and the browser can
shear a strip with an affine transform far more smoothly than a server can
re-render one per drag frame.
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
    """Import `seam_metric` / `stitch_apply`, or explain why not.

    Returns a dict with ``metric`` and ``camera`` (module or None) plus
    ``errors``. The page degrades rather than 500s: the metric and the camera
    surface fail independently, and an operator with neither can still read the
    documentation the page carries.
    """
    if _toolkit_cache:
        return _toolkit_cache
    out: dict[str, Any] = {"metric": None, "camera": None, "errors": []}
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
    for key, name in (("metric", "seam_metric"), ("camera", "stitch_apply")):
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
        quality = _frame_quality(metric, chains, frame.shape[0])
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
        session.baseline = _score_payload(scr, ssr)
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


def _frame_quality(metric: Any, chains: Any, height: int) -> dict:
    """Ask the frame whether it can constrain the calibration at all.

    This exists because of what real material does. On three separate Duo 3
    tripod placements -- an indoor dome and two outdoor pitches -- SCR reports
    p90 between 27 and 36 px and moves by 1-15% as dx sweeps the whole
    plausible range, while the seam is *visibly* well registered. Every
    coverage gate passes (40-69 structures, 3 row bands, 69-79% height), so
    nothing in the acceptance check flags it.

    What is happening: the matcher pairs a left structure with any right
    structure within 40 px at the seam, and a busy scene at mixed depths
    offers plenty. The resulting "observations" are unrelated edges, and their
    residuals describe the matching tolerance rather than the registration.

    So a number alone is not honest. Sweeping dx once per frame answers the
    question the operator actually has -- *can this picture tell me anything*
    -- and it is the same question the automatic solver has to answer before
    it trusts a fit.
    """
    sweep = []
    for dx in range(-16, 17, 2):
        r = metric.residual_from_chains(
            chains, [(0.0, float(dx)), (height - 1.0, float(dx))]
        )
        if r.n:
            sweep.append({"dx": dx, "p50": round(r.p50, 3), "p90": round(r.p90, 3)})
    if not sweep:
        return {"usable": False, "reason": "no structure crosses the seam", "sweep": []}

    at_zero = next((s for s in sweep if s["dx"] == 0), sweep[0])
    best = min(sweep, key=lambda s: s["p90"])
    gain = 0.0 if at_zero["p90"] <= 0 else 1.0 - best["p90"] / at_zero["p90"]

    base = metric.residual_from_chains(chains)
    saturated = [o for o in base.observations if o.residual_perp > 0.8 * _MATCH_GAP_PX]
    sat_frac = len(saturated) / base.n if base.n else 0.0

    usable = gain >= _MIN_USEFUL_GAIN
    if usable:
        reason = (
            f"sliding dx across +/-16 px moves SCR p90 by {gain * 100:.0f}%, "
            f"best at dx={best['dx']:+d}"
        )
    elif sat_frac > 0.4:
        reason = (
            f"{sat_frac * 100:.0f}% of matched structures sit near the "
            f"{_MATCH_GAP_PX:.0f} px pairing limit, so the score is dominated by "
            "unrelated edges paired across the seam, not by misregistration"
        )
    else:
        reason = (
            f"sliding dx across the whole plausible range moves SCR p90 by only "
            f"{gain * 100:.0f}% -- this frame does not constrain the seam offset"
        )
    return {
        "usable": usable,
        "reason": reason,
        "best_dx": best["dx"] if usable else None,
        "gain": round(gain, 4),
        "saturated_fraction": round(sat_frac, 3),
        "sweep": sweep,
    }


def _score_payload(scr: Any, ssr: Any) -> dict:
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
        "ssr": {
            "ssr": round(ssr.ssr, 4),
            "abs_ln_ssr": round(ssr.abs_ln_ssr, 4),
            "noise_floor": ssr.noise_floor,
        },
        # The structures the score is computed from, so the picture and the
        # number are the same evidence. Each is one structure fitted
        # independently on the two shoulders and extrapolated to the seam;
        # `y_left` and `y_right` are where those two extrapolations arrive.
        # Drawing them IS the "alternate the two extrapolated shoulders" view,
        # and it is honest in a way extrapolating pixels is not: the metric
        # really does extrapolate lines, and a line drawn on screen cannot be
        # mistaken for photographic evidence the fused frame does not contain.
        "observations": [
            {
                "y_left": round(o.y_left, 2),
                "y_right": round(o.y_right, 2),
                "slope": round(o.slope, 5),
                "residual": round(o.residual_perp, 2),
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
    halves = _encode_halves(frame, frame.shape[1] // 2)
    with _session.lock:
        _session.version += 1
        version = _session.version
        _session.camera_name = camera_name
        _session.host = host
        _session.source_path = source_path
        _session.frame = frame
        _session.halves = halves
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

    @router.post("/stitch/snap")
    def post_snap() -> JSONResponse:
        """Fetch a fresh still and the camera's own scalars, in one action.

        `GetStitch` returns both the live values and the factory `initial`
        block, so "confirm what the camera's auto-adjustment already found" is
        a real comparison rather than a leap of faith.
        """
        toolkit = load_toolkit()
        camera_mod, metric = toolkit["camera"], toolkit["metric"]
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

        try:
            current, factory = camera_mod.get_stitch(host, cam.username, cam.password)
            scalars = (current.to_api(), factory.to_api())
        except Exception as exc:  # noqa: BLE001
            logger.warning("STITCH: GetStitch failed: %s", exc)
            scalars = (None, None)

        del metric
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
        if chains is None or frame is None:
            raise HTTPException(409, "no measured snapshot yet")

        seam = width // 2
        scr = metric.residual_from_chains(chains, anchors)
        ssr = _ssr_for_anchors(metric, frame, seam, height, anchors)
        payload = _score_payload(scr, ssr)
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
    metric: Any, frame: Any, seam: int, height: int, anchors: list[tuple[float, float]]
) -> Any:
    """SSR of the frame as the candidate curve would leave it.

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
    return metric.seam_sharpness_ratio(shifted, seam_x=seam - lo, blend_w=2 * BLEND_W)


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
        "last_apply": _session.last_apply,
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
  --display:'Barlow Condensed','Bebas Neue',sans-serif;
  --body:'IBM Plex Sans',system-ui,sans-serif;
  --mono:'IBM Plex Mono',ui-monospace,monospace;
}
* { box-sizing:border-box; }
body {
  margin:0; font-family:var(--body); font-size:14px; line-height:1.55;
  color:var(--text); background:var(--bg-base);
}
.topbar { border-bottom:1px solid var(--rule); background:rgba(10,11,15,.72); }
.topbar-inner {
  max-width:1400px; margin:0 auto; padding:14px 24px;
  display:flex; align-items:center; justify-content:space-between;
}
.brand {
  font-family:var(--display); font-weight:700; letter-spacing:.18em;
  font-size:18px; text-transform:uppercase;
}
.brand .dot { color:var(--accent); }
.crumb {
  font-family:var(--mono); font-size:11px; letter-spacing:.16em;
  text-transform:uppercase; color:var(--text-mute);
}
.crumb a { color:var(--text-mute); text-decoration:none; }
.crumb a:hover { color:var(--accent); }
.shell { max-width:1400px; margin:0 auto; padding:24px 24px 80px; }
.headline {
  font-family:var(--display); font-weight:700; text-transform:uppercase;
  letter-spacing:.04em; font-size:clamp(32px,4vw,48px); line-height:.95; margin:0 0 6px;
}
.lede { color:var(--text-mute); max-width:70ch; margin:0 0 20px; }
h2.sec {
  font-family:var(--display); font-weight:700; text-transform:uppercase;
  letter-spacing:.06em; font-size:19px; margin:0 0 12px;
}
h2.sec .accent { color:var(--accent); }
.grid { display:grid; grid-template-columns:minmax(0,1fr) 380px; gap:24px; align-items:start; }
@media (max-width:1080px) { .grid { grid-template-columns:minmax(0,1fr); } }
.panel {
  background:var(--bg-surface); border:1px solid var(--rule);
  padding:16px 18px; margin-bottom:18px;
}
.mono { font-family:var(--mono); font-size:12px; }
.muted { color:var(--text-mute); }
.faint { color:var(--text-faint); font-size:12px; }
.btn {
  font-family:var(--display); text-transform:uppercase; letter-spacing:.09em;
  font-weight:600; font-size:14px; padding:8px 16px; cursor:pointer;
  background:var(--accent); color:#141414; border:0;
}
.btn:hover { filter:brightness(1.12); }
.btn[disabled] { background:var(--rule-strong); color:var(--text-faint); cursor:not-allowed; }
.btn-ghost { background:transparent; color:var(--accent); border:1px solid var(--accent); }
.btn-ghost:hover { background:rgba(251,146,60,.12); }
.btn-danger { background:transparent; color:var(--signal-bad); border:1px solid var(--signal-bad); }
.btn-row { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
canvas { display:block; background:#000; image-rendering:pixelated; }
#viewwrap { border:1px solid var(--rule); position:relative; }
#view { width:100%; height:auto; cursor:ew-resize; }
#zoomwrap { border:1px solid var(--rule); margin-top:12px; position:relative; }
#zoom { width:100%; height:auto; }
.modes { display:flex; gap:6px; flex-wrap:wrap; margin:12px 0 8px; }
.modes button {
  font-family:var(--mono); font-size:11px; letter-spacing:.1em; text-transform:uppercase;
  padding:6px 11px; background:var(--bg-elev); color:var(--text-mute);
  border:1px solid var(--rule); cursor:pointer;
}
.modes button.on { color:var(--accent); border-color:var(--accent); }
.caption {
  font-family:var(--mono); font-size:12px; letter-spacing:.04em;
  padding:9px 12px; margin-top:10px;
  border-left:3px solid var(--accent); background:var(--bg-elev); color:var(--text);
}
.caveat {
  font-size:12.5px; padding:10px 12px; margin-top:10px;
  border-left:3px solid var(--signal-warn); background:rgba(251,191,36,.07);
  color:var(--text-mute);
}
.caveat strong { color:var(--signal-warn); }
label.row { display:block; margin:14px 0 4px; }
label.row .lbl {
  font-family:var(--mono); font-size:11px; letter-spacing:.12em;
  text-transform:uppercase; color:var(--text-mute);
  display:flex; justify-content:space-between; align-items:baseline;
}
label.row .val { color:var(--accent); font-weight:600; }
input[type=range] { width:100%; margin:6px 0 0; accent-color:var(--accent); }
input[type=number], select {
  background:var(--bg-input); color:var(--text); border:1px solid var(--rule);
  padding:5px 7px; font-family:var(--mono); font-size:12px; width:100%;
}
input[type=number]:focus, select:focus { outline:1px solid var(--accent); border-color:var(--accent); }
table { border-collapse:collapse; width:100%; font-family:var(--mono); font-size:12px; }
th, td { padding:5px 7px; border-bottom:1px solid var(--rule); text-align:left; }
th { color:var(--text-faint); font-weight:600; text-transform:uppercase; letter-spacing:.1em; font-size:10px; }
td.num { text-align:right; }
.kpi { display:grid; grid-template-columns:repeat(2,1fr); gap:10px; margin-bottom:8px; }
.kpi div { background:var(--bg-elev); border:1px solid var(--rule); padding:8px 10px; }
.kpi .k {
  font-family:var(--mono); font-size:10px; letter-spacing:.12em;
  text-transform:uppercase; color:var(--text-faint);
}
.kpi .v { font-family:var(--display); font-size:26px; line-height:1.1; }
.kpi .d { font-family:var(--mono); font-size:11px; }
.better { color:var(--signal-on); }
.worse { color:var(--signal-bad); }
.flat { color:var(--text-faint); }
.flash { padding:10px 14px; margin:0 0 16px; border-left:3px solid var(--signal-bad);
  background:rgba(244,63,94,.08); font-size:13px; }
.flash ul { margin:6px 0 0 16px; padding:0; }
.flash-ok { border-left-color:var(--signal-on); background:rgba(34,197,94,.08); }
.dot { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:6px; }
.dot.on { background:var(--signal-on); } .dot.off { background:var(--signal-off); }
.dot.warn { background:var(--signal-warn); } .dot.bad { background:var(--signal-bad); }
.locked { color:var(--text-faint); }
"""


_SCRIPT = r"""
'use strict';
// ---------------------------------------------------------------------------
// Model. The anchor curve is the ONLY authored state: the translate and roll
// sliders are a decomposition of it (mean, best-fit ramp), recomputed whenever
// the curve changes by any route. Nothing the operator can move exists outside
// the artifact.
// ---------------------------------------------------------------------------
var S = {
  st: null, anchors: [], surface: 'camera_mesh', mode: 'blink',
  imgs: {}, blink: 0, cursorRow: 1080, dragging: null, ready: false,
  metrics: null, baseline: null, busy: false, pending: false
};
// Geometry is taken from the frame the server actually fetched, never assumed.
// These are only the pre-snapshot defaults.
var H = 2160, STRIP = 640, BLENDH = 128, ZOOM = 4, ZOOM_ROWS = 400;

function $(id) { return document.getElementById(id); }
function fmt(v, d) { return (v === null || v === undefined || isNaN(v)) ? '--' : v.toFixed(d === undefined ? 2 : d); }

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
// one GPU-composited draw call instead of 2160 per-row blits.
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
// score is computed from. A gap between a red line and its cyan partner at the
// seam IS that structure's residual, to sub-pixel precision, and no smear of
// replicated columns comes close to showing it that clearly.
function drawShoulders(view, which) {
  var obs = (S.metrics && S.metrics.observations) || [];
  if (!obs.length) return;
  var ctx = view.ctx;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.lineWidth = Math.max(1, view.sy * 1.5);
  var seam = STRIP, reach = BLENDH * 2;
  for (var i = 0; i < obs.length; i++) {
    var o = obs[i];
    // slope is dy/dx in source px; the view scales the axes differently, so
    // the drawn slope has to be scaled too or the lines would lie.
    [['left', o.y_left, '#ff5a5a'], ['right', o.y_right, '#48e0ff']].forEach(function (t) {
      if (which && which !== t[0]) return;
      var side = t[0], yAt = t[1];
      var x0 = side === 'left' ? seam - reach : seam;
      var x1 = side === 'left' ? seam : seam + reach;
      var X = function (x) { return (x - view.ox) * view.sx; };
      var Y = function (x) { return (yAt + o.slope * (x - seam) - view.oy) * view.sy; };
      ctx.strokeStyle = t[2];
      ctx.globalAlpha = 0.9;
      ctx.beginPath(); ctx.moveTo(X(x0), Y(x0)); ctx.lineTo(X(x1), Y(x1)); ctx.stroke();
      // dashed continuation across the window: this part is extrapolation.
      ctx.setLineDash([4, 4]); ctx.globalAlpha = 0.55;
      var x2 = side === 'left' ? seam + reach : seam - reach;
      ctx.beginPath(); ctx.moveTo(X(x1 === seam ? seam : seam), Y(seam));
      ctx.lineTo(X(x2), Y(x2)); ctx.stroke();
      ctx.setLineDash([]);
    });
  }
  ctx.globalAlpha = 1;
}

function paint(view, grid) {
  var ctx = view.ctx;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, view.c.width, view.c.height);
  ctx.fillStyle = '#000'; ctx.fillRect(0, 0, view.c.width, view.c.height);
  if (!S.imgs.left || !S.imgs.right) return;

  // Both halves are always drawn at their true positions. Only the 256-px
  // blend window changes between views -- blanking a whole half would be a
  // flash, and what a blink has to reveal is a few pixels of jump.
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
  var sxOf = function (x) { return (x - view.ox) * view.sx; };
  ctx.fillStyle = 'rgba(251,146,60,.10)';
  ctx.fillRect(sxOf(STRIP - BLENDH), 0, 2 * BLENDH * view.sx, view.c.height);
  ctx.strokeStyle = 'rgba(251,146,60,.85)'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(sxOf(STRIP) + .5, 0); ctx.lineTo(sxOf(STRIP) + .5, view.c.height); ctx.stroke();

  if (grid) {
    ctx.strokeStyle = 'rgba(255,255,255,.13)';
    for (var x = STRIP - BLENDH; x <= STRIP + BLENDH; x++) {
      var px = Math.round(sxOf(x)) + .5;
      ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, view.c.height); ctx.stroke();
    }
  }
  // the locked half is dimmed, always, so the hardware constraint is visible
  var lockedLeft = movingSide() === 'right';
  ctx.fillStyle = 'rgba(0,0,0,.42)';
  ctx.fillRect(lockedLeft ? 0 : sxOf(STRIP), 0,
    lockedLeft ? sxOf(STRIP) : view.c.width - sxOf(STRIP), view.c.height);
  // row cursor
  ctx.strokeStyle = 'rgba(34,197,94,.8)';
  var cy = (S.cursorRow - view.oy) * view.sy;
  ctx.beginPath(); ctx.moveTo(0, cy); ctx.lineTo(view.c.width, cy); ctx.stroke();
}

function draw() {
  var v = $('view'), z = $('zoom');
  var dpr = Math.min(window.devicePixelRatio || 1, 2);
  var cssW = v.clientWidth || 900;
  v.width = Math.round(cssW * dpr); v.height = Math.round(660 * dpr);
  v.style.height = '660px';
  paint(new View(v, 0, 0, v.width / (2 * STRIP), v.height / H), false);

  var zw = z.clientWidth || 900;
  z.width = Math.round(zw * dpr);
  var zs = z.width / (2 * BLENDH * ZOOM);          // display px per (source px * ZOOM)
  var scale = zs * ZOOM;                            // display px per source px
  z.height = Math.round(ZOOM_ROWS * scale);
  z.style.height = Math.round(z.height / dpr) + 'px';
  var top = Math.max(0, Math.min(H - ZOOM_ROWS, S.cursorRow - ZOOM_ROWS / 2));
  paint(new View(z, STRIP - BLENDH, top, scale, scale), true);
  $('zoomlabel').textContent = 'rows ' + Math.round(top) + '-' + Math.round(top + ZOOM_ROWS)
    + ' @ ' + ZOOM + 'x, true aspect';
  $('viewscale').textContent = (v.width / dpr / (2 * STRIP)).toFixed(2);
}

// ---------------------------------------------------------------------------
// Curve editing
// ---------------------------------------------------------------------------
function setAnchors(a, why) {
  S.anchors = a.map(function (p) { return [p[0], Math.max(-64, Math.min(64, p[1]))]; });
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
  $('translate').value = t.toFixed(2); $('translateV').textContent = fmt(t) + ' px';
  $('roll').value = r.toFixed(2); $('rollV').textContent = fmt(r) + ' px top-to-bottom';
  var rows = S.st ? S.st.anchor_rows : [0, 540, 1080, 1620, 2159];
  var html = '';
  for (var i = 0; i < S.anchors.length; i++) {
    html += '<tr><td class="muted">y ' + S.anchors[i][0] + '</td><td>'
      + '<input type="number" step="0.25" data-i="' + i + '" class="anch" value="'
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
  void rows;
}

function nudge(delta) {
  setAnchors(S.anchors.map(function (p) { return [p[0], p[1] + delta]; }),
    'translate ' + (delta > 0 ? '+' : '') + delta.toFixed(2) + ' px');
}
function setRoll(amp) {
  var cur = rollAmp(), d = amp - cur, mid = (H - 1) / 2;
  setAnchors(S.anchors.map(function (p) {
    return [p[0], p[1] + d * (p[0] - mid) / (H - 1)];
  }), 'roll set to ' + fmt(amp) + ' px top-to-bottom');
}

// ---------------------------------------------------------------------------
// Metrics
// ---------------------------------------------------------------------------
var measureTimer = null;
function scheduleMeasure() {
  if (measureTimer) clearTimeout(measureTimer);
  measureTimer = setTimeout(measure, 160);
}
function measure() {
  if (!S.ready) return;
  if (S.busy) { S.pending = true; return; }
  S.busy = true; $('metricstate').textContent = 'scoring...';
  fetch('/stitch/measure', {
    method: 'POST', credentials: 'same-origin',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ dx_anchors: S.anchors })
  }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
    .then(function (res) {
      S.busy = false;
      if (!res.ok) { $('metricstate').textContent = res.j.detail || 'scoring failed'; return; }
      S.metrics = res.j; S.baseline = res.j.baseline || S.baseline;
      renderMetrics(); $('metricstate').textContent = '';
      if (S.pending) { S.pending = false; measure(); }
    }).catch(function (e) { S.busy = false; $('metricstate').textContent = String(e); });
}
function delta(now, before, unit) {
  if (now === null || before === null || now === undefined || before === undefined) return '';
  var d = now - before;
  var cls = Math.abs(d) < 1e-6 ? 'flat' : (d < 0 ? 'better' : 'worse');
  return '<span class="' + cls + '">' + (d >= 0 ? '+' : '') + d.toFixed(2) + ' ' + unit + '</span>';
}
// Whether this frame can constrain the calibration at all, stated before any
// number is. Real tripod frames routinely produce a large SCR that barely
// responds to dx -- unrelated edges paired across the seam -- and every
// coverage gate passes while it happens. A precise-looking number on a frame
// that cannot steer is worse than no number.
function renderQuality(q) {
  var el = $('quality');
  if (!q) { el.style.display = 'none'; return; }
  el.style.display = '';
  S.quality = q;
  if (q.usable) {
    el.className = 'caption';
    el.innerHTML = '<b>This frame can steer the calibration.</b> ' + q.reason
      + '. Drag until SCR p90 stops falling.';
  } else {
    el.className = 'caveat';
    el.innerHTML = '<strong>This frame cannot steer the calibration.</strong> ' + q.reason
      + '. Read SCR here as a bound, not as the seam offset, and do not trust a curve '
      + 'tuned against it &mdash; find a frame with a clear structure crossing the seam '
      + '(a field line, a goal frame, a fence) at roughly the depth you care about.';
  }
  $('suggest').disabled = (q.best_dx === null || q.best_dx === undefined);
}

function renderMetrics() {
  var m = S.metrics, b = S.baseline;
  if (!m) return;
  var scr = m.scr, ssr = m.ssr;
  $('scrn').textContent = scr.n;
  if (!scr.n) {
    $('scrp50').textContent = '--'; $('scrp90').textContent = '--';
    $('scrnote').textContent =
      'No structure crosses the seam in this frame. That is the expected case '
      + 'mid-field: point the camera at something with lines through the seam, '
      + 'or walk a high-contrast edge across it, then re-snap.';
  } else {
    $('scrp50').textContent = fmt(scr.p50);
    $('scrp90').textContent = fmt(scr.p90);
    $('scrnote').innerHTML =
      'p50 ' + delta(scr.p50, b && b.scr ? b.scr.p50 : null, 'px')
      + ' &middot; p90 ' + delta(scr.p90, b && b.scr ? b.scr.p90 : null, 'px')
      + ' &middot; ' + scr.row_bands_covered + '/3 row bands, '
      + Math.round(scr.height_coverage * 100) + '% height'
      // Only offered when the frame was shown to respond to the curve at all.
      // `implied_dx` is a regression that returns confident nonsense on a
      // contaminated observation set -- it read -17, +159 and -7 px on three
      // real frames whose seams were all fine.
      + (S.quality && !S.quality.usable ? ''
        : scr.suggested_dx === null ? ''
          : ' &middot; measurement suggests dx &asymp; <b>' + fmt(scr.suggested_dx) + ' px</b>');
  }
  $('ssrv').textContent = fmt(ssr.abs_ln_ssr, 3);
  $('ssrnote').innerHTML =
    delta(ssr.abs_ln_ssr, b && b.ssr ? b.ssr.abs_ln_ssr : null, '')
    + ' &middot; noise floor ' + ssr.noise_floor.toFixed(2)
    + ' &mdash; a post-fusion shift cannot move this; only a camera-side '
    + 'correction applied before the blend can, and that shows up after Apply.';
  if (!S.quality) {
    $('suggest').disabled = (scr.suggested_dx === null || scr.suggested_dx === undefined);
  }
}

// ---------------------------------------------------------------------------
// Session
// ---------------------------------------------------------------------------
function applyState(st) {
  S.st = st;
  if (st.height) {
    H = st.height; STRIP = st.strip_w; BLENDH = st.blend_w;
    ZOOM_ROWS = Math.min(400, H);
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
  renderQuality(st.quality);
  if (st.baseline) { S.baseline = st.baseline; S.metrics = S.metrics || st.baseline; renderMetrics(); }
  S.ready = (ms === 'ready');
  if (ms === 'running') setTimeout(pollState, 1500);
  $('apply').disabled = !st.has_snapshot;
  $('save').disabled = !st.has_snapshot;
}
function pollState() {
  fetch('/stitch/state', { credentials: 'same-origin' })
    .then(function (r) { return r.json(); })
    .then(function (st) { applyState(st); if (st.metric_state === 'ready') measure(); });
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

function snap() {
  $('snap').disabled = true; $('snapstate').textContent = 'fetching 7680x2160 still...';
  fetch('/stitch/snap', { method: 'POST', credentials: 'same-origin' })
    .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
    .then(function (res) {
      $('snap').disabled = false;
      if (!res.ok) { $('snapstate').textContent = res.j.detail || 'snapshot failed'; return; }
      $('snapstate').textContent = 'live snapshot ' + new Date().toLocaleTimeString();
      loadFrame(res.j);
    }).catch(function (e) { $('snap').disabled = false; $('snapstate').textContent = String(e); });
}

function loadFrame(res) {
  var v = res.version, n = 0;
  ['left', 'right'].forEach(function (side) {
    var im = new Image();
    im.onload = function () { S.imgs[side] = im; if (++n === 2) draw(); };
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
      $('snapstate').textContent = 'frame from file';
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
    });
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
    });
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------
function surfaceChanged() {
  S.surface = $('owner').value === 'downstream' ? 'downstream'
    : ($('owner').value === 'camera_mesh' ? 'camera_mesh' : 'downstream');
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
  draw();
}

function onViewDrag(ev, canvas, view) {
  var rect = canvas.getBoundingClientRect();
  var y = (ev.clientY - rect.top) / rect.height * (view.rows) + view.top;
  S.cursorRow = Math.max(0, Math.min(H - 1, y));
  if (S.dragging === null) return;
  var dxPx = (ev.clientX - S.dragging.x0) / rect.width * (view.cols);
  var sgn = signFor(movingSide());
  var target = S.dragging.base + dxPx * (sgn === 0 ? 1 : sgn);
  // The nearest anchor follows the grab; the rest of the curve is untouched.
  var i = S.dragging.i, a = S.anchors.slice();
  a[i] = [a[i][0], target];
  setAnchors(a, 'anchor at y=' + a[i][0] + ' grabbed directly');
}
function nearestAnchor(row) {
  var best = 0, bd = 1e9;
  for (var i = 0; i < S.anchors.length; i++) {
    var d = Math.abs(S.anchors[i][0] - row);
    if (d < bd) { bd = d; best = i; }
  }
  return best;
}

function init() {
  S.anchors = [[0, 0], [540, 0], [1080, 0], [1620, 0], [2159, 0]];
  syncControls();
  $('snap').addEventListener('click', snap);
  $('browse').addEventListener('click', browse);
  $('open').addEventListener('click', openFrame);
  $('save').addEventListener('click', save);
  $('apply').addEventListener('click', function () { applyToCamera(false); });
  $('dryrun').addEventListener('click', function () { applyToCamera(true); });
  $('reset').addEventListener('click', function () {
    setAnchors(S.anchors.map(function (p) { return [p[0], 0]; }), 'curve reset to zero');
  });
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
  $('translate').addEventListener('input', function (e) { nudge(parseFloat(e.target.value) - meanDx()); });
  $('roll').addEventListener('input', function (e) { setRoll(parseFloat(e.target.value)); });
  var modes = document.querySelectorAll('.modes button');
  for (var i = 0; i < modes.length; i++) {
    modes[i].addEventListener('click', function (e) {
      S.mode = e.target.getAttribute('data-mode');
      for (var k = 0; k < modes.length; k++) modes[k].classList.remove('on');
      e.target.classList.add('on');
      draw();
    });
  }
  ['view', 'zoom'].forEach(function (id) {
    var c = $(id);
    var geom = function () {
      return id === 'view'
        ? { cols: 2 * STRIP, rows: H, top: 0, left: 0 }
        : {
          cols: 2 * BLENDH, rows: ZOOM_ROWS, left: STRIP - BLENDH,
          top: Math.max(0, Math.min(H - ZOOM_ROWS, S.cursorRow - ZOOM_ROWS / 2))
        };
    };
    c.addEventListener('mousedown', function (ev) {
      var g = geom(), rect = c.getBoundingClientRect();
      var row = (ev.clientY - rect.top) / rect.height * g.rows + g.top;
      S.cursorRow = row;
      // A grab only starts on the half that can actually move. The other one
      // is drawn dimmed and labelled locked, and letting it be dragged anyway
      // would make "locked" a decoration rather than a statement about the
      // hardware.
      var srcX = (ev.clientX - rect.left) / rect.width * g.cols + (g.left || 0);
      var onMoving = (srcX < STRIP) === (movingSide() === 'left');
      if (!onMoving) {
        $('curvewhy').textContent =
          'That half is locked. ' + (movingSide() === 'left'
            ? 'The camera can only warp the left half -- drag that one.'
            : 'The downstream corrector only rolls the right half -- drag that one.');
        draw(); return;
      }
      var i = nearestAnchor(row);
      S.dragging = { x0: ev.clientX, base: S.anchors[i][1], i: i };
      draw();
    });
    c.addEventListener('mousemove', function (ev) { onViewDrag(ev, c, geom()); });
    window.addEventListener('mouseup', function () { S.dragging = null; });
  });
  document.addEventListener('keydown', function (ev) {
    var step = ev.shiftKey ? 1 : 0.25;
    if (ev.key === 'ArrowLeft') { nudge(-step); ev.preventDefault(); }
    if (ev.key === 'ArrowRight') { nudge(step); ev.preventDefault(); }
  });
  setInterval(function () { if (S.mode === 'blink') { S.blink ^= 1; draw(); } }, 250);
  window.addEventListener('resize', draw);
  surfaceChanged();
  pollState();
}
document.addEventListener('DOMContentLoaded', init);
"""


_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Soccer-Cam &middot; Seam Calibration</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@500;600;700&family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>__STYLE__</style>
</head>
<body>
<div class="topbar"><div class="topbar-inner">
<div class="brand">SOCCER<span class="dot">&middot;</span>CAM</div>
<div class="crumb"><a href="/">Dashboard</a> / <a href="/config">Config</a> / Seam calibration</div>
</div></div>

<div class="shell">
<h1 class="headline">Stitch seam calibration</h1>
<p class="lede">Pull a still off the camera, slide the halves into registration by eye, and
send the geometry back. The curve you author below <em>is</em> the calibration artifact &mdash;
there is no separate export, and nothing you can move here exists outside it.</p>

__BANNER__

<div class="panel">
  <div class="btn-row">
    <button class="btn" id="snap">Fetch live snapshot</button>
    <span class="mono muted" id="snapstate">no frame loaded</span>
    <span style="flex:1"></span>
    <span class="mono faint">source <b id="camname">--</b> &middot; <b id="camhost">--</b></span>
  </div>
  <p class="faint">Two doors into the same editor. A live <span class="mono">Snap</span> is
    right when the camera is already looking at the field; most calibrations will come from a
    recorded game instead, because nobody stands at the touchline with a laptop mid-match and a
    camera on a bench indoors is looking at a scene 0.3&nbsp;m away &mdash; where parallax runs to
    tens of pixels and drowns the lens roll this tool corrects.</p>
  <div class="btn-row" style="margin-top:8px">
    <input type="text" id="framedir" placeholder="folder of frames or recordings"
      style="flex:2;min-width:240px">
    <button class="btn btn-ghost" id="browse">List</button>
    <select id="framelist" style="flex:2;min-width:200px"></select>
    <input type="number" id="atsec" placeholder="sec" step="1" style="width:80px" title="seconds into a video file">
    <button class="btn" id="open">Open frame</button>
  </div>
  <div class="btn-row" style="margin-top:8px">
    <input type="text" id="deployment" placeholder="deployment / tripod placement label"
      style="flex:1;min-width:240px">
    <span class="mono faint" id="openstate"></span>
  </div>
  <p class="faint">A calibration belongs to a camera <em>and where it was standing</em>. Whether
    the seam offset actually differs between tripod placements is still being measured, so every
    save is also filed under this label in
    <span class="mono">stitch_calibrations/</span> &mdash; if it turns out to be per-deployment,
    the evidence is already there instead of having been overwritten.</p>
</div>

<div class="grid">
<div>
  <div class="panel">
    <h2 class="sec">Seam <span class="accent">view</span></h2>
    <div class="modes">
      <button data-mode="blink" class="on">Blink 2Hz</button>
      <button data-mode="anaglyph">Anaglyph</button>
      <button data-mode="mirror">Mirror</button>
      <button data-mode="split">Plain</button>
    </div>
    <div id="viewwrap"><canvas id="view"></canvas></div>
    <p class="faint mono">Full height, horizontal <b id="viewscale">1.00</b>&times; and vertical
      0.31&times; &mdash; deliberately anisotropic: what you are judging is horizontal, and
      squashing the rows keeps all 2160 of them on screen without shrinking a 4&nbsp;px error
      to nothing. Drag anywhere to grab the nearest anchor; arrow keys nudge (shift = 1&nbsp;px).</p>
    <div class="caption" id="caption"></div>
    <div class="caveat"><strong>What these views can and cannot show.</strong>
      The camera fuses the two sensors before it hands out a JPEG, blending them over the
      256&nbsp;px window shaded above. Every pixel in there is already a mixture of both, so
      there is no second layer to reveal and no honest way to paint one. Blink and anaglyph
      therefore draw the <em>fitted</em> structures instead: each one measured independently on
      the left shoulder (red) and the right shoulder (cyan) and extrapolated to the seam, solid
      where there is real image behind it and dashed where it is extrapolation. A gap between a
      red line and its cyan partner at the seam is that structure's residual. The shoulders
      either side of the window are real; the window is inference.</div>

    <div id="zoomwrap"><canvas id="zoom"></canvas></div>
    <p class="faint mono">Blend window at 4&times;, true aspect, 1&nbsp;px grid &mdash;
      <span id="zoomlabel"></span>. Follows the green row cursor.</p>
  </div>
</div>

<div>
  <div class="panel">
    <h2 class="sec">Correction <span class="accent">surface</span></h2>
    <select id="owner">
      <option value="camera_mesh">Camera warp mesh &mdash; sub-pixel, before the blend</option>
      <option value="camera_scalars+downstream">Camera scalars + downstream residual</option>
      <option value="downstream">Downstream corrector only &mdash; whole-pixel, after the blend</option>
    </select>
    <p class="faint">Exactly one surface owns the correction. Splitting it across two is how a
      reboot silently turns a full correction into half of one: the camera's mesh is runtime
      state and does not survive a power cycle, while the downstream profile always does.</p>
  </div>

  <div class="panel">
    <h2 class="sec">The <span class="accent">curve</span></h2>
    <label class="row"><span class="lbl">Translate <span class="val" id="translateV"></span></span>
      <input type="range" id="translate" min="-40" max="40" step="0.25" value="0"></label>
    <label class="row"><span class="lbl">Roll &mdash; linear in y <span class="val" id="rollV"></span></span>
      <input type="range" id="roll" min="-40" max="40" step="0.25" value="0"></label>
    <p class="faint">Roll is the shape the physics predicts: two lenses rotated relative to each
      other about their optical axes displace the seam by <span class="mono">dx = -&theta;&middot;y</span>,
      linear in the row. Translate and roll are a <em>view</em> of the curve, recomputed from it
      after every edit &mdash; not stored parameters.</p>
    <table><thead><tr><th>row</th><th>dx (px, right half moves right)</th></tr></thead>
      <tbody id="curverows"></tbody></table>
    <p class="mono faint" style="word-break:break-all" id="curvejson"></p>
    <p class="faint" id="curvewhy"></p>
    <div class="btn-row">
      <button class="btn btn-ghost" id="reset">Reset to zero</button>
      <button class="btn btn-ghost" id="suggest" disabled>Use measured dx</button>
    </div>
  </div>

  <div class="panel">
    <h2 class="sec">Live <span class="accent">score</span></h2>
    <p class="mono faint"><span class="dot off" id="metricdot"></span><span id="metriclabel"></span>
      <span id="metricstate"></span></p>
    <div id="quality" style="display:none"></div>
    <div class="kpi">
      <div><div class="k">SCR p50</div><div class="v" id="scrp50">--</div></div>
      <div><div class="k">SCR p90</div><div class="v" id="scrp90">--</div></div>
      <div><div class="k">|ln SSR|</div><div class="v" id="ssrv">--</div></div>
      <div><div class="k">structures</div><div class="v" id="scrn">0</div></div>
    </div>
    <p class="faint" id="scrnote"></p>
    <p class="faint" id="ssrnote"></p>
    <p class="faint">These are the same numbers the automatic solver minimises, computed by the
      same code &mdash; so a calibration done by hand and one done by search are directly
      comparable. Acceptance wants p90 below 2&nbsp;px and at least halved.</p>
  </div>

  <div class="panel">
    <h2 class="sec">What the camera <span class="accent">already decided</span></h2>
    <table><thead><tr><th>scalar</th><th class="num">live</th><th class="num">factory</th></tr></thead>
      <tbody id="scalars"></tbody></table>
    <p class="faint" id="scalarnote"></p>
  </div>

  <div class="panel">
    <h2 class="sec">Ship <span class="accent">it</span></h2>
    <label class="row"><span class="lbl">Calibrated for distance (m)</span>
      <input type="number" id="distance_m" step="1" placeholder="45"></label>
    <label class="row"><span class="lbl">Basis</span>
      <input type="text" id="basis" placeholder="far touchline midpoint"></label>
    <p class="faint">One calibration is correct at one depth. Calibrating for the far field costs
      a couple of pixels across the far half of the pitch and tens of pixels at the near
      touchline &mdash; the right trade here, but a stated one.</p>
    <div class="btn-row">
      <button class="btn" id="save">Save profile</button>
      <span class="mono" id="savestate"></span>
    </div>
    <div id="applywrap" style="margin-top:14px">
      <div class="btn-row">
        <button class="btn btn-ghost" id="dryrun">Dry run</button>
        <button class="btn btn-danger" id="apply">Apply to camera</button>
        <span class="mono" id="applystate"></span>
      </div>
      <p class="faint">Apply sets the vendor scalars first and only then writes a mesh composed
        onto the baseline they produce &mdash; <span class="mono">SetStitch</span> re-runs the
        camera's own mesh optimiser and destroys anything written before it. The baseline is
        re-checked immediately before the write and the write is refused if it moved. Afterwards
        a fresh snapshot is pulled and re-scored, so you see numbers rather than a promise.</p>
      <pre class="mono faint" id="applyreport" style="white-space:pre-wrap;max-height:220px;overflow:auto"></pre>
    </div>
  </div>
</div>
</div>
</div>
<script>__SCRIPT__</script>
</body>
</html>
""".replace("__STYLE__", _STYLE).replace("__SCRIPT__", _SCRIPT)
