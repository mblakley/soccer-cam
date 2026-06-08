"""Detection step — run the homegrown ball detector on each frame.

Wraps :mod:`video_grouper.inference.ball_detector`. Reads ``input_path``,
writes a per-frame ``detections.json`` next to it, and records
``detections_path``. Each detection is ``{frame_idx, cx, cy, w, h, conf}`` in
panoramic pixel coords.

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
from typing import Any, Optional

import numpy as np
from pydantic import BaseModel

# Top-level import: pulls in onnxruntime/cv2. In a bundle without the inference
# stack (e.g. the tray bundle) importing this module fails, and register_steps'
# try/except simply omits the step — which is the intended behaviour.
from video_grouper.inference.ball_detector import create_session, detect_video
from video_grouper.pipeline import register_step
from video_grouper.pipeline.base import PipelineStep, StepContext
from video_grouper.pipeline.manifest import PipelineManifest

logger = logging.getLogger(__name__)


class DetectStepConfig(BaseModel):
    # Pick exactly one source:
    #   model_key:  ask TTT for a license + encrypted artifact (TTT free/premium)
    #   model_path: load a plaintext .onnx from disk (community / bring-your-own)
    model_key: Optional[str] = None
    model_path: Optional[str] = None
    detect_channel: Optional[str] = None  # canary / beta / stable
    detect_pipeline_version: Optional[str] = None
    device: str = "cuda:0"
    detect_confidence: float = 0.45
    detect_frame_interval: int = 4
    # Reject ball detections whose center falls >field_margin px outside the field
    # polygon (manifest ``field_polygon_path``). Off-field FPs — the camera's
    # burned-in timestamp, spectators, a scoreboard — otherwise out-compete the
    # real ball. Always on when a field polygon is available.
    field_margin: float = 50.0


def _load_field_polygon(path: str | None) -> "np.ndarray | None":
    """Load the field-perimeter polygon from the manifest's ``field_polygon_path``
    (the same artifact the render step consumes). Returns None if unavailable."""
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning(
            "detect: field polygon %s unusable (%s); detecting unmasked", path, e
        )
        return None
    poly = payload.get("polygon")
    if not poly or len(poly) < 3:
        return None
    return np.array(poly, dtype=np.float32)


def _run_detection_with_session(
    video_path: str,
    output_json_path: str,
    session: Any,
    confidence: float,
    frame_interval: int,
    field_polygon: "np.ndarray | None" = None,
    field_margin: float = 50.0,
) -> int:
    """Sync helper: run detection against a pre-built session, write JSON."""
    detections = detect_video(
        Path(video_path),
        session,
        frame_interval=frame_interval,
        conf_threshold=confidence,
        field_polygon=field_polygon,
        field_margin=field_margin,
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
    field_polygon: "np.ndarray | None" = None,
    field_margin: float = 50.0,
) -> int:
    """Community/BYO variant: load the model from disk, then run detection."""
    sess = create_session(Path(model_path), use_gpu=use_gpu)
    return _run_detection_with_session(
        video_path,
        output_json_path,
        sess,
        confidence,
        frame_interval,
        field_polygon=field_polygon,
        field_margin=field_margin,
    )


def _build_secure_loader_session(
    model_key: str,
    channel: str | None,
    pipeline_version: str | None,
    ctx: StepContext,
) -> Any:
    """Acquire a license + decrypted ONNX session from TTT.

    Constructs a TTTApiClient from ``ctx.ttt_config`` and runs the SecureLoader
    against it. Returns the loaded ``InferenceSession``.
    """
    from video_grouper.api_integrations.ttt_api import TTTApiClient
    from video_grouper.ball_tracking.secure_loader import SecureLoader

    if not ctx.ttt_config:
        raise RuntimeError(
            "detect: model_key is set but TTT integration is disabled "
            "(set [TTT] enabled = true and configure credentials, or fall back "
            "to model_path for a community / bring-your-own model)"
        )

    cfg = ctx.ttt_config
    client = TTTApiClient(
        supabase_url=cfg.get("supabase_url", ""),
        anon_key=cfg.get("anon_key", ""),
        api_base_url=cfg.get("api_base_url", ""),
        storage_path=str(ctx.storage_path),
    )
    public_keys = cfg.get("plugin_signing_public_keys") or []
    loader = SecureLoader(client, public_keys, state_storage_path=str(ctx.storage_path))
    loaded = loader.acquire(
        model_key, channel=channel, pipeline_version=pipeline_version
    )
    logger.info(
        "detect: licensed %s v%s (%s, provider=%s)",
        loaded.model_key,
        loaded.version,
        loaded.tier,
        loaded.provider,
    )
    return loaded.session


class DetectStep(PipelineStep):
    name = "detect"
    config_model = DetectStepConfig
    consumes = ("input_path",)
    produces = ("detections_path",)
    runtime = "service"
    requires = ("onnxruntime", "cv2")
    resources = ("gpu",)

    async def run(self, manifest: PipelineManifest, ctx: StepContext) -> bool:
        cfg = self.config
        in_path = Path(manifest.get("input_path"))
        detections_path = in_path.with_name("detections.json")

        field_polygon = _load_field_polygon(manifest.get("field_polygon_path"))
        if field_polygon is None:
            logger.warning(
                "detect: no usable field_polygon_path in manifest — detecting "
                "UNMASKED; off-field false positives (camera timestamp, spectators, "
                "scoreboard) will not be filtered out"
            )

        if cfg.model_key:
            session = await asyncio.to_thread(
                _build_secure_loader_session,
                cfg.model_key,
                cfg.detect_channel,
                cfg.detect_pipeline_version,
                ctx,
            )
            count = await asyncio.to_thread(
                _run_detection_with_session,
                str(in_path),
                str(detections_path),
                session,
                cfg.detect_confidence,
                cfg.detect_frame_interval,
                field_polygon,
                cfg.field_margin,
            )
        elif cfg.model_path:
            use_gpu = cfg.device.startswith(("cuda", "gpu"))
            count = await asyncio.to_thread(
                _run_detection_from_path,
                str(in_path),
                str(detections_path),
                cfg.model_path,
                cfg.detect_confidence,
                cfg.detect_frame_interval,
                use_gpu,
                field_polygon,
                cfg.field_margin,
            )
        else:
            raise RuntimeError(
                "detect: neither model_key nor model_path is configured. Set "
                "model_key for a TTT-licensed model, or model_path for a "
                "community / bring-your-own .onnx."
            )

        logger.info("detect: wrote %d detections to %s", count, detections_path)
        manifest.put("detections_path", str(detections_path))
        return True


register_step(DetectStep.name, DetectStep, DetectStepConfig)
