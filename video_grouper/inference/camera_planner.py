"""Camera PLANNER: turn the tracked ball path into an explicit per-frame camera path.

Architecture decision (Mark, 2026-07-09): the renderer is DUMB — it executes
``{frame, center_px, hfov_deg}`` commands and enforces only hard projection
feasibility (source-edge clamps). Everything intelligent about the camera lives
HERE, upstream, where the ball-state machine's information is: a second control
system inside the renderer would double-filter the track and reopen the
validation gap between what we score and what viewers see.

Behavioral spec: AutoCam's rendering FEEL (we improved the input, not the
cinematography). The aesthetic tunables are ported from the calibrated
``feat/broadcast-camera-render`` camera modes (zoom curve vision-matched to
AutoCam's framing on the Reolink Duo 3; smoothing/lead-room constants tuned on
real footage there). What is deliberately NOT ported is that branch's defensive
input-cleaning — detection teleport gates (700 px), reacquisition timers,
recency-window averaging — which exists to survive raw noisy detections. Our
input is the Viterbi + Kalman + ball-state track: teleports are already gone,
misses are already coasted with INFORMED positions (ballistic landings,
out-of-bounds restart pins), so the planner follows its input honestly.

The camera path is a first-class artifact: score it with the same viewport
benchmark as the track BEFORE any pixel is rendered.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import overload

import numpy as np


@dataclass(frozen=True)
class PlannerConfig:
    """Aesthetic tunables (defaults = the AutoCam-calibrated render-branch values)."""

    fps: float = 20.0
    # pan/tilt follow: EMA whose gain rises with normalized error (steady when on
    # target, responsive when the play breaks away) — render-branch smoothing range
    pan_smoothing_min: float = 0.04
    pan_smoothing_max: float = 0.12
    pitch_smoothing: float = 0.05  # vertical follow — slower than pan
    # velocity lead room: aim ahead of a moving ball, capped as a fraction of the
    # current view width
    velocity_ema: float = 0.3
    lead_frames: float = 8.0  # aim this many frames ahead along the velocity
    max_lead_room_fraction: float = 0.20
    # calibrated zoom curve (degrees of view HFOV): far/slow -> tight, near OR
    # fast -> wide; 0.90 scale vision-matched to AutoCam's framing (cmp3 study)
    zoom_base_deg: float = 47.0
    zoom_min_deg: float = 46.0
    zoom_max_deg: float = 58.0
    # angular units so the feel is camera-independent (15 px/f on the 7680-wide
    # Reolink calibration = 0.35 deg/f; a 4096-wide Dahua frame maps px differently)
    zoom_speed_norm_degf: float = 0.35
    zoom_speed_gain_deg: float = 8.0
    zoom_depth_gain_deg: float = 5.0
    zoom_scale: float = 0.90
    zoom_smoothing: float = 0.03  # incremental zoom (AutoCam's slow ease)
    # dead ball: sustained slow ball -> ease wider (restarts, keeper holds)
    deadball_speed_degf: float = 0.094  # 4 px/f at the Reolink calibration
    deadball_frames: int = 15
    deadball_hfov_deg: float = 52.0
    # no input at all (outside the track span): hold bearing, ease to widest
    missing_hfov_deg: float = 58.0
    # ---- W2 stoppage-HOLD FSM (training/docs/W2_STOPPAGE_HOLD_DESIGN.md) ----
    # Everything below is gated on enable_hold: False (the default) runs the
    # pre-W2 planner UNCHANGED — bit-identical output, states/disp inputs
    # ignored. Defaults are the design doc's pre-registered values (section 4).
    enable_hold: bool = False
    hold_entry_frames: int = 20  # sustained vote frames to enter HOLD (1 s @ 20 fps)
    hold_exit_frames: int = 8  # consecutive tracked 'T' frames to leave HOLD
    hold_speed_thresh: float = 0.094  # slow-ball voter, deg/f (= deadball_speed_degf)
    hold_dispersion_px: float = 180.0  # candidate-cloud spread < this votes HOLD
    reacq_ramp_frames: int = 12  # pan-gain ramp length leaving HOLD
    hold_anchor_lookback: int = (
        20  # anchor = campath position this many frames before the entry run began
    )


# tracker states that vote HOLD on their own: the track is coasting a miss ('C')
# or outside any span ('M') — never pan on a coasted estimate (design doc §3.1)
_HOLD_VOTE_STATES = ("C", "M")


def _plan_camera_hold(
    trajectory: list[tuple[float, float] | None],
    *,
    src_w: int,
    src_h: int,
    depth01: list[float | None] | None,
    states: list[str],
    disp: list[float | None] | None,
    cfg: PlannerConfig,
) -> list[tuple[float, float, float]]:
    """LIVE / HOLD / REACQUIRE planner FSM (W2 stoppage-hold design, section 3).

    The LIVE math is a faithful copy of the default :func:`plan_camera` loop
    (same operations in the same order, so a run that never leaves LIVE is
    bit-identical to the default planner). On top of it:

    - per-frame hold VOTE = tracker state in ``{C, M}`` OR slow ball
      (|EMA velocity| < ``hold_speed_thresh`` deg/f) OR low candidate
      dispersion (< ``hold_dispersion_px``, the scramble/pile-up signature)
      when a dispersion channel is present;
    - LIVE -> HOLD after ``hold_entry_frames`` consecutive votes; the pan
      freezes at the OUTPUT campath position ``hold_anchor_lookback`` frames
      BEFORE the entry run began (the last-GOOD position — the EXP-OP-04
      lesson: never freeze where the campath happens to be when the vote
      fires), zoom eases wide on the existing dead-ball curve, the velocity
      EMA resets;
    - HOLD -> REACQUIRE after ``hold_exit_frames`` consecutive ``'T'`` frames
      with finite positions; the pan gain ramps linearly 0 -> normal over
      ``reacq_ramp_frames``; a hold vote mid-ramp returns to HOLD at the SAME
      anchor.
    """
    n = len(trajectory)
    out: list[tuple[float, float, float]] = []
    if n == 0:
        return out
    if len(states) != n:
        raise ValueError(f"plan_camera: {len(states)} states for {n} trajectory frames")
    if disp is not None and len(disp) != n:
        raise ValueError(
            f"plan_camera: {len(disp)} disp values for {n} trajectory frames"
        )

    # seed on the first known position (or frame centre) — same as LIVE-only
    first = next((p for p in trajectory if p is not None), None)
    cx, cy = (
        (float(first[0]), float(first[1]))
        if first is not None
        else (src_w / 2.0, src_h / 2.0)
    )
    hfov = cfg.zoom_base_deg * cfg.zoom_scale
    vx = vy = 0.0
    prev: tuple[float, float] | None = None
    slow_run = 0

    mode = "LIVE"
    vote_run = exit_run = ramp_i = 0
    anchor = (cx, cy)

    for t in range(n):
        p = trajectory[t]
        # the frame's usable position, or None (a NaN point is unusable: it must
        # never become a pan target or an exit-condition frame)
        pt: tuple[float, float] | None = None
        if p is not None:
            x0, y0 = float(p[0]), float(p[1])
            if math.isfinite(x0) and math.isfinite(y0):
                pt = (x0, y0)
        # ---- input / velocity-EMA update (identical to LIVE-only when LIVE) ----
        if pt is not None:
            x, y = pt
            if mode == "HOLD":
                vx = vy = 0.0  # velocity EMA stays reset while holding
            elif prev is not None:
                vx = cfg.velocity_ema * (x - prev[0]) + (1 - cfg.velocity_ema) * vx
                vy = cfg.velocity_ema * (y - prev[1]) + (1 - cfg.velocity_ema) * vy
            prev = (x, y)
            speed = float(np.hypot(vx, vy))
            speed_degf = speed / (src_w / 180.0)
            slow_run = slow_run + 1 if speed_degf < cfg.deadball_speed_degf else 0
        else:
            prev = None
            vx = vy = 0.0
            speed_degf = 0.0

        # ---- hold vote: OR of the enumerated voters (design doc section 3) ----
        vote = states[t] in _HOLD_VOTE_STATES or speed_degf < cfg.hold_speed_thresh
        if not vote and disp is not None:
            dv = disp[t]
            # low dispersion = the candidate cloud agrees — the scramble/pile-up
            # HOLD signature per the design doc (below-threshold votes HOLD)
            vote = dv is not None and float(dv) < cfg.hold_dispersion_px

        # ---- FSM transitions ----
        if mode == "LIVE":
            vote_run = vote_run + 1 if vote else 0
            if vote_run >= cfg.hold_entry_frames:
                # anchor at the last-GOOD campath position: hold_anchor_lookback
                # frames before the entry run began (clamped to the path start)
                run_start = t - cfg.hold_entry_frames + 1
                if out:
                    ai = min(max(run_start - cfg.hold_anchor_lookback, 0), len(out) - 1)
                    anchor = (out[ai][0], out[ai][1])
                else:
                    anchor = (cx, cy)
                mode = "HOLD"
                vote_run = exit_run = 0
                vx = vy = 0.0
        elif mode == "REACQUIRE":
            if vote:  # exit condition collapsed mid-ramp: back to the SAME anchor
                mode = "HOLD"
                vote_run = exit_run = 0
                vx = vy = 0.0
        if mode == "HOLD":
            exit_run = exit_run + 1 if (states[t] == "T" and pt is not None) else 0
            if exit_run >= cfg.hold_exit_frames:
                mode = "REACQUIRE"
                ramp_i = 0
                vote_run = exit_run = 0

        # ---- emission ----
        if mode == "HOLD":
            cx, cy = anchor  # pan FROZEN at the anchor
            if pt is not None:
                # zoom continues the existing dead-ball ease-wide behavior
                d = 0.5
                if depth01 is not None:
                    dval = depth01[t]
                    if dval is not None:
                        d = float(dval)
                target_hfov = cfg.zoom_base_deg + (
                    min(speed_degf / cfg.zoom_speed_norm_degf, 1.0)
                    * cfg.zoom_speed_gain_deg
                )
                target_hfov += d * cfg.zoom_depth_gain_deg
                target_hfov = float(
                    np.clip(target_hfov, cfg.zoom_min_deg, cfg.zoom_max_deg)
                )
                target_hfov *= cfg.zoom_scale
                target_hfov = max(target_hfov, cfg.deadball_hfov_deg)
            else:
                target_hfov = cfg.missing_hfov_deg
        elif pt is not None:
            # LIVE / REACQUIRE: the default planner math; the only REACQUIRE
            # difference is the pan gain ramping linearly 0 -> 1.
            d = 0.5
            if depth01 is not None:
                dval = depth01[t]
                if dval is not None:
                    d = float(dval)
            target_hfov = cfg.zoom_base_deg + (
                min(speed_degf / cfg.zoom_speed_norm_degf, 1.0)
                * cfg.zoom_speed_gain_deg
            )
            target_hfov += d * cfg.zoom_depth_gain_deg
            target_hfov = float(
                np.clip(target_hfov, cfg.zoom_min_deg, cfg.zoom_max_deg)
            )
            target_hfov *= cfg.zoom_scale
            if slow_run >= cfg.deadball_frames:
                target_hfov = max(target_hfov, cfg.deadball_hfov_deg)

            view_w_px = src_w * (hfov / 180.0)  # approx px width of the view
            lead_cap = cfg.max_lead_room_fraction * view_w_px
            tx = x + float(np.clip(vx * cfg.lead_frames, -lead_cap, lead_cap))
            ty = y + float(np.clip(vy * cfg.lead_frames, -lead_cap, lead_cap))
            err = float(np.hypot(tx - cx, ty - cy))
            resp = min(1.0, err / max(view_w_px / 2.0, 1.0))
            a_pan = cfg.pan_smoothing_min + resp * (
                cfg.pan_smoothing_max - cfg.pan_smoothing_min
            )
            gain = 1.0
            if mode == "REACQUIRE":
                ramp_i += 1
                gain = min(ramp_i / max(cfg.reacq_ramp_frames, 1), 1.0)
                if ramp_i >= cfg.reacq_ramp_frames:
                    mode = "LIVE"  # ramp complete: full-gain follow resumes
            cx += gain * a_pan * (tx - cx)
            cy += gain * cfg.pitch_smoothing * (ty - cy)
        else:
            # no information at all (and not holding): legacy behavior —
            # hold bearing, ease to the widest view
            target_hfov = cfg.missing_hfov_deg
        hfov += cfg.zoom_smoothing * (target_hfov - hfov)
        out.append((float(cx), float(cy), float(hfov)))
    return out


def plan_camera(
    trajectory: list[tuple[float, float] | None],
    *,
    src_w: int,
    src_h: int,
    depth01: list[float | None] | None = None,
    states: list[str] | None = None,
    disp: list[float | None] | None = None,
    config: PlannerConfig | None = None,
) -> list[tuple[float, float, float]]:
    """Per-frame camera commands ``[(cx, cy, hfov_deg), ...]`` for ``trajectory``
    (source-px ball positions per SOURCE frame; ``None`` = no information).

    ``depth01`` optionally gives the ball's field depth per frame (0 = far
    touchline, 1 = near) for the calibrated depth-zoom term; ``None`` entries
    (or the whole argument) fall back to mid-depth.

    ``states`` / ``disp`` are the trajectory/2 per-frame channels (see
    :func:`parse_trajectory_artifact`): tracker state ``'T'``/``'C'``/``'M'``
    and optional candidate-cloud dispersion (px). They drive the stoppage-HOLD
    FSM ONLY when ``config.enable_hold`` is set AND ``states`` is given;
    otherwise the default single-pass planner below runs untouched, so
    ``enable_hold=False`` output is bit-identical to the pre-W2 planner
    regardless of which channels are passed.
    """
    cfg = config or PlannerConfig()
    if cfg.enable_hold and states is not None:
        return _plan_camera_hold(
            trajectory,
            src_w=src_w,
            src_h=src_h,
            depth01=depth01,
            states=states,
            disp=disp,
            cfg=cfg,
        )
    n = len(trajectory)
    out: list[tuple[float, float, float]] = []
    if n == 0:
        return out

    # seed on the first known position (or frame centre)
    first = next((p for p in trajectory if p is not None), None)
    cx, cy = (
        (float(first[0]), float(first[1]))
        if first is not None
        else (src_w / 2.0, src_h / 2.0)
    )
    hfov = cfg.zoom_base_deg * cfg.zoom_scale
    vx = vy = 0.0
    prev: tuple[float, float] | None = None
    slow_run = 0

    for t in range(n):
        p = trajectory[t]
        if p is not None:
            x, y = float(p[0]), float(p[1])
            if prev is not None:
                vx = cfg.velocity_ema * (x - prev[0]) + (1 - cfg.velocity_ema) * vx
                vy = cfg.velocity_ema * (y - prev[1]) + (1 - cfg.velocity_ema) * vy
            prev = (x, y)
            speed = float(np.hypot(vx, vy))
            slow = speed / (src_w / 180.0) < cfg.deadball_speed_degf
            slow_run = slow_run + 1 if slow else 0

            # ---- zoom target: calibrated curve, then dead-ball override ----
            d = 0.5
            if depth01 is not None:
                dv = depth01[t]
                if dv is not None:
                    d = float(dv)
            speed_degf = speed / (src_w / 180.0)
            target_hfov = cfg.zoom_base_deg + (
                min(speed_degf / cfg.zoom_speed_norm_degf, 1.0)
                * cfg.zoom_speed_gain_deg
            )
            target_hfov += d * cfg.zoom_depth_gain_deg
            target_hfov = float(
                np.clip(target_hfov, cfg.zoom_min_deg, cfg.zoom_max_deg)
            )
            target_hfov *= cfg.zoom_scale
            if slow_run >= cfg.deadball_frames:
                target_hfov = max(target_hfov, cfg.deadball_hfov_deg)

            # ---- pan target: ball + capped velocity lead room ----
            view_w_px = src_w * (hfov / 180.0)  # approx px width of the view
            lead_cap = cfg.max_lead_room_fraction * view_w_px
            tx = x + float(np.clip(vx * cfg.lead_frames, -lead_cap, lead_cap))
            ty = y + float(np.clip(vy * cfg.lead_frames, -lead_cap, lead_cap))

            # error-adaptive follow: steady on target, responsive when far behind
            err = float(np.hypot(tx - cx, ty - cy))
            resp = min(1.0, err / max(view_w_px / 2.0, 1.0))
            a_pan = cfg.pan_smoothing_min + resp * (
                cfg.pan_smoothing_max - cfg.pan_smoothing_min
            )
            cx += a_pan * (tx - cx)
            cy += cfg.pitch_smoothing * (ty - cy)
        else:
            # no information at all: hold bearing, ease to the widest view. Reset
            # the velocity/prev state: upsample_track emits None across a wide
            # blanked gap (e.g. halftime), and without this the first real frame
            # after the gap reads the whole-break displacement (x - prev[0]) as a
            # single frame's velocity and lurches the lead term to its cap.
            prev = None
            vx = vy = 0.0
            target_hfov = cfg.missing_hfov_deg
        hfov += cfg.zoom_smoothing * (target_hfov - hfov)
        out.append((float(cx), float(cy), float(hfov)))
    return out


@overload
def upsample_track(
    track: dict[int, tuple[float, float]],
    ef: list[int],
    g_start: int,
    g_end: int,
    *,
    max_gap: int = ...,
    states: None = ...,
    conf: dict[int, float] | None = ...,
) -> list[tuple[float, float] | None]: ...


@overload
def upsample_track(
    track: dict[int, tuple[float, float]],
    ef: list[int],
    g_start: int,
    g_end: int,
    *,
    max_gap: int = ...,
    states: dict[int, str],
    conf: dict[int, float] | None = ...,
) -> tuple[list[tuple[float, float] | None], list[str], list[float]]: ...


def upsample_track(
    track: dict[int, tuple[float, float]],
    ef: list[int],
    g_start: int,
    g_end: int,
    *,
    max_gap: int = 24,
    states: dict[int, str] | None = None,
    conf: dict[int, float] | None = None,
) -> (
    list[tuple[float, float] | None]
    | tuple[list[tuple[float, float] | None], list[str], list[float]]
):
    """Expand a stride-N track (keyed by ef INDEX) to per-source-frame positions on
    ``[g_start, g_end)`` by linear interpolation between consecutive tracked
    entries. Frames outside the tracked span — or inside a grid DISCONTINUITY
    wider than ``max_gap`` source frames (active-play range boundaries: halftime,
    warmup gaps) — are ``None``: interpolating across minutes of dead time would
    hand the planner a fake linear pan bridging the break.

    When ``states`` is given (per-grid-frame ``'T'``/``'C'`` keyed like
    ``track``), returns ``(points, state, conf)`` — the trajectory/2 channels
    aligned 1:1 with the dense trajectory. Interpolated frames inherit their
    source span's state: ``'T'`` between two ``'T'`` grid frames, ``'C'`` when
    the span touches a coasted grid frame; blanked wide-gap interiors and
    frames outside the tracked span are ``'M'``. Dense conf interpolates the
    grid confidences linearly and is 0.0 on ``'C'``/``'M'`` frames (grid conf
    defaults to 1.0 for ``'T'`` / 0.0 for ``'C'`` entries when ``conf`` is not
    given)."""
    pts = sorted((ef[i], xy) for i, xy in track.items() if 0 <= i < len(ef))
    out: list[tuple[float, float] | None] = [None] * (g_end - g_start)
    st: list[str] = ["M"] * (g_end - g_start)
    cf: list[float] = [0.0] * (g_end - g_start)
    if not pts:
        return out if states is None else (out, st, cf)
    gs = np.asarray([g for g, _ in pts], int)
    xs = np.asarray([xy[0] for _, xy in pts], float)
    ys = np.asarray([xy[1] for _, xy in pts], float)
    lo, hi = int(gs[0]), int(gs[-1])
    gst: list[str] = []
    gcf: list[float] = []
    if states is not None:
        # grid channels aligned with gs (ef index -> its grid frame)
        by_g = {ef[i]: i for i in track if 0 <= i < len(ef)}
        gst = [states.get(by_g[int(g)], "C") for g in gs]
        gcf = [
            float(conf.get(by_g[int(g)], 0.0))
            if conf is not None
            else (1.0 if s == "T" else 0.0)
            for g, s in zip(gs, gst, strict=True)
        ]
    for g in range(max(g_start, lo), min(g_end, hi + 1)):
        x = float(np.interp(g, gs, xs))
        y = float(np.interp(g, gs, ys))
        out[g - g_start] = (x, y)
        if states is not None:
            k = int(np.searchsorted(gs, g))
            if k < len(gs) and int(gs[k]) == g:  # exactly on a grid sample
                s, c = gst[k], gcf[k]
            else:  # interior of the span (gs[k-1], gs[k])
                s = "T" if (gst[k - 1] == "T" and gst[k] == "T") else "C"
                w = (g - int(gs[k - 1])) / (int(gs[k]) - int(gs[k - 1]))
                c = gcf[k - 1] + w * (gcf[k] - gcf[k - 1])
            st[g - g_start] = s
            cf[g - g_start] = float(c) if s == "T" else 0.0
    # blank the interiors of wide grid gaps (exclusive: endpoints stay tracked)
    for k in range(1, len(gs)):
        if int(gs[k]) - int(gs[k - 1]) > max_gap:
            for g in range(int(gs[k - 1]) + 1, int(gs[k])):
                if g_start <= g < g_end:
                    out[g - g_start] = None
                    st[g - g_start] = "M"
                    cf[g - g_start] = 0.0
    return out if states is None else (out, st, cf)


def upsample_disp(
    disp: list[float | None],
    ef: list[int],
    g_start: int,
    g_end: int,
    *,
    points: list[tuple[float, float] | None] | None = None,
) -> list[float | None]:
    """Densify the per-GRID-frame candidate-dispersion channel (aligned with
    ``ef``; ``None`` = no candidates that frame) to per-source-frame on
    ``[g_start, g_end)``: linear interpolation between dispersion-carrying grid
    frames, ``None`` outside their span and — when ``points`` (the dense
    trajectory) is given — wherever the trajectory itself has no point, so a
    blanked play discontinuity never carries an interpolated scramble signal."""
    out: list[float | None] = [None] * (g_end - g_start)
    pairs = [
        (int(ef[i]), float(v))
        for i, v in enumerate(disp)
        if i < len(ef) and v is not None
    ]
    if not pairs:
        return out
    gs = np.asarray([g for g, _ in pairs], int)
    vs = np.asarray([v for _, v in pairs], float)
    for g in range(max(g_start, int(gs[0])), min(g_end, int(gs[-1]) + 1)):
        if points is not None and points[g - g_start] is None:
            continue
        out[g - g_start] = float(np.interp(g, gs, vs))
    return out


def parse_trajectory_artifact(obj: list | dict) -> dict:
    """Normalize any trajectory artifact to the trajectory/2 channel set.

    Accepts, per the required-artifact / neutral-default convention:

    - ``trajectory/2`` dicts — ``points`` + ``state`` + ``conf`` (+ optional
      ``disp``), all validated to align 1:1;
    - ``trajectory/1`` dicts and the bare per-frame point list the pipeline's
      ball_select step wrote before trajectory/2 — the states become the
      neutral element (``'T'`` where a point exists, ``'M'`` at nulls), conf
      1.0/0.0, disp absent.

    Returns ``{"g_start", "fps", "points", "state", "conf", "disp"}``
    (``g_start`` 0 / ``fps`` None when the artifact carries no metadata).
    A dict with any other schema raises ``ValueError``.
    """
    state: list[str] | None = None
    conf: list[float] | None = None
    disp: list[float | None] | None = None
    if isinstance(obj, dict):
        schema = obj.get("schema")
        if schema not in ("trajectory/1", "trajectory/2"):
            raise ValueError(f"not a trajectory artifact (schema {schema!r})")
        raw_pts = obj["points"]
        g_start = int(obj.get("g_start", 0))
        fps = float(obj["fps"]) if obj.get("fps") is not None else None
        if schema == "trajectory/2":
            state = [str(s) for s in obj["state"]]
            conf = [float(c) for c in obj["conf"]]
            raw_disp = obj.get("disp")
            if raw_disp is not None:
                disp = [None if v is None else float(v) for v in raw_disp]
    else:
        raw_pts = obj
        g_start, fps = 0, None
    points: list[tuple[float, float] | None] = [
        None if p is None else (float(p[0]), float(p[1])) for p in raw_pts
    ]
    if state is None or conf is None:  # v1 / bare list: the neutral element
        state = ["T" if p is not None else "M" for p in points]
        conf = [1.0 if p is not None else 0.0 for p in points]
    elif len(state) != len(points) or len(conf) != len(points):
        raise ValueError("trajectory/2 state/conf channels misaligned with points")
    if disp is not None and len(disp) != len(points):
        raise ValueError("trajectory/2 disp channel misaligned with points")
    return {
        "g_start": g_start,
        "fps": fps,
        "points": points,
        "state": state,
        "conf": conf,
        "disp": disp,
    }


def save_camera_path(
    path: Path | str,
    plan: list[tuple[float, float, float]],
    *,
    g_start: int,
    src_w: int,
    src_h: int,
    fps: float,
) -> None:
    payload = {
        "schema": "camera_path/1",
        "g_start": int(g_start),
        "src_w": int(src_w),
        "src_h": int(src_h),
        "fps": float(fps),
        "frames": [[round(cx, 1), round(cy, 1), round(h, 2)] for cx, cy, h in plan],
    }
    Path(path).write_text(json.dumps(payload))
