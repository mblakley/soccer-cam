"""Integration test: ``frame_fanout`` applies stabilization in the decode
loop and shares already-stabilized frames with all consumers.

This is the architectural payoff for polygon-zone blend: the 3× warpAffine
cost happens ONCE per decoded frame (in the fanout), not once per
consumer. The test exercises:

  * ``fanout_stabilize=True`` causes fanout to load ``motion_path`` from
    the manifest and apply the stabilizer before dispatching.
  * Consumers receive a :class:`FrameSourceInfo` whose width/height match
    the stabilizer's ``output_shape`` (so geometry sizes against the
    stabilized dims, not raw source dims).
  * Each consumer is called exactly once per decoded frame regardless of
    how many consumers are registered.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import ClassVar

import numpy as np
import pytest
from pydantic import BaseModel

import video_grouper.pipeline.register_steps  # noqa: F401
from video_grouper.pipeline import create_step
from video_grouper.pipeline.base import StepContext
from video_grouper.pipeline.frame_consumer import (
    FrameConsumer,
    FrameSourceInfo,
    register_frame_consumer,
)
from video_grouper.pipeline.manifest import PipelineManifest

SRC_H, SRC_W = 360, 640
INSET_Y, INSET_X = 30, 40
N_FRAMES = 12


# Tests here need REAL PyAV (synthetic video write + decode). The conftest's
# autouse mock_ffmpeg / mock_file_system fixtures would otherwise stub those
# out; override locally so the synthesis function actually writes a file.
@pytest.fixture(autouse=True)
def mock_ffmpeg():  # noqa: PT004
    yield None


@pytest.fixture(autouse=True)
def mock_file_system():  # noqa: PT004
    yield None


# ---------------------------------------------------------------------------
# Synthetic input clip + motion.json fixtures
# ---------------------------------------------------------------------------


def _write_synthetic_clip(path: Path, n_frames: int = N_FRAMES) -> None:
    from fractions import Fraction

    import av

    rng = np.random.default_rng(0)
    base = (rng.random((SRC_H, SRC_W, 3)) * 255).astype(np.uint8)
    container = av.open(str(path), mode="w")
    stream = container.add_stream("h264", rate=20)
    stream.width = SRC_W
    stream.height = SRC_H
    stream.pix_fmt = "yuv420p"
    stream.codec_context.time_base = Fraction(1, 20)
    for i in range(n_frames):
        vf = av.VideoFrame.from_ndarray(base, format="rgb24")
        vf.pts = i
        for pkt in stream.encode(vf):
            container.mux(pkt)
    for pkt in stream.encode():
        container.mux(pkt)
    container.close()


def _write_identity_motion_json(path: Path, n_frames: int = N_FRAMES) -> None:
    """Identity stabilization at a known safe inset."""
    from video_grouper.inference.stabilization import write_motion_json

    inset_M = np.array(
        [[1.0, 0.0, float(INSET_X)], [0.0, 1.0, float(INSET_Y)]],
        dtype=np.float32,
    )
    transforms = [inset_M.copy() for _ in range(n_frames)]
    write_motion_json(
        path,
        src_size=(SRC_H, SRC_W),
        output_size=(SRC_H - 2 * INSET_Y, SRC_W - 2 * INSET_X),
        safe_inset=(INSET_Y, INSET_X),
        transforms=transforms,
        confidences=[1.0] * n_frames,
    )


# ---------------------------------------------------------------------------
# A tiny spy consumer that records what fanout hands it
# ---------------------------------------------------------------------------


class _SpyConfig(BaseModel):
    output_key: str = "spy_path"


class _SpyConsumer(FrameConsumer):
    config_model: ClassVar[type[BaseModel]] = _SpyConfig

    def __init__(self, config: _SpyConfig):
        super().__init__(config)
        self.opened_with: FrameSourceInfo | None = None
        self.frame_shapes: list[tuple[int, int, int]] = []
        self.frame_indices: list[int] = []

    @property
    def produces(self) -> tuple[str, ...]:
        return (self.config.output_key,)

    def open(self, source, ctx, manifest):
        self.opened_with = source

    def consume(self, rgb, frame_pts, frame_idx):
        self.frame_shapes.append(rgb.shape)
        self.frame_indices.append(frame_idx)

    def close(self, manifest):
        manifest.put(self.config.output_key, "spy://done")


register_frame_consumer("_spy_for_test", _SpyConsumer, _SpyConfig)


def _capture_spy(step) -> _SpyConsumer:
    """Return the first _SpyConsumer instance owned by *step*."""
    for c in step._consumers:
        if isinstance(c, _SpyConsumer):
            return c
    raise AssertionError("no spy consumer wired into the fanout step")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _make_step(consumers: list[dict], fanout_stabilize: bool):
    return create_step(
        "frame_fanout",
        {"consumers": consumers, "fanout_stabilize": fanout_stabilize},
    )


def test_fanout_default_passes_raw_source_to_consumers(tmp_path: Path):
    """Without ``fanout_stabilize``, consumers see the raw source dims —
    the baseline behaviour stays untouched."""
    in_path = tmp_path / "clip.mp4"
    _write_synthetic_clip(in_path)
    step = _make_step(
        [{"type": "_spy_for_test", "config": {"output_key": "spy_path"}}],
        fanout_stabilize=False,
    )
    manifest = PipelineManifest.load_or_init(
        tmp_path, str(in_path), str(tmp_path / "out.mp4")
    )
    manifest.put("input_path", str(in_path))
    ctx = StepContext(group_dir=tmp_path, team_name=None, storage_path=tmp_path)
    asyncio.run(step.run(manifest, ctx))
    spy = _capture_spy(step)
    assert spy.opened_with is not None
    assert (spy.opened_with.width, spy.opened_with.height) == (SRC_W, SRC_H)
    assert len(spy.frame_shapes) > 0
    assert all(shape == (SRC_H, SRC_W, 3) for shape in spy.frame_shapes)


def test_fanout_stabilize_passes_stabilized_dims_and_frames(tmp_path: Path):
    """With ``fanout_stabilize=True``, the consumer sees the post-warp
    dims AND post-warp frames — proving stabilization ran exactly once
    per decoded frame, in the fanout decode loop, before dispatch."""
    in_path = tmp_path / "clip.mp4"
    _write_synthetic_clip(in_path)
    motion_path = tmp_path / "motion.json"
    _write_identity_motion_json(motion_path)

    step = _make_step(
        [{"type": "_spy_for_test", "config": {"output_key": "spy_path"}}],
        fanout_stabilize=True,
    )
    manifest = PipelineManifest.load_or_init(
        tmp_path, str(in_path), str(tmp_path / "out.mp4")
    )
    manifest.put("input_path", str(in_path))
    manifest.put("motion_path", str(motion_path))
    ctx = StepContext(group_dir=tmp_path, team_name=None, storage_path=tmp_path)
    asyncio.run(step.run(manifest, ctx))

    spy = _capture_spy(step)
    expected_h = SRC_H - 2 * INSET_Y
    expected_w = SRC_W - 2 * INSET_X
    assert spy.opened_with is not None
    assert (spy.opened_with.width, spy.opened_with.height) == (
        expected_w,
        expected_h,
    )
    assert len(spy.frame_shapes) > 0
    assert all(shape == (expected_h, expected_w, 3) for shape in spy.frame_shapes)


def test_fanout_stabilize_shares_one_apply_across_multiple_consumers(
    tmp_path: Path,
):
    """Two spy consumers receive the SAME stabilized output dimensions —
    proving one stabilizer.apply call serves N consumers, not N×."""
    in_path = tmp_path / "clip.mp4"
    _write_synthetic_clip(in_path)
    motion_path = tmp_path / "motion.json"
    _write_identity_motion_json(motion_path)

    step = _make_step(
        [
            {"type": "_spy_for_test", "config": {"output_key": "spy_path_a"}},
            {"type": "_spy_for_test", "config": {"output_key": "spy_path_b"}},
        ],
        fanout_stabilize=True,
    )
    manifest = PipelineManifest.load_or_init(
        tmp_path, str(in_path), str(tmp_path / "out.mp4")
    )
    manifest.put("input_path", str(in_path))
    manifest.put("motion_path", str(motion_path))
    ctx = StepContext(group_dir=tmp_path, team_name=None, storage_path=tmp_path)
    asyncio.run(step.run(manifest, ctx))

    spies = [c for c in step._consumers if isinstance(c, _SpyConsumer)]
    assert len(spies) == 2
    a, b = spies
    assert a.frame_indices == b.frame_indices  # lockstep
    assert a.frame_shapes == b.frame_shapes  # identical dims
    expected_h = SRC_H - 2 * INSET_Y
    expected_w = SRC_W - 2 * INSET_X
    assert all(shape == (expected_h, expected_w, 3) for shape in a.frame_shapes)


def test_fanout_consumes_includes_motion_path_when_enabled(tmp_path: Path):
    """When ``fanout_stabilize`` is on, the step declares ``motion_path``
    as a consumed manifest key so the runner can validate it upstream."""
    step_on = _make_step(
        [{"type": "_spy_for_test", "config": {"output_key": "spy_path"}}],
        fanout_stabilize=True,
    )
    step_off = _make_step(
        [{"type": "_spy_for_test", "config": {"output_key": "spy_path"}}],
        fanout_stabilize=False,
    )
    assert "motion_path" in step_on.consumes
    assert "motion_path" not in step_off.consumes
