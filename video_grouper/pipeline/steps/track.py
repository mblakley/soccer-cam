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
            json.dump([], f, sort_keys=True)
        return 0

    # Deterministic per-frame detection ordering: ball_tracker.update() iterates
    # the detections list in-order, and Kalman gating + association is order-
    # dependent. The JSON ordering from detect is already deterministic with
    # sort_keys, but we re-sort here to be defensive against any future input
    # source that doesn't guarantee it.
    for frame_idx in by_frame:
        by_frame[frame_idx].sort(key=lambda d: (d.x, d.y, d.confidence))

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
        json.dump(trajectory, f, sort_keys=True)

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
        )
        logger.info(
            "track: wrote trajectory with %d populated frames to %s",
            populated,
            trajectory_path,
        )
        manifest.put("trajectory_path", str(trajectory_path))

        if ctx.dump_intermediates_dir is not None:
            import shutil

            dump = ctx.dump_intermediates_dir
            dump.mkdir(parents=True, exist_ok=True)
            shutil.copy2(trajectory_path, dump / "trajectory.json")
            logger.info("track: dumped parity baseline to %s", dump / "trajectory.json")

        return True


register_step(TrackStep.name, TrackStep, TrackStepConfig)
