"""``frame_fanout`` pipeline step: decode an expensive source ONCE and feed every decoded
frame, in memory, to N per-frame consumers (e.g. several render variants for a side-by-side
model comparison, or a detector + a render sharing one decode).

This is the in-memory complement to the file-based step handoff. The step's *boundary* stays
file-in / files-out — so the runner's resumability + output validation are unchanged — while
*inside* the step the costly decode is shared across consumers. Rendering one game three ways
(AutoCam dets / model A / model B) is then a single ``frame_fanout`` with three ``render``
consumers: one HEVC decode instead of three.
"""

from __future__ import annotations

import asyncio
import logging
from typing import cast

from pydantic import BaseModel

from video_grouper.pipeline import register_step
from video_grouper.pipeline.base import PipelineStep, StepContext
from video_grouper.pipeline.frame_consumer import FrameSourceInfo, create_frame_consumer
from video_grouper.pipeline.manifest import PipelineManifest

logger = logging.getLogger(__name__)


class ConsumerSpec(BaseModel):
    """One consumer in a fan-out: its registered ``type`` plus its raw config dict (validated
    by the consumer's own ``config_model`` when instantiated). Mirrors a pipeline ``StepSpec``."""

    type: str
    config: dict


class FanoutStepConfig(BaseModel):
    consumers: list[ConsumerSpec]
    # When true and the manifest carries a ``motion_path``, the fanout
    # decode loop applies :class:`FrameStabilizer` ONCE per decoded frame
    # and passes the stabilized frame to every consumer (with a
    # FrameSourceInfo reporting the stabilized output dims). Consumers
    # therefore don't need their own stabilizer — the per-frame
    # ``warpAffine`` work happens once even with N consumers.
    fanout_stabilize: bool = False


class FrameFanoutStep(PipelineStep[FanoutStepConfig]):
    """Single-decode, multi-consumer fan-out over one input video.

    Optionally applies frame stabilization in the decode loop so that all
    consumers receive already-stabilized frames (one apply for N
    consumers, with polygon-zone blend's 3× cost amortized across the
    full downstream pipeline).
    """

    name = "frame_fanout"
    config_model = FanoutStepConfig
    runtime = "service"
    requires = ("av", "cv2")
    resources = ("ram_heavy",)

    def __init__(self, config: FanoutStepConfig):
        super().__init__(config)
        # Instantiate consumers now so consumes/produces can aggregate their artifact keys
        # (the runner reads step.consumes before running). A bad type/config raises here and
        # the runner reports it as a failed-to-construct step.
        self._consumers = [
            create_frame_consumer(c.type, c.config) for c in config.consumers
        ]

    # This step's consumes/produces are dynamic (aggregated from its consumers),
    # so it overrides the base's writeable class attribute with a read-only
    # property. mypy flags the writeable->property override, but it's the
    # intended design and read-only is correct here.
    @property
    def consumes(self) -> tuple[str, ...]:  # type: ignore[override]
        keys = ["input_path"]
        if self.config.fanout_stabilize:
            keys.append("motion_path")
        for c in self._consumers:
            keys.extend(c.consumes)
        return tuple(dict.fromkeys(keys))  # dedupe, order-preserving

    @property
    def produces(self) -> tuple[str, ...]:  # type: ignore[override]
        keys: list[str] = []
        for c in self._consumers:
            keys.extend(c.produces)
        return tuple(dict.fromkeys(keys))

    async def run(self, manifest: PipelineManifest, ctx: StepContext) -> bool:
        # input_path is the immutable source the runner binds before run().
        in_path = cast(str, manifest.get("input_path"))
        await asyncio.to_thread(self._run_sync, in_path, ctx, manifest)
        logger.info(
            "frame_fanout: %d consumer(s) rendered from one decode of %s",
            len(self._consumers),
            in_path,
        )
        return True

    def _run_sync(
        self, in_path: str, ctx: StepContext, manifest: PipelineManifest
    ) -> None:
        import av

        stabilizer = None
        if self.config.fanout_stabilize:
            motion_path = manifest.get("motion_path")
            if not motion_path:
                raise RuntimeError(
                    "frame_fanout: fanout_stabilize=True but the manifest carries "
                    "no 'motion_path'. Pipeline must run a 'stabilize' step "
                    "upstream of the fanout."
                )
            from video_grouper.inference.stabilization import FrameStabilizer

            stabilizer = FrameStabilizer.from_json(motion_path)

        with av.open(in_path) as in_container:
            iv = in_container.streams.video[0]
            if stabilizer is not None:
                sh, sw = stabilizer.output_shape
                source = FrameSourceInfo(
                    sw, sh, iv.average_rate, iv.time_base, stabilized=True
                )
                logger.info(
                    "frame_fanout: stabilization on — consumers see %dx%d "
                    "(raw source %dx%d, safe_inset=%s)",
                    sw,
                    sh,
                    iv.width,
                    iv.height,
                    stabilizer.safe_inset,
                )
            else:
                source = FrameSourceInfo(
                    iv.width, iv.height, iv.average_rate, iv.time_base
                )
            for c in self._consumers:
                c.open(source, ctx, manifest)
            frame_idx = 0
            for packet in in_container.demux(iv):
                if packet.dts is None:
                    continue
                for frame in packet.decode():
                    rgb = frame.to_ndarray(format="rgb24")
                    if stabilizer is not None:
                        rgb = stabilizer.apply(rgb, frame_idx)
                    for c in self._consumers:
                        c.consume(rgb, frame.pts, frame_idx)
                    frame_idx += 1
            for c in self._consumers:
                c.close(manifest)


register_step(FrameFanoutStep.name, FrameFanoutStep, FanoutStepConfig)
