"""``transform_detections`` pipeline step — apply a ``motion.json`` to
existing raw-coord ball detections, producing stabilized-coord detections
WITHOUT re-running ONNX inference.

The expensive part of "redo with different stabilization" is the
detection pass (GPU-bound ONNX, ~minutes per game). The stabilization
itself is per-frame 2×3 affine math that costs microseconds — and a
ball detection is just a single ``(cx, cy)`` point per frame. Forwarding
the existing detections through the new ``motion.json`` is exactly the
work ``FrameStabilizer.apply`` does to a frame, restricted to two
numbers.

This step exists so the **reprocess** flow (different
``stabilization_strength``, same source video) can re-render without
re-detecting. In a fresh run the regular ``detect`` step with
``detect_stabilize = True`` is the canonical path; ``transform_detections``
is the cheap-reprocess shortcut for after-the-fact strength tweaks.

Output: writes ``detections_stabilized.json`` to the recording-group
dir and updates the manifest's ``detections_path`` to point at it.
Downstream ``track``/``render`` are unchanged — they just see the same
schema with stabilized coords.
"""

from __future__ import annotations

import asyncio
import json
import logging

import numpy as np
from pydantic import BaseModel

from video_grouper.pipeline import register_step
from video_grouper.pipeline.base import PipelineStep, StepContext
from video_grouper.pipeline.manifest import PipelineManifest

logger = logging.getLogger(__name__)


class TransformDetectionsStepConfig(BaseModel):
    """Output filename + which detection fields hold the source-coord
    centroid. Defaults match :mod:`video_grouper.inference.ball_detector`."""

    transform_detections_output_name: str = "detections_stabilized.json"
    # Field names of the (x, y) centroid in each detection dict. Kept
    # configurable because other detector schemas (player boxes,
    # keypoints) reuse this step in principle.
    transform_detections_x_field: str = "cx"
    transform_detections_y_field: str = "cy"


def _transform_detections_sync(
    detections_path: str,
    motion_path: str,
    output_path: str,
    x_field: str,
    y_field: str,
) -> dict:
    """Read detections + motion.json, transform each point, write output.

    Returns a summary dict for logging.
    """
    from video_grouper.inference.stabilization import FrameStabilizer

    stabilizer = FrameStabilizer.from_json(motion_path)
    with open(detections_path, encoding="utf-8") as f:
        detections = json.load(f)
    if not isinstance(detections, list):
        raise ValueError(
            f"transform_detections: {detections_path!r} did not contain a list of "
            f"detection dicts; got {type(detections).__name__}"
        )

    # Bucket by frame_idx so we can call transform_points once per frame
    # with all that frame's points (vectorised inverse + matmul).
    by_frame: dict[int, list[int]] = {}
    for i, det in enumerate(detections):
        fi = int(det["frame_idx"])
        by_frame.setdefault(fi, []).append(i)

    n_transformed = 0
    for frame_idx, det_indices in by_frame.items():
        pts = np.array(
            [[detections[i][x_field], detections[i][y_field]] for i in det_indices],
            dtype=np.float32,
        )
        stab_pts = stabilizer.transform_points(pts, frame_idx)
        for det_idx, (xs, ys) in zip(det_indices, stab_pts, strict=False):
            detections[det_idx][x_field] = float(xs)
            detections[det_idx][y_field] = float(ys)
            n_transformed += 1

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(detections, f)
    return {
        "detections_in": len(detections),
        "detections_transformed": n_transformed,
        "frames_with_detections": len(by_frame),
        "stabilizer_mode": stabilizer._mode,
        "output_shape": stabilizer.output_shape,
    }


class TransformDetectionsStep(PipelineStep):
    """Apply ``motion.json`` to existing detections, no ONNX rerun."""

    name = "transform_detections"
    config_model = TransformDetectionsStepConfig
    consumes = ("detections_path", "motion_path")
    produces = ("detections_path",)
    runtime = "service"
    requires = ("cv2", "numpy")
    resources = ()

    async def run(self, manifest: PipelineManifest, ctx: StepContext) -> bool:
        cfg = self.config
        detections_path = manifest.get("detections_path")
        motion_path = manifest.get("motion_path")
        out_path = ctx.group_dir / cfg.transform_detections_output_name

        summary = await asyncio.to_thread(
            _transform_detections_sync,
            str(detections_path),
            str(motion_path),
            str(out_path),
            cfg.transform_detections_x_field,
            cfg.transform_detections_y_field,
        )
        logger.info(
            "transform_detections[%s]: %d detections in %d frames -> %s "
            "(output_shape=%s)",
            summary["stabilizer_mode"],
            summary["detections_transformed"],
            summary["frames_with_detections"],
            out_path,
            summary["output_shape"],
        )
        # Re-point the manifest at the stabilized version so downstream
        # track/render consume it instead of the original raw-coord file.
        manifest.put("detections_path", str(out_path))
        return True


register_step(
    TransformDetectionsStep.name,
    TransformDetectionsStep,
    TransformDetectionsStepConfig,
)
