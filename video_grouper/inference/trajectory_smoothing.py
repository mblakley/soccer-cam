"""Trajectory smoothing — AutoCam's ``smooth_with_memory`` equivalent.

The Kalman tracker emits a per-frame state list with ``None`` gaps wherever
the detector missed (or the tracker rejected) a frame. Feeding that
directly to the renderer produces visible drift during the gaps because
the renderer's missing-ball gap-handling kicks in (camera widens, drifts
to center) — even when AutoCam, given the same noisy raw detections, is
visibly tracking smoothly.

AutoCam's trick (per the reverse-engineered ``smooth_with_memory`` function
plus the per-frame ``xy`` ground-truth in its jsonl) is a 3-second buffer
that weighted-averages all recent real detections, producing a
fully-populated trajectory with no gaps. Missing-frame "ball position" is
just the smoothed value of the past few seconds of detections.

This module replicates that behaviour as a pure function. No I/O, no av/cv2.
"""

from __future__ import annotations

import math


def smooth_with_memory(
    raw_states: list[dict | None],
    buffer_frames: int = 60,
    decay_per_frame: float = 0.985,
) -> list[dict | None]:
    """Exponentially-weighted buffered smoothing of a per-frame state list.

    Args:
        raw_states: list of ``{"x", "y"}`` (and optionally ``vx``, ``vy``,
            ``conf``) dicts, or ``None`` for frames with no real detection.
        buffer_frames: max age (in source frames) of detections kept in
            the smoothing buffer. Default 60 ≈ 3 seconds at 20 fps.
        decay_per_frame: weight multiplier per frame of age. Default 0.985
            gives weight ≈ 0.40 at age=60 (gentle decay across 3 s).

    Returns:
        A list the same length as ``raw_states``. Frames before the first
        real detection remain ``None``. Frames at or after the first real
        detection always get a populated dict, even if no detection is
        present at that frame — the buffer fills the gap. Velocity ``vx,vy``
        is computed by finite differencing the smoothed positions.
    """
    n = len(raw_states)
    smoothed: list[dict | None] = [None] * n

    # Collect (frame_idx, x, y) for every real detection
    real: list[tuple[int, float, float]] = []
    for i, s in enumerate(raw_states):
        if s is None:
            continue
        real.append((i, float(s["x"]), float(s["y"])))

    if not real:
        return smoothed

    first_real = real[0][0]

    # Walk frames; maintain a sliding window of in-buffer detections.
    win_start = 0
    win_end = 0  # index of the last detection with frame_idx <= current frame
    for f in range(first_real, n):
        # Drop detections older than buffer_frames
        while win_start < len(real) and real[win_start][0] < f - buffer_frames + 1:
            win_start += 1
        # Advance win_end while next detection's frame_idx <= f
        while win_end < len(real) and real[win_end][0] <= f:
            win_end += 1

        if win_end == win_start:
            # No detections in the buffer for this frame yet
            continue

        sum_w = 0.0
        sum_wx = 0.0
        sum_wy = 0.0
        for j in range(win_start, win_end):
            f_j, x_j, y_j = real[j]
            age = f - f_j
            w = math.pow(decay_per_frame, age)
            sum_w += w
            sum_wx += w * x_j
            sum_wy += w * y_j
        if sum_w > 0:
            smoothed[f] = {
                "x": sum_wx / sum_w,
                "y": sum_wy / sum_w,
                "vx": 0.0,
                "vy": 0.0,
            }

    # Velocity = finite difference of smoothed positions over 1 frame
    for f in range(1, n):
        cur = smoothed[f]
        prev = smoothed[f - 1]
        if cur is None or prev is None:
            continue
        cur["vx"] = cur["x"] - prev["x"]
        cur["vy"] = cur["y"] - prev["y"]

    return smoothed
