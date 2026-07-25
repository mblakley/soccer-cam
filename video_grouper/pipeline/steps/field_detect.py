"""Field-detect step — find the playing-field boundary polygon once per game.

Runs the in-house field-outline model (10-point boundary polygon) on a few
sampled frames and keeps the highest-confidence polygon. The field is static
for a fixed camera, so one polygon serves the whole game. Writes
``field_polygon.json`` next to the input video and records
``field_polygon_path`` in the manifest, where the downstream steps already
look for it:

- **track** drops off-field ball detections (``track_field_margin``);
- **render** derives mount tilt / leveling roll from the polygon's world-up,
  rejects off-field detections, and bounds the pan to the field's lateral
  extent.

Model source mirrors the ``detect`` step (the freemium boundary lives at TTT,
not here): ``model_key`` licenses a TTT-provided model (free tier), or
``model_path`` loads a local plaintext ``.onnx``.

The polygon artifact is **always produced**: with no model configured, or no
usable polygon found, the step writes the neutral full-frame rectangle
(``source: "full_frame"``). Downstream steps require the polygon and treat the
full frame as "the field is everywhere" — full pan range, centred framing, and
a filter that keeps every in-frame detection.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel

# Top-level import: pulls in onnxruntime/cv2. In a bundle without the inference
# stack importing this module fails and register_steps' try/except omits the
# step — the intended behaviour (same as detect).
from video_grouper.inference.field_detector import (
    _infer_keypoints,
    aggregate_keypoints,
    build_field_polygon,
    create_field_session,
)
from video_grouper.pipeline import register_step
from video_grouper.pipeline.base import PipelineStep, StepContext
from video_grouper.pipeline.manifest import PipelineManifest
from video_grouper.pipeline.steps.licensed_model import build_secure_loader_session

logger = logging.getLogger(__name__)


class FieldDetectStepConfig(BaseModel):
    # Pick exactly one source (or neither — the step then passes through):
    #   model_key:  ask TTT for a license + encrypted artifact (free tier)
    #   model_path: load a plaintext .onnx from disk (community / bring-your-own)
    model_key: str | None = None
    model_path: str | None = None
    field_detect_channel: str | None = None  # canary / beta / stable
    field_detect_pipeline_version: str | None = None
    device: str = "cuda:0"
    # Per-keypoint score floor; points below it don't count as "confident".
    field_score_threshold: float = 0.5
    # Require at least this many truly-confident (>= floor) aggregated keypoints
    # before trusting the polygon (else fall back to the full-frame default).
    field_min_keypoints: int = 6
    # Frames sampled across the middle 80% of the video, aggregated per-keypoint
    # (static camera) — more frames => each keypoint is unoccluded in more of them.
    field_sample_frames: int = 12
    # Min frames a keypoint must clear the floor in to be medianed.
    field_min_confident_frames: int = 1
    # Fill never-confident keypoints (occluded near-sideline foreground) from
    # their best frame so the polygon stays complete (10 points).
    field_fallback_to_best: bool = True


def _video_dims(video_path: str) -> tuple[int, int]:
    """Return ``(width, height)`` of the video's first stream."""
    import av

    with av.open(video_path) as container:
        stream = container.streams.video[0]
        return stream.width, stream.height


def _sample_times(duration: float, n: int) -> list[float]:
    """N timestamps spread across the middle 80% of the video."""
    if duration <= 0 or n <= 1:
        return [max(duration, 0.0) / 2.0]
    lo, hi = 0.1 * duration, 0.9 * duration
    return [lo + (hi - lo) * i / (n - 1) for i in range(n)]


def _detect_field_polygon(
    video_path: str,
    session: Any,
    score_threshold: float,
    min_keypoints: int,
    sample_frames: int,
    min_confident_frames: int = 1,
    fallback_to_best: bool = True,
) -> tuple[list[list[float]], float] | None:
    """Sample frames, aggregate keypoints per index, return (polygon, mean_score).

    The camera is fixed per game, so instead of trusting one frame we run the
    model on ``sample_frames`` frames and take each keypoint's **median** over
    the frames where it's confident (:func:`aggregate_keypoints`). A keypoint
    occluded in some frames (players / foreground spectators) is recovered from
    others, so the polygon comes out **complete (10 points)** far more often
    than any single frame. Returns ``None`` only when too few keypoints are
    truly confident across all frames (the field wasn't found).
    """
    import av

    per_frame: list[tuple[Any, Any]] = []
    container = av.open(video_path)
    try:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        if stream.duration is not None and stream.time_base is not None:
            duration = float(stream.duration * stream.time_base)
        elif container.duration is not None:
            duration = container.duration / av.time_base
        else:
            duration = 0.0

        for t in _sample_times(duration, sample_frames):
            try:
                if stream.time_base:
                    container.seek(
                        int(t / stream.time_base), stream=stream, backward=True
                    )
                frame = next(container.decode(stream), None)
            except Exception as e:  # corrupt packet at this offset — skip
                logger.debug("field_detect: seek/decode failed at %.0fs: %s", t, e)
                continue
            if frame is None:
                continue
            per_frame.append(
                _infer_keypoints(frame.to_ndarray(format="bgr24"), session)
            )
    finally:
        container.close()

    if not per_frame:
        return None
    agg = aggregate_keypoints(
        per_frame, score_threshold, min_confident_frames, fallback_to_best
    )
    confident = [kp for kp in agg if kp[2] >= score_threshold]
    if len(confident) < min_keypoints:
        logger.warning(
            "field_detect: only %d/10 keypoints cleared the floor across %d frames "
            "(< %d) — not trusting the polygon",
            len(confident),
            len(per_frame),
            min_keypoints,
        )
        return None
    polygon = build_field_polygon(agg)
    if polygon is None:
        return None
    mean_score = sum(kp[2] for kp in confident) / len(confident)
    logger.info(
        "field_detect: aggregated %d-point polygon from %d frames (%d confident kpts)",
        len(polygon),
        len(per_frame),
        len(confident),
    )
    return ([[float(x), float(y)] for x, y in polygon], mean_score)


class FieldDetectStep(PipelineStep[FieldDetectStepConfig]):
    name = "field_detect"
    config_model = FieldDetectStepConfig
    consumes = ("input_path",)
    produces = ("field_polygon_path",)
    runtime = "service"
    requires = ("onnxruntime", "cv2", "av")
    resources = ("gpu",)

    async def run(self, manifest: PipelineManifest, ctx: StepContext) -> bool:
        cfg = self.config
        in_path = Path(cast(str, manifest.get("input_path")))

        result = None
        source = "full_frame"
        if cfg.model_key:
            session = await asyncio.to_thread(
                build_secure_loader_session,
                self.name,
                cfg.model_key,
                cfg.field_detect_channel,
                cfg.field_detect_pipeline_version,
                ctx,
            )
            source = "model_key"
        elif cfg.model_path:
            use_gpu = cfg.device.startswith(("cuda", "gpu"))
            session = await asyncio.to_thread(
                create_field_session, Path(cfg.model_path), use_gpu
            )
            source = "model_path"
        else:
            session = None
            logger.info(
                "field_detect: no model_key/model_path configured; using the "
                "full-frame polygon"
            )

        if session is not None:
            result = await asyncio.to_thread(
                _detect_field_polygon,
                str(in_path),
                session,
                cfg.field_score_threshold,
                cfg.field_min_keypoints,
                cfg.field_sample_frames,
                cfg.field_min_confident_frames,
                cfg.field_fallback_to_best,
            )
            if result is None:
                source = "full_frame"
                logger.warning(
                    "field_detect: no valid field polygon detected; falling "
                    "back to the full-frame polygon"
                )

        if result is None:
            # Neutral default: the field IS the frame. Downstream filters keep
            # everything; render frames to the full extent.
            src_w, src_h = await asyncio.to_thread(_video_dims, str(in_path))
            polygon = [
                [0.0, 0.0],
                [float(src_w), 0.0],
                [float(src_w), float(src_h)],
                [0.0, float(src_h)],
            ]
            mean_score = 0.0
        else:
            polygon, mean_score = result

        polygon_path = in_path.with_name("field_polygon.json")
        payload = {
            "polygon": polygon,
            "source": source,
            "mean_score": round(mean_score, 4),
        }
        with open(polygon_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        logger.info(
            "field_detect: %d-point polygon (source=%s, mean_score=%.2f) -> %s",
            len(polygon),
            source,
            mean_score,
            polygon_path,
        )
        manifest.put("field_polygon_path", str(polygon_path))
        return True


register_step(FieldDetectStep.name, FieldDetectStep, FieldDetectStepConfig)
