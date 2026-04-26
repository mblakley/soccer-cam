"""Field-mask stage — detect the playing-field polygon for downstream stages.

Mirrors :class:`DetectStage`: acquire a keypoint model (TTT-licensed via
SecureLoader, or local plaintext for dev), run inference on the first
frame (or sampled frames if ``field_mask_sample_seconds > 0``), and emit
``field_polygon.json`` next to the source. The render stage consumes this
in subsequent commits to:

- bound the camera's pan to the field's lateral extent (Phase 3),
- classify ball position into field zones (Phase 4),
- detect game-event states like near-goal / near-corner (Phase 5).

Skips silently if neither ``field_mask_model_key`` nor
``field_mask_model_path`` is configured — installs that don't have a
field model still get the rest of the homegrown pipeline.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from video_grouper.ball_tracking.base import ProviderContext
from video_grouper.ball_tracking.providers.homegrown._secure_loader_helpers import (
    acquire_session,
)
from video_grouper.inference.field_geometry import (
    build_field_polygon,
    field_homography,
)

from . import register_stage
from .base import ProcessingStage

logger = logging.getLogger(__name__)


def _read_first_frame_bgr(video_path: str):
    """Pull the first decoded frame from ``video_path`` as a BGR ndarray."""
    import av
    import cv2

    with av.open(video_path) as container:
        stream = container.streams.video[0]
        for frame in container.decode(stream):
            rgb = frame.to_ndarray(format="rgb24")
            return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    raise RuntimeError(f"field_mask: no frames decoded from {video_path}")


def _detect_field(session: Any, frame_bgr, score_threshold: float) -> dict | None:
    """Run keypoint inference on one frame, return the polygon + homography dict."""
    from video_grouper.inference.field_detector import detect_field_keypoints

    keypoints = detect_field_keypoints(frame_bgr, session, score_threshold)
    detected = sum(1 for kp in keypoints if kp[0] is not None)
    logger.info(
        "field_mask: %d/10 keypoints above threshold %.2f", detected, score_threshold
    )

    polygon = build_field_polygon(keypoints)
    homography = field_homography(keypoints)
    if polygon is None and homography is None:
        return None

    return {
        "keypoints": [{"x": kp[0], "y": kp[1], "score": kp[2]} for kp in keypoints],
        "polygon": polygon.tolist() if polygon is not None else None,
        "homography": homography.tolist() if homography is not None else None,
    }


def _run_field_mask_with_session(
    video_path: str,
    output_json_path: str,
    session: Any,
    confidence: float,
) -> dict | None:
    """Sync helper: detect field on the first frame, persist JSON."""
    frame_bgr = _read_first_frame_bgr(video_path)
    src_h, src_w = frame_bgr.shape[:2]

    result = _detect_field(session, frame_bgr, confidence)
    if result is None:
        logger.warning(
            "field_mask: no usable polygon — downstream stages will see no field constraints"
        )
        # Still write the file so downstream knows we ran (with nulls).
        result = {"keypoints": None, "polygon": None, "homography": None}

    result["src_w"] = src_w
    result["src_h"] = src_h

    with open(output_json_path, "w", encoding="utf-8") as f:
        json.dump(result, f)

    return result


def _run_field_mask_from_path(
    video_path: str,
    output_json_path: str,
    model_path: str,
    confidence: float,
    use_gpu: bool,
) -> dict | None:
    """Local-path variant: load the model from disk, then run."""
    from pathlib import Path as _Path

    from video_grouper.inference.field_detector import create_field_session

    sess = create_field_session(_Path(model_path), use_gpu=use_gpu)
    return _run_field_mask_with_session(video_path, output_json_path, sess, confidence)


class FieldMaskStage(ProcessingStage):
    name = "field_mask"

    async def run(
        self, artifacts: dict[str, Any], ctx: ProviderContext
    ) -> dict[str, Any] | None:
        cfg = self.provider_config

        if not cfg.field_mask_model_key and not cfg.field_mask_model_path:
            logger.info(
                "field_mask: no model configured (set field_mask_model_key for "
                "TTT-licensed or field_mask_model_path for local) — skipping"
            )
            return None

        in_path = Path(artifacts["input_path"])
        polygon_path = in_path.with_name("field_polygon.json")

        if cfg.field_mask_model_key:
            session = await asyncio.to_thread(
                acquire_session,
                model_key=cfg.field_mask_model_key,
                channel=cfg.field_mask_channel,
                pipeline_version=cfg.field_mask_pipeline_version,
                ctx=ctx,
                log_label="field_mask",
            )
            await asyncio.to_thread(
                _run_field_mask_with_session,
                str(in_path),
                str(polygon_path),
                session,
                cfg.field_mask_confidence,
            )
        else:
            use_gpu = cfg.device.startswith(("cuda", "gpu"))
            await asyncio.to_thread(
                _run_field_mask_from_path,
                str(in_path),
                str(polygon_path),
                cfg.field_mask_model_path,
                cfg.field_mask_confidence,
                use_gpu,
            )

        logger.info("field_mask: wrote %s", polygon_path)
        return {"field_polygon_path": str(polygon_path)}


register_stage(FieldMaskStage.name, FieldMaskStage)
