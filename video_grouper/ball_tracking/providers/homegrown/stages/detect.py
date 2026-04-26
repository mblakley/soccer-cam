"""Detection stage — run the homegrown ball detector on each frame.

Wraps :mod:`video_grouper.inference.ball_detector`. Outputs per-frame
detections to a ``detections.json`` next to the source. The result is a
list of ``{frame_idx, cx, cy, w, h, conf}`` dicts in panoramic pixel
coords.

Two model-source modes:

- **TTT-licensed (production):** when ``model_key`` is set, the stage
  acquires a license from TTT, decrypts the artifact in memory, and
  runs inference against the resulting session. This is the path that
  enforces the entitlement / tier / version-binding contract.
- **Local path (dev / local testing):** when ``model_path`` is set,
  the stage loads a plaintext .onnx from disk via
  :func:`create_session`. Convenient for dev workflows; production
  installs should not use this.
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
from video_grouper.inference.ball_detector import create_session, detect_video

from . import register_stage
from .base import ProcessingStage

logger = logging.getLogger(__name__)


def _run_detection_with_session(
    video_path: str,
    output_json_path: str,
    session: Any,
    confidence: float,
    frame_interval: int,
) -> int:
    """Sync helper: run detection against a pre-built session, write JSON."""
    detections = detect_video(
        Path(video_path),
        session,
        frame_interval=frame_interval,
        conf_threshold=confidence,
    )

    with open(output_json_path, "w", encoding="utf-8") as f:
        json.dump(detections, f)

    return len(detections)


def _run_detection_from_path(
    video_path: str,
    output_json_path: str,
    model_path: str,
    confidence: float,
    frame_interval: int,
    use_gpu: bool,
) -> int:
    """Local-path variant: load the model from disk, then run detection."""
    sess = create_session(Path(model_path), use_gpu=use_gpu)
    return _run_detection_with_session(
        video_path, output_json_path, sess, confidence, frame_interval
    )


class DetectStage(ProcessingStage):
    name = "detect"

    async def run(
        self, artifacts: dict[str, Any], ctx: ProviderContext
    ) -> dict[str, Any] | None:
        cfg = self.provider_config
        in_path = Path(artifacts["input_path"])
        detections_path = in_path.with_name("detections.json")

        if cfg.model_key:
            # Production path: license-acquire, decrypt in memory, run.
            session = await asyncio.to_thread(
                acquire_session,
                model_key=cfg.model_key,
                channel=cfg.detect_channel,
                pipeline_version=cfg.detect_pipeline_version,
                ctx=ctx,
                log_label="detect",
            )
            count = await asyncio.to_thread(
                _run_detection_with_session,
                str(in_path),
                str(detections_path),
                session,
                cfg.detect_confidence,
                cfg.detect_frame_interval,
            )
        elif cfg.model_path:
            # Dev path: plaintext on disk.
            use_gpu = cfg.device.startswith(("cuda", "gpu"))
            count = await asyncio.to_thread(
                _run_detection_from_path,
                str(in_path),
                str(detections_path),
                cfg.model_path,
                cfg.detect_confidence,
                cfg.detect_frame_interval,
                use_gpu,
            )
        else:
            raise RuntimeError(
                "detect: neither model_key nor model_path is configured. "
                "Set [BALL_TRACKING.HOMEGROWN] model_key for TTT-licensed "
                "production use, or model_path for a local plaintext .onnx."
            )

        logger.info("detect: wrote %d detections to %s", count, detections_path)
        return {"detections_path": str(detections_path)}


register_stage(DetectStage.name, DetectStage)
