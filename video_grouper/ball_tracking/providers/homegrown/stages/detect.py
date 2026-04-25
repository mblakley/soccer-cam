"""Detection stage — run the homegrown ball detector on each frame.

Wraps :mod:`training.inference.external_ball_detector`. Outputs
per-frame detections to a ``detections.json`` next to the source.
The result is a list of ``{frame_idx, cx, cy, w, h, conf}`` dicts in
panoramic pixel coords.

Heavy deps (``onnxruntime``, ``cv2``) are imported lazily inside the
sync helper so the tray app doesn't load them unless this stage runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from video_grouper.ball_tracking.base import ProviderContext

from . import register_stage
from .base import ProcessingStage

logger = logging.getLogger(__name__)


def _run_detection(
    video_path: str,
    output_json_path: str,
    model_path: str,
    confidence: float,
    frame_interval: int,
    use_gpu: bool,
) -> int:
    """Sync helper: run detection, write JSON. Returns detection count.

    Uses ``importlib.import_module`` rather than a ``from … import`` to keep
    the heavy training-pipeline deps (torch, ultralytics, opencv) outside
    PyInstaller's static modulegraph. Otherwise the service / tray exes
    balloon past NSIS's 32-bit mmap ceiling at install time.
    """
    import importlib
    from pathlib import Path as _Path

    ext_ball = importlib.import_module("training.inference.external_ball_detector")

    sess = ext_ball.create_session(_Path(model_path), use_gpu=use_gpu)
    detections = ext_ball.detect_video(
        _Path(video_path),
        sess,
        frame_interval=frame_interval,
        conf_threshold=confidence,
    )

    with open(output_json_path, "w", encoding="utf-8") as f:
        json.dump(detections, f)

    return len(detections)


class DetectStage(ProcessingStage):
    name = "detect"

    async def run(
        self, artifacts: dict[str, Any], ctx: ProviderContext
    ) -> dict[str, Any] | None:
        model_path = self.provider_config.model_path
        if not model_path:
            raise RuntimeError(
                "detect: model_path is not configured "
                "(set [BALL_TRACKING.HOMEGROWN] model_path in config.ini)"
            )

        in_path = Path(artifacts["input_path"])
        detections_path = in_path.with_name("detections.json")

        use_gpu = self.provider_config.device.startswith(("cuda", "gpu"))
        count = await asyncio.to_thread(
            _run_detection,
            str(in_path),
            str(detections_path),
            model_path,
            self.provider_config.detect_confidence,
            self.provider_config.detect_frame_interval,
            use_gpu,
        )
        logger.info("detect: wrote %d detections to %s", count, detections_path)
        return {"detections_path": str(detections_path)}


register_stage(DetectStage.name, DetectStage)
