"""Tracking step — link per-frame detections into a smoothed trajectory.

Wraps :mod:`video_grouper.inference.ball_tracker`. Reads ``detections_path``
(the per-frame detections JSON), runs the Kalman tracker, picks the longest
valid track, and writes a per-frame ``trajectory.json`` (one ``[x, y]`` row per
source frame; ``null`` when no estimate is available).
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from pydantic import BaseModel

from video_grouper.inference.ball_tracker import BallTracker, Detection
from video_grouper.pipeline import register_step
from video_grouper.pipeline.base import PipelineStep, StepContext
from video_grouper.pipeline.manifest import PipelineManifest

logger = logging.getLogger(__name__)


class TrackStepConfig(BaseModel):
    track_kalman_gate: float = 200.0
    track_max_missing: int = 15
    # Trajectory stitching (see BallTracker.build_trajectory): the ball is gated into several short
    # tracks, so stitch them best-first instead of keeping only the longest. A track is dropped if it
    # is shorter than track_stationary_len AND spans < track_move_px (i.e. a sustained stationary FP
    # like a sprinkler/bystander is kept out; brief real tracks are kept). Gaps up to interp are filled.
    track_move_px: float = 80.0
    track_stationary_len: int = 20
    track_interp_gap: int = 16


def _run_tracking(
    detections_path: str,
    output_json_path: str,
    gate_distance: float,
    max_missing: int,
    move_px: float = 250.0,
    stationary_len: int = 20,
    interp_gap: int = 16,
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

    trajectory = tracker.build_trajectory(
        last_frame + 1,
        move_px=move_px,
        stationary_len=stationary_len,
        interp_gap=interp_gap,
    )

    with open(output_json_path, "w", encoding="utf-8") as f:
        json.dump(trajectory, f)

    populated = sum(1 for p in trajectory if p is not None)
    return populated


class TrackStep(PipelineStep):
    name = "track"
    config_model = TrackStepConfig
    consumes = ("detections_path",)
    produces = ("trajectory_path",)
    runtime = "service"
    requires = ()
    resources = ()

    async def run(self, manifest: PipelineManifest, ctx: StepContext) -> bool:
        detections_path = manifest.get("detections_path")
        in_path = Path(manifest.get("input_path"))
        trajectory_path = in_path.with_name("trajectory.json")

        populated = await asyncio.to_thread(
            _run_tracking,
            detections_path,
            str(trajectory_path),
            self.config.track_kalman_gate,
            self.config.track_max_missing,
            self.config.track_move_px,
            self.config.track_stationary_len,
            self.config.track_interp_gap,
        )
        logger.info(
            "track: wrote trajectory with %d populated frames to %s",
            populated,
            trajectory_path,
        )
        manifest.put("trajectory_path", str(trajectory_path))
        return True


register_step(TrackStep.name, TrackStep, TrackStepConfig)
