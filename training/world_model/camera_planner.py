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
from dataclasses import dataclass
from pathlib import Path

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


def plan_camera(
    trajectory: list[tuple[float, float] | None],
    *,
    src_w: int,
    src_h: int,
    depth01: list[float | None] | None = None,
    config: PlannerConfig | None = None,
) -> list[tuple[float, float, float]]:
    """Per-frame camera commands ``[(cx, cy, hfov_deg), ...]`` for ``trajectory``
    (source-px ball positions per SOURCE frame; ``None`` = no information).

    ``depth01`` optionally gives the ball's field depth per frame (0 = far
    touchline, 1 = near) for the calibrated depth-zoom term; ``None`` entries
    (or the whole argument) fall back to mid-depth.
    """
    cfg = config or PlannerConfig()
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
            if depth01 is not None and depth01[t] is not None:
                d = float(depth01[t])
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
            # no information at all: hold bearing, ease to the widest view
            target_hfov = cfg.missing_hfov_deg
        hfov += cfg.zoom_smoothing * (target_hfov - hfov)
        out.append((float(cx), float(cy), float(hfov)))
    return out


def upsample_track(
    track: dict[int, tuple[float, float]],
    ef: list[int],
    g_start: int,
    g_end: int,
    *,
    max_gap: int = 24,
) -> list[tuple[float, float] | None]:
    """Expand a stride-N track (keyed by ef INDEX) to per-source-frame positions on
    ``[g_start, g_end)`` by linear interpolation between consecutive tracked
    entries. Frames outside the tracked span — or inside a grid DISCONTINUITY
    wider than ``max_gap`` source frames (active-play range boundaries: halftime,
    warmup gaps) — are ``None``: interpolating across minutes of dead time would
    hand the planner a fake linear pan bridging the break."""
    pts = sorted((ef[i], xy) for i, xy in track.items() if 0 <= i < len(ef))
    out: list[tuple[float, float] | None] = [None] * (g_end - g_start)
    if not pts:
        return out
    gs = np.asarray([g for g, _ in pts], int)
    xs = np.asarray([xy[0] for _, xy in pts], float)
    ys = np.asarray([xy[1] for _, xy in pts], float)
    lo, hi = int(gs[0]), int(gs[-1])
    for g in range(max(g_start, lo), min(g_end, hi + 1)):
        x = float(np.interp(g, gs, xs))
        y = float(np.interp(g, gs, ys))
        out[g - g_start] = (x, y)
    # blank the interiors of wide grid gaps (exclusive: endpoints stay tracked)
    for k in range(1, len(gs)):
        if int(gs[k]) - int(gs[k - 1]) > max_gap:
            for g in range(int(gs[k - 1]) + 1, int(gs[k])):
                if g_start <= g < g_end:
                    out[g - g_start] = None
    return out


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
