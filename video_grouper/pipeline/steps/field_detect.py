"""Field-outline detection step — find the playing-field boundary once.

Runs the in-house field-outline model (10-point boundary polygon) on a few
sampled frames and keeps the highest-confidence polygon. The field is static
for a fixed camera, so one polygon serves the whole game: the ``track`` step
uses it to drop off-field ball detections and ``render`` uses it to frame the
broadcast crop. The polygon is written in RAW-source pixel coords; track/
render translate it into stabilized coords via the motion sidecar.

Model source mirrors the ball detector (the freemium boundary lives at TTT):

- **TTT-licensed:** ``model_key`` set → license + decrypt in memory. The
  field-outline model ships as a *free* TTT-provided model.
- **Community / bring-your-own:** ``model_path`` set → plaintext ``.onnx``.

Reprocess override: when ``override_polygon`` is set (the TTT field-mask
editor's user-corrected outline, 10 normalized ``[x, y]`` points), the step
writes that polygon instead of running the model.

The step is **graceful**: with no model configured and no override it writes a
null polygon and passes through, so a pipeline without a field model still
runs (track/render handle a missing polygon). Heavy deps are imported at top
so ``register_steps`` omits the step in a bundle without the inference stack.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel

from video_grouper.inference.field_detector import (
    create_field_session,
    detect_field_keypoints,
)
from video_grouper.inference.field_geometry import build_field_polygon, field_homography
from video_grouper.pipeline import register_step
from video_grouper.pipeline.base import PipelineStep, StepContext
from video_grouper.pipeline.manifest import PipelineManifest

logger = logging.getLogger(__name__)


class FieldDetectStepConfig(BaseModel):
    # Pick one source (same pattern as detect); both unset + no override =
    # graceful pass-through (null polygon).
    model_key: str | None = None
    model_path: str | None = None
    detect_channel: str | None = None
    detect_pipeline_version: str | None = None
    device: str = "cuda:0"
    score_threshold: float = 0.5
    min_keypoints: int = 6
    sample_frames: int = 7
    # Reprocess override: 10 normalized [x, y] points (near 0-4 L->R, far 5-9
    # R->L) from the TTT field-mask editor. When set, written instead of
    # running the model (scaled to source pixels).
    override_polygon: list[list[float]] | None = None


def _build_secure_loader_session(
    model_key: str,
    channel: str | None,
    pipeline_version: str | None,
    ctx: StepContext,
) -> Any:
    """Acquire a license + decrypted field-outline session from TTT."""
    from video_grouper.api_integrations.ttt_api import TTTApiClient
    from video_grouper.ball_tracking.secure_loader import SecureLoader

    if not ctx.ttt_config:
        raise RuntimeError(
            "field_detect: model_key is set but TTT integration is disabled "
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
        "field_detect: licensed %s v%s (%s, provider=%s)",
        loaded.model_key,
        loaded.version,
        loaded.tier,
        loaded.provider,
    )
    return loaded.session


def _video_dims(video_path: str) -> tuple[int, int]:
    """(width, height) of the source video's first video stream."""
    import av

    with av.open(video_path) as container:
        stream = container.streams.video[0]
        return int(stream.width), int(stream.height)


def _sample_times(duration: float, n: int) -> list[float]:
    """N timestamps spread across the middle 80% of the video."""
    if duration <= 0 or n <= 1:
        return [max(duration, 0.0) / 2.0]
    lo, hi = 0.1 * duration, 0.9 * duration
    return [lo + (hi - lo) * i / (n - 1) for i in range(n)]


def _detect_polygon_from_session(
    video_path: str,
    session: Any,
    score_threshold: float,
    min_keypoints: int,
    sample_frames: int,
) -> dict | None:
    """Sample frames, run the model, return the best polygon payload or None."""
    import av

    best: dict | None = None
    best_score = -1.0
    with av.open(video_path) as container:
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
            except Exception as e:  # corrupt packet — skip
                logger.debug("field_detect: seek/decode failed at %.0fs: %s", t, e)
                continue
            if frame is None:
                continue
            frame_bgr = frame.to_ndarray(format="bgr24")
            kpts = detect_field_keypoints(frame_bgr, session, score_threshold)
            detected = [kp for kp in kpts if kp[0] is not None]
            if len(detected) < min_keypoints:
                continue
            polygon = build_field_polygon(kpts)
            if polygon is None:
                continue
            mean_score = sum(kp[2] for kp in detected) / len(detected)
            if mean_score > best_score:
                h = field_homography(kpts)
                best_score = mean_score
                best = {
                    "polygon": [[float(x), float(y)] for x, y in polygon],
                    "keypoints": [[kp[0], kp[1], kp[2]] for kp in kpts],
                    "homography": h.tolist() if h is not None else None,
                    "source": "model",
                    "mean_score": round(mean_score, 4),
                }

    return best


def _override_payload(
    override_polygon: list[list[float]], src_w: int, src_h: int
) -> dict:
    """Scale the editor's normalized points to source pixels -> polygon payload."""
    polygon = [[float(x) * src_w, float(y) * src_h] for x, y in override_polygon]
    keypoints = [[px, py, 1.0] for px, py in polygon]
    h = field_homography([(px, py, 1.0) for px, py in polygon])
    return {
        "polygon": polygon,
        "keypoints": keypoints,
        "homography": h.tolist() if h is not None else None,
        "source": "user_override",
    }


class FieldDetectStep(PipelineStep[FieldDetectStepConfig]):
    name = "field_detect"
    config_model = FieldDetectStepConfig
    consumes = ("input_path",)
    produces = ("field_polygon_path",)
    runtime = "service"
    requires = ("onnxruntime", "cv2")
    resources = ("gpu",)

    async def run(self, manifest: PipelineManifest, ctx: StepContext) -> bool:
        cfg = self.config
        in_path = Path(cast(str, manifest.get("input_path")))
        polygon_path = in_path.with_name("field_polygon.json")

        payload: dict | None = None
        if cfg.override_polygon is not None:
            src_w, src_h = await asyncio.to_thread(_video_dims, str(in_path))
            payload = _override_payload(cfg.override_polygon, src_w, src_h)
            logger.info("field_detect: applied user override polygon")
        elif cfg.model_key or cfg.model_path:
            if cfg.model_key:
                session = await asyncio.to_thread(
                    _build_secure_loader_session,
                    cfg.model_key,
                    cfg.detect_channel,
                    cfg.detect_pipeline_version,
                    ctx,
                )
            else:
                use_gpu = cfg.device.startswith(("cuda", "gpu"))
                session = await asyncio.to_thread(
                    create_field_session, Path(cast(str, cfg.model_path)), use_gpu
                )
            payload = await asyncio.to_thread(
                _detect_polygon_from_session,
                str(in_path),
                session,
                cfg.score_threshold,
                cfg.min_keypoints,
                cfg.sample_frames,
            )
        else:
            logger.info(
                "field_detect: no model_key/model_path/override; passing through "
                "(null polygon — track/render run without field awareness)"
            )

        # Always produce field_polygon_path (graceful: null polygon when none).
        if payload is None:
            payload = {"polygon": None, "keypoints": None, "source": "none"}
            logger.warning("field_detect: no valid polygon; wrote null polygon")
        with open(polygon_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        logger.info(
            "field_detect: %s polygon -> %s",
            payload.get("source"),
            polygon_path,
        )
        manifest.put("field_polygon_path", str(polygon_path))
        return True


register_step(FieldDetectStep.name, FieldDetectStep, FieldDetectStepConfig)
