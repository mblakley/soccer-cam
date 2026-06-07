"""Integration tests for the inline stabilization application in detect + render.

The pure-helper layer is covered by ``test_stabilization.py``; the standalone
``stabilize`` step's manifest contract + end-to-end pass is in
``test_pipeline_steps_stabilize.py``. These tests focus narrowly on the
inline-application surface: that detect and render correctly construct a
:class:`~video_grouper.inference.stabilization.FrameStabilizer` from the
manifest's ``motion_path`` when the flag is on, and don't when it isn't.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

import video_grouper.pipeline.register_steps  # noqa: F401
from video_grouper.pipeline import create_step
from video_grouper.pipeline.base import StepContext
from video_grouper.pipeline.manifest import PipelineManifest


def _ctx(tmp_path: Path) -> StepContext:
    return StepContext(group_dir=tmp_path, team_name=None, storage_path=tmp_path)


def _write_minimal_motion_json(path: Path, n_frames: int = 50) -> None:
    """Write a tiny valid motion.json (identity transforms) at *path*."""
    from video_grouper.inference.stabilization import write_motion_json

    transforms = [
        np.array([[1.0, 0.0, 20.0], [0.0, 1.0, 30.0]], dtype=np.float32)
        for _ in range(n_frames)
    ]
    confidences = [1.0] * n_frames
    write_motion_json(
        path,
        src_size=(2160, 7680),
        output_size=(2100, 7640),
        safe_inset=(30, 20),
        transforms=transforms,
        confidences=confidences,
    )


# ---------------------------------------------------------------------------
# DetectStep: stabilizer wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_constructs_stabilizer_when_flag_on(tmp_path, monkeypatch):
    """When ``detect_stabilize`` is True AND ``motion_path`` is in the
    manifest, ``detect_video`` receives a FrameStabilizer instance."""
    captured: dict = {}

    def fake_create_session(model_path, use_gpu=False):
        return object()

    def fake_detect_video(
        video_path, session, frame_interval=1, conf_threshold=0.0, stabilizer=None
    ):
        captured["stabilizer"] = stabilizer
        return []

    monkeypatch.setattr(
        "video_grouper.pipeline.steps.detect.create_session", fake_create_session
    )
    monkeypatch.setattr(
        "video_grouper.pipeline.steps.detect.detect_video", fake_detect_video
    )

    motion_path = tmp_path / "motion.json"
    _write_minimal_motion_json(motion_path)

    manifest = PipelineManifest.load_or_init(
        tmp_path, str(tmp_path / "game.mp4"), str(tmp_path / "out.mp4")
    )
    manifest.put("motion_path", str(motion_path))

    step = create_step(
        "detect",
        {"model_path": "m.onnx", "detect_stabilize": True},
    )
    await step.run(manifest, _ctx(tmp_path))
    from video_grouper.inference.stabilization import FrameStabilizer

    assert isinstance(captured.get("stabilizer"), FrameStabilizer)
    assert captured["stabilizer"].output_shape == (2100, 7640)


@pytest.mark.asyncio
async def test_detect_no_stabilizer_when_flag_off(tmp_path, monkeypatch):
    """``detect_stabilize=False`` → no stabilizer constructed even when
    ``motion_path`` is present."""
    captured: dict = {}

    def fake_create_session(model_path, use_gpu=False):
        return object()

    def fake_detect_video(
        video_path, session, frame_interval=1, conf_threshold=0.0, stabilizer=None
    ):
        captured["stabilizer"] = stabilizer
        return []

    monkeypatch.setattr(
        "video_grouper.pipeline.steps.detect.create_session", fake_create_session
    )
    monkeypatch.setattr(
        "video_grouper.pipeline.steps.detect.detect_video", fake_detect_video
    )

    motion_path = tmp_path / "motion.json"
    _write_minimal_motion_json(motion_path)

    manifest = PipelineManifest.load_or_init(
        tmp_path, str(tmp_path / "game.mp4"), str(tmp_path / "out.mp4")
    )
    manifest.put("motion_path", str(motion_path))

    step = create_step(
        "detect", {"model_path": "m.onnx"}
    )  # detect_stabilize defaults False
    await step.run(manifest, _ctx(tmp_path))
    assert captured.get("stabilizer") is None


@pytest.mark.asyncio
async def test_detect_no_stabilizer_when_motion_path_missing(tmp_path, monkeypatch):
    """``detect_stabilize=True`` without ``motion_path`` is silently a no-op."""
    captured: dict = {}

    def fake_create_session(model_path, use_gpu=False):
        return object()

    def fake_detect_video(
        video_path, session, frame_interval=1, conf_threshold=0.0, stabilizer=None
    ):
        captured["stabilizer"] = stabilizer
        return []

    monkeypatch.setattr(
        "video_grouper.pipeline.steps.detect.create_session", fake_create_session
    )
    monkeypatch.setattr(
        "video_grouper.pipeline.steps.detect.detect_video", fake_detect_video
    )

    manifest = PipelineManifest.load_or_init(
        tmp_path, str(tmp_path / "game.mp4"), str(tmp_path / "out.mp4")
    )
    # No motion_path put.

    step = create_step("detect", {"model_path": "m.onnx", "detect_stabilize": True})
    await step.run(manifest, _ctx(tmp_path))
    assert captured.get("stabilizer") is None


# ---------------------------------------------------------------------------
# ball_detector.detect_video: stabilizer applies to each decoded frame
# ---------------------------------------------------------------------------


def test_detect_video_applies_stabilizer_per_frame(tmp_path, monkeypatch):
    """detect_video should call stabilizer.apply on every Nth decoded frame
    before passing to detect_balls."""
    from video_grouper.inference import ball_detector

    fake_frame = np.zeros((480, 640, 3), dtype=np.uint8)

    class FakeCap:
        def __init__(self):
            self._n = 0

        def isOpened(self):  # noqa: N802
            return True

        def get(self, prop):
            return 4  # total frames

        def read(self):
            self._n += 1
            return (True, fake_frame.copy()) if self._n <= 4 else (False, None)

        def release(self):
            pass

    monkeypatch.setattr(ball_detector.cv2, "VideoCapture", lambda *_a, **_kw: FakeCap())
    monkeypatch.setattr(ball_detector, "detect_balls", lambda *a, **kw: [])

    stabilizer = MagicMock()
    stabilizer.apply.return_value = fake_frame
    stabilizer.output_shape = (480, 640)
    stabilizer.safe_inset = (0, 0)

    ball_detector.detect_video(
        video_path=Path("/dummy.mp4"),
        sess=MagicMock(),
        frame_interval=2,  # every other frame
        stabilizer=stabilizer,
    )
    # 4 frames total, every 2nd = frames 0 and 2 — 2 calls to apply.
    assert stabilizer.apply.call_count == 2


# ---------------------------------------------------------------------------
# RenderStep: stabilizer wiring via _render_video signature
# ---------------------------------------------------------------------------


def test_render_video_accepts_motion_path_arg():
    """The _render_video sync helper must accept motion_path so the
    RenderStep can hand the manifest entry through to it."""
    import inspect

    from video_grouper.pipeline.steps.render import _render_video

    sig = inspect.signature(_render_video)
    assert "motion_path" in sig.parameters


def test_render_step_config_has_stabilize_flag():
    """``render_stabilize: bool = False`` so existing pipelines are
    unchanged."""
    from video_grouper.pipeline.steps.render import RenderStepConfig

    cfg = RenderStepConfig()
    assert cfg.render_stabilize is False


def test_detect_step_config_has_stabilize_flag():
    """``detect_stabilize: bool = False`` so existing pipelines are
    unchanged."""
    from video_grouper.pipeline.steps.detect import DetectStepConfig

    cfg = DetectStepConfig()
    assert cfg.detect_stabilize is False
