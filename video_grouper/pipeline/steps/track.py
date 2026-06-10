"""Tracking step — filter raw detections, then link them into a smoothed trajectory.

Wraps :mod:`video_grouper.inference.ball_tracker`. Reads ``detections_path`` (the RAW per-frame
detections JSON written by the detect step), applies the tunable confidence + field-location
filters, runs the Kalman tracker, then stitches the ball's gated track fragments into a per-frame
``trajectory.json`` (one ``[x, y]`` row per source frame; ``null`` when no estimate is available).
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import numpy as np
from pydantic import BaseModel

from video_grouper.inference.ball_tracker import BallTracker, Detection
from video_grouper.pipeline import register_step
from video_grouper.pipeline.base import PipelineStep, StepContext
from video_grouper.pipeline.manifest import PipelineManifest

logger = logging.getLogger(__name__)


class TrackStepConfig(BaseModel):
    # Tunable filters on the RAW detections (detect saves all candidates above its low floor):
    #   confidence threshold + field-polygon location filter. Applied here, cheaply, so they can be
    #   re-swept without re-running detection. The off-field reject (timestamp/spectators/scoreboard)
    #   needs a field_polygon_path in the manifest; absent ⇒ confidence filter only.
    track_conf_threshold: float = 0.45
    track_field_margin: float = 50.0
    track_kalman_gate: float = 200.0
    track_max_missing: int = 15
    # Trajectory stitching (see BallTracker.build_trajectory): the ball is gated into several short
    # tracks, so stitch them best-first instead of keeping only the longest. A track is dropped if it
    # is a sustained stationary FP (sprinkler/bystander: spans < track_move_px over >= stationary_len
    # frames) OR a fixed object (spans < track_tiny_span_px at any length — a real ball jitters more,
    # so a rigider track is a marker/post the camera must not hold on). Gaps up to interp are filled.
    track_move_px: float = 80.0
    track_stationary_len: int = 20
    track_tiny_span_px: float = 6.0
    track_interp_gap: int = 16


def _load_field_polygon(path: str | None) -> "np.ndarray | None":
    """Load the field-perimeter polygon from the manifest's ``field_polygon_path`` (the same
    artifact the render step consumes). Returns None if unavailable."""
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning(
            "track: field polygon %s unusable (%s); skipping location filter", path, e
        )
        return None
    poly = payload.get("polygon")
    if not poly or len(poly) < 3:
        return None
    return np.array(poly, dtype=np.float32)


def _run_tracking(
    detections_path: str,
    output_json_path: str,
    gate_distance: float,
    max_missing: int,
    move_px: float = 80.0,
    stationary_len: int = 20,
    interp_gap: int = 16,
    conf_threshold: float = 0.45,
    field_polygon: "np.ndarray | None" = None,
    field_margin: float = 50.0,
    tiny_span_px: float = 6.0,
) -> int:
    """Load raw detections, apply the (tunable) confidence + field-location filters, run the tracker,
    write the trajectory JSON."""
    with open(detections_path, "r", encoding="utf-8") as f:
        per_frame: list[dict] = json.load(f)

    # Tunable filters on the RAW detections — kept out of the expensive detect step so they can be
    # re-swept without re-detecting.
    per_frame = [d for d in per_frame if float(d.get("conf", 1.0)) >= conf_threshold]
    if field_polygon is not None:
        from video_grouper.inference.field_detector import (
            is_on_field,
        )  # cv2 — lazy (tray bundle)

        per_frame = [
            d
            for d in per_frame
            if is_on_field(float(d["cx"]), float(d["cy"]), field_polygon, field_margin)
        ]

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
        tiny_span_px=tiny_span_px,
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
        field_polygon = _load_field_polygon(manifest.get("field_polygon_path"))

        populated = await asyncio.to_thread(
            _run_tracking,
            detections_path,
            str(trajectory_path),
            self.config.track_kalman_gate,
            self.config.track_max_missing,
            self.config.track_move_px,
            self.config.track_stationary_len,
            self.config.track_interp_gap,
            self.config.track_conf_threshold,
            field_polygon,
            self.config.track_field_margin,
            self.config.track_tiny_span_px,
        )
        logger.info(
            "track: wrote trajectory with %d populated frames to %s",
            populated,
            trajectory_path,
        )
        manifest.put("trajectory_path", str(trajectory_path))
        return True


register_step(TrackStep.name, TrackStep, TrackStepConfig)
