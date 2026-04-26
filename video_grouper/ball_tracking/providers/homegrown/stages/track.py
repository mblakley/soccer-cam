"""Tracking stage — link per-frame detections into a smooth trajectory.

Two-stage process:

1. **Kalman tracker** (``BallTracker``) does *spatial association* and
   *false-positive rejection*. Multi-detection frames get linked into
   tracks; we pick the best (longest × highest-confidence) track. Only
   real measurements from that track survive — the Kalman extrapolated
   states are NOT used downstream because they drift wildly during long
   gaps (we observed extrapolation to 60,000 px when ``max_missing`` was
   set high enough to span typical detection misses).

2. **smooth_with_memory** (``trajectory_smoothing``) does *visual
   smoothness* and *gap filling*. Given the surviving real measurements
   from the best track, it produces a fully-populated per-frame
   trajectory using a 3-second exponentially-weighted buffer — the same
   approach AutoCam uses internally per the reverse-engineered
   ``smooth_with_memory`` function. Missing frames get the buffer's
   weighted-average value rather than ``None``.

Output ``trajectory.json``: one ``{"x", "y", "vx", "vy"}`` dict per
source frame from the first detection onward (``None`` only for the
initial pre-detection frames). Velocity is computed by finite differencing
the smoothed positions. The render stage also still accepts the legacy
``[x, y]`` list format for backwards compatibility.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from video_grouper.ball_tracking.base import ProviderContext
from video_grouper.inference.ball_tracker import BallTracker, Detection
from video_grouper.inference.trajectory_smoothing import smooth_with_memory

from . import register_stage
from .base import ProcessingStage

logger = logging.getLogger(__name__)


def _run_tracking(
    detections_path: str,
    output_json_path: str,
    gate_distance: float,
    max_missing: int,
    smooth_buffer_frames: int,
    smooth_decay_per_frame: float,
) -> int:
    """Sync helper: load detections, run tracker + smoother, write JSON."""
    with open(detections_path, "r", encoding="utf-8") as f:
        per_frame: list[dict] = json.load(f)

    tracker = BallTracker(gate_distance=gate_distance, max_missing=max_missing)

    # Group detections by frame_idx for the per-frame update loop.
    by_frame: dict[int, list[Detection]] = {}
    for d in per_frame:
        frame_idx = int(d["frame_idx"])
        by_frame.setdefault(frame_idx, []).append(
            Detection(
                x=float(d["cx"]),
                y=float(d["cy"]),
                confidence=float(d["conf"]),
                frame_idx=frame_idx,
            )
        )
    if not by_frame:
        logger.warning("track: no detections to track")
        with open(output_json_path, "w", encoding="utf-8") as f:
            json.dump([], f)
        return 0

    last_frame = max(by_frame)
    for frame_idx in range(last_frame + 1):
        tracker.update(frame_idx, by_frame.get(frame_idx, []))

    best = tracker.get_best_track() if hasattr(tracker, "get_best_track") else None

    # Build the raw per-frame state list using ONLY real measurements from the
    # best track. Kalman extrapolations (Track.states for unmeasured frames)
    # are intentionally excluded — they drift quadratically during gaps.
    raw: list[dict | None] = [None] * (last_frame + 1)
    if best is not None and getattr(best, "detections", None):
        for det in best.detections:
            if 0 <= det.frame_idx <= last_frame:
                raw[det.frame_idx] = {
                    "x": float(det.x),
                    "y": float(det.y),
                    "conf": float(det.confidence),
                }

    n_real = sum(1 for r in raw if r is not None)

    # Apply smooth_with_memory. Buffer size + decay tuned to AutoCam-like
    # behavior (~3 sec window with gentle decay).
    smoothed = smooth_with_memory(
        raw,
        buffer_frames=smooth_buffer_frames,
        decay_per_frame=smooth_decay_per_frame,
    )

    with open(output_json_path, "w", encoding="utf-8") as f:
        json.dump(smoothed, f)

    populated = sum(1 for p in smoothed if p is not None)
    logger.info(
        "track: %d real detections in best track → %d/%d frames after smoothing",
        n_real,
        populated,
        len(smoothed),
    )
    return populated


class TrackStage(ProcessingStage):
    name = "track"

    async def run(
        self, artifacts: dict[str, Any], ctx: ProviderContext
    ) -> dict[str, Any] | None:
        detections_path = artifacts.get("detections_path")
        if not detections_path:
            raise RuntimeError(
                "track: detections_path missing — was the detect stage skipped?"
            )

        in_path = Path(artifacts["input_path"])
        trajectory_path = in_path.with_name("trajectory.json")

        populated = await asyncio.to_thread(
            _run_tracking,
            detections_path,
            str(trajectory_path),
            self.provider_config.track_kalman_gate,
            self.provider_config.track_max_missing,
            self.provider_config.track_smooth_buffer_frames,
            self.provider_config.track_smooth_decay_per_frame,
        )
        logger.info(
            "track: wrote trajectory with %d populated frames to %s",
            populated,
            trajectory_path,
        )
        return {"trajectory_path": str(trajectory_path)}


register_stage(TrackStage.name, TrackStage)
