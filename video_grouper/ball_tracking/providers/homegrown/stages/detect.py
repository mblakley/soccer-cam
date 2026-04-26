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
from video_grouper.inference.field_geometry import is_on_field

from . import register_stage
from .base import ProcessingStage

logger = logging.getLogger(__name__)


def _filter_by_polygon(
    detections: list[dict],
    field_polygon_path: str | None,
    margin_px: float,
) -> tuple[list[dict], int]:
    """Drop detections that fall outside the field polygon (with optional margin).

    No-op when ``field_polygon_path`` is missing or has no polygon. Returns
    ``(filtered_detections, n_dropped)``.
    """
    if not field_polygon_path:
        return detections, 0
    try:
        with open(field_polygon_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except FileNotFoundError:
        return detections, 0
    poly_raw = payload.get("polygon")
    if not poly_raw:
        return detections, 0

    import numpy as np

    polygon = np.array(poly_raw, dtype=np.float32)
    kept = [
        d
        for d in detections
        if is_on_field(float(d["cx"]), float(d["cy"]), polygon, margin=margin_px)
    ]
    return kept, len(detections) - len(kept)


def _run_detection_with_session(
    video_path: str,
    output_json_path: str,
    session: Any,
    confidence: float,
    frame_interval: int,
    field_polygon_path: str | None,
    field_filter_margin_px: float,
) -> int:
    """Sync helper: run detection against a pre-built session, write JSON."""
    detections = detect_video(
        Path(video_path),
        session,
        frame_interval=frame_interval,
        conf_threshold=confidence,
    )

    raw_count = len(detections)
    detections, dropped = _filter_by_polygon(
        detections, field_polygon_path, field_filter_margin_px
    )
    if dropped > 0:
        logger.info(
            "detect: field-polygon filter dropped %d/%d off-field detections (margin=%dpx)",
            dropped,
            raw_count,
            int(field_filter_margin_px),
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
    field_polygon_path: str | None,
    field_filter_margin_px: float,
) -> int:
    """Local-path variant: load the model from disk, then run detection."""
    sess = create_session(Path(model_path), use_gpu=use_gpu)
    return _run_detection_with_session(
        video_path,
        output_json_path,
        sess,
        confidence,
        frame_interval,
        field_polygon_path,
        field_filter_margin_px,
    )


class DetectStage(ProcessingStage):
    name = "detect"

    async def run(
        self, artifacts: dict[str, Any], ctx: ProviderContext
    ) -> dict[str, Any] | None:
        cfg = self.provider_config
        in_path = Path(artifacts["input_path"])
        detections_path = in_path.with_name("detections.json")

        field_polygon_path = artifacts.get("field_polygon_path")

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
                field_polygon_path,
                cfg.detect_field_filter_margin_px,
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
                field_polygon_path,
                cfg.detect_field_filter_margin_px,
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
