"""Detection step — run the homegrown heatmap ball detector on the video.

Wraps :mod:`video_grouper.inference.ball_detector`. Reads ``input_path`` (+ the
``field_detect`` step's polygon), writes a ``candidates/1`` artifact next to it,
and records ``detections_path``. Detection is the expensive step, so it emits the
RAW top-K heatmap peaks per sampled frame above a low score floor — game-ball
SELECTION happens cheaply downstream in the ``ball_select`` step, where it can be
re-tuned without re-running detection.

Model source (the freemium boundary lives at TTT, not here — see the project's
model-tiering notes):

- **TTT-licensed:** when ``model_key`` is set, acquire a license from TTT,
  decrypt the artifact in memory, and run inference. A *free* TTT account
  licenses the free model; a *premium* account licenses the premium model —
  the tier is resolved server-side.
- **Community / bring-your-own:** when ``model_path`` is set, load a plaintext
  .onnx from disk. No TTT account required.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, cast

import numpy as np
from pydantic import BaseModel

# Top-level import: pulls in onnxruntime. In a bundle without the inference
# stack (e.g. the tray bundle) importing this module fails, and register_steps'
# try/except simply omits the step — which is the intended behaviour.
from video_grouper.inference.ball_detector import (
    create_session,
    detect_video_candidates,
)
from video_grouper.pipeline import register_step
from video_grouper.pipeline.base import PipelineStep, StepContext
from video_grouper.pipeline.manifest import PipelineManifest
from video_grouper.pipeline.steps.licensed_model import build_secure_loader_session

logger = logging.getLogger(__name__)


class BallDetectStepConfig(BaseModel):
    # Pick exactly one source:
    #   model_key:  ask TTT for a license + encrypted artifact (TTT free/premium)
    #   model_path: load a plaintext .onnx from disk (community / bring-your-own)
    model_key: str | None = None
    model_path: str | None = None
    detect_channel: str | None = None  # canary / beta / stable
    detect_pipeline_version: str | None = None
    device: str = "cuda:0"
    # SAVE FLOOR, not the working threshold: detection is the expensive step, so it
    # writes the raw top-K peaks above this low floor; selection re-weighs them
    # downstream where re-tuning is cheap.
    detect_confidence: float = 0.1
    # Inference runs every Nth source frame (every frame is still decoded — the
    # detector consumes a 3-consecutive-frame temporal stack).
    detect_frame_interval: int = 8
    detect_top_k: int = 24
    detect_min_distance: int = 3
    detect_tile_w: int = 2560
    detect_overlap: int = 256
    # Band geometry: far-side margin above the far touchline (airborne balls) and
    # the optional isotropic band width (cross-camera ball-size normalization;
    # None = native resolution).
    detect_far_margin: float = 400.0
    detect_target_width: int | None = None


def _load_polygon(path: str | None) -> np.ndarray | None:
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning("detect: field polygon %s unusable (%s)", path, e)
        return None
    poly = payload.get("polygon")
    if not poly or len(poly) < 3:
        return None
    return np.asarray(poly, float)


def _run_detection_with_session(
    video_path: str,
    output_json_path: str,
    session: Any,
    polygon: np.ndarray,
    cfg: BallDetectStepConfig,
) -> int:
    """Sync helper: detect against a pre-built session, write the candidates/1 artifact."""
    cands, info = detect_video_candidates(
        Path(video_path),
        session,
        polygon,
        stride=cfg.detect_frame_interval,
        top_k=cfg.detect_top_k,
        threshold=cfg.detect_confidence,
        min_distance=cfg.detect_min_distance,
        tile_w=cfg.detect_tile_w,
        overlap=cfg.detect_overlap,
        far_margin=cfg.detect_far_margin,
        target_width=cfg.detect_target_width,
    )
    artifact = {
        "schema": "candidates/1",
        "stride": cfg.detect_frame_interval,
        "src_w": info["src_w"],
        "src_h": info["src_h"],
        "fps": info["fps"],
        "n_frames": info["n_frames"],
        "frames": {str(g): rows for g, rows in sorted(cands.items())},
    }
    with open(output_json_path, "w", encoding="utf-8") as f:
        json.dump(artifact, f)
    return len(cands)


class BallDetectStep(PipelineStep[BallDetectStepConfig]):
    name = "ball_detect"
    config_model = BallDetectStepConfig
    consumes = ("input_path",)
    produces = ("detections_path",)
    runtime = "service"
    requires = ("onnxruntime", "cv2")
    resources = ("gpu",)

    async def run(self, manifest: PipelineManifest, ctx: StepContext) -> bool:
        cfg = self.config
        # input_path is the immutable source the runner binds before run().
        in_path = Path(cast(str, manifest.get("input_path")))
        detections_path = in_path.with_name("detections.json")

        # The heatmap detector runs on the field BAND: it needs the 10-point
        # polygon from the upstream field_detect step. This is the single
        # homegrown path — a missing polygon is a hard error, not a fallback.
        polygon = _load_polygon(manifest.get("field_polygon_path"))
        if polygon is None or len(polygon) < 10:
            raise RuntimeError(
                "detect: the homegrown detector requires the field_detect step's "
                "10-point field polygon (field_polygon_path) to crop the field "
                "band. Run field_detect first / fix its output."
            )

        if cfg.model_key:
            session = await asyncio.to_thread(
                build_secure_loader_session,
                self.name,
                cfg.model_key,
                cfg.detect_channel,
                cfg.detect_pipeline_version,
                ctx,
            )
        elif cfg.model_path:
            use_gpu = cfg.device.startswith(("cuda", "gpu"))
            session = await asyncio.to_thread(
                create_session, Path(cfg.model_path), use_gpu
            )
        else:
            raise RuntimeError(
                "detect: neither model_key nor model_path is configured. Set "
                "model_key for a TTT-licensed model, or model_path for a "
                "community / bring-your-own .onnx."
            )

        count = await asyncio.to_thread(
            _run_detection_with_session,
            str(in_path),
            str(detections_path),
            session,
            polygon,
            cfg,
        )
        logger.info("detect: wrote %d candidate frames to %s", count, detections_path)
        manifest.put("detections_path", str(detections_path))
        return True


register_step(BallDetectStep.name, BallDetectStep, BallDetectStepConfig)
