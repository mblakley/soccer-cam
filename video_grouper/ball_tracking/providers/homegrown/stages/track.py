"""Tracking stage — link per-frame detections into a smoothed trajectory.

Wraps :mod:`video_grouper.inference.ball_tracker`. Reads
``detections.json``, runs the Kalman filter tracker, picks the longest
valid track, and writes a per-frame ``trajectory.json`` (one ``[x, y]``
row per source frame; ``None`` when no estimate is available).
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from video_grouper.ball_tracking.base import ProviderContext
from video_grouper.inference.ball_tracker import BallTracker, Detection

from . import register_stage
from .base import ProcessingStage

logger = logging.getLogger(__name__)


def _run_tracking(
    detections_path: str,
    output_json_path: str,
    gate_distance: float,
    max_missing: int,
) -> int:
    """Sync helper: load detections, run tracker, write trajectory JSON."""
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
    trajectory: list[list[float] | None] = [None] * (last_frame + 1)
    if best is not None and getattr(best, "detections", None):
        for det in best.detections:
            trajectory[det.frame_idx] = [det.x, det.y]
        for frame_idx, x, y in getattr(best, "predictions", []):
            if 0 <= frame_idx < len(trajectory) and trajectory[frame_idx] is None:
                trajectory[frame_idx] = [float(x), float(y)]

    with open(output_json_path, "w", encoding="utf-8") as f:
        json.dump(trajectory, f)

    populated = sum(1 for p in trajectory if p is not None)
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
        )
        logger.info(
            "track: wrote trajectory with %d populated frames to %s",
            populated,
            trajectory_path,
        )
        return {"trajectory_path": str(trajectory_path)}


register_stage(TrackStage.name, TrackStage)
