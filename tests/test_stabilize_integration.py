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

import cv2
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


# ---------------------------------------------------------------------------
# End-to-end sign-direction guard — catches "stabilizer amplifies wobble"
# ---------------------------------------------------------------------------


def test_stabilizer_reduces_wobble_not_amplifies(tmp_path):
    """Wire up the full estimate → smooth → apply chain on a synthetic
    wobbling sequence and assert the stabilized output has LESS
    frame-to-frame drift than the raw input.

    Regression guard for the 2026-06-07 sign bug where the residual was
    composed instead of its inverse — the stabilizer was doubling the
    wobble instead of canceling it. The unit tests for each piece passed
    because the test fixtures simulated cum values directly without going
    through the cur→ref convention ``cv2.estimateAffinePartial2D`` returns.
    """
    import math

    from video_grouper.inference.stabilization import (
        FrameStabilizer,
        MotionEstimationConfig,
        SimilarityTransform,
        _ReferenceState,
        compose_stabilizing_transforms,
        compute_safe_inset,
        extract_features,
        l1_smooth_path,
        measure_frame_motion,
        soccer_stability_mask,
        write_motion_json,
    )

    # Build a 30-frame textured sequence where every frame is the same
    # base, translated by a known sinusoidal wobble. The "stabilizer"
    # must invert that exact wobble.
    rng = np.random.default_rng(0)
    h, w = 480, 720
    base = np.full((h, w, 3), 128, dtype=np.uint8)
    for _ in range(500):
        x = int(rng.integers(20, w - 20))
        y = int(rng.integers(20, h - 20))
        r = int(rng.integers(3, 12))
        color = tuple(int(c) for c in rng.integers(0, 256, size=3))
        if rng.random() < 0.5:
            cv2.rectangle(base, (x - r, y - r), (x + r, y + r), color, -1)
        else:
            cv2.circle(base, (x, y), r, color, -1)

    n_frames = 30
    wobble = [
        (
            6.0 * math.sin(2 * math.pi * i / 10),
            4.0 * math.cos(2 * math.pi * i / 10),
        )
        for i in range(n_frames)
    ]
    frames = []
    for dx, dy in wobble:
        M_translate = np.array([[1.0, 0.0, dx], [0.0, 1.0, dy]], dtype=np.float32)
        frames.append(
            cv2.warpAffine(
                base,
                M_translate,
                (w, h),
                flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_REPLICATE,
            )
        )

    # Run the estimation loop just like StabilizeStep does.
    mask = soccer_stability_mask(w, h, polygon=None)
    mys, mxs = np.where(mask > 0)
    roi_y0, roi_y1 = int(mys.min()), int(mys.max()) + 1
    roi_x0, roi_x1 = int(mxs.min()), int(mxs.max()) + 1
    cropped_mask = mask[roi_y0:roi_y1, roi_x0:roi_x1]
    kp_offset = (float(roi_x0), float(roi_y0))

    cfg = MotionEstimationConfig(min_inliers=8, min_inlier_ratio=0.15)
    reference = _ReferenceState()
    cum_tx, cum_ty, cum_theta, cum_log_scale = [], [], [], []
    confidences = []
    for i, rgb in enumerate(frames):
        cropped = rgb[roi_y0:roi_y1, roi_x0:roi_x1]
        motion, reanchor = measure_frame_motion(
            cropped, cropped_mask, reference, cfg, keypoint_offset=kp_offset
        )
        cum_tx.append(motion.cum_tx)
        cum_ty.append(motion.cum_ty)
        cum_theta.append(motion.cum_theta)
        cum_log_scale.append(motion.cum_log_scale)
        confidences.append(motion.confidence)
        if reference.descriptors is None or reanchor:
            kp, desc = extract_features(
                cropped,
                cropped_mask,
                n_features=cfg.n_features,
                keypoint_offset=kp_offset,
            )
            if desc is not None and len(desc) >= cfg.min_inliers:
                reference.keypoints = kp
                reference.descriptors = desc
                reference.cumulative = SimilarityTransform(
                    tx=motion.cum_tx,
                    ty=motion.cum_ty,
                    theta=motion.cum_theta,
                    log_scale=motion.cum_log_scale,
                )
                reference.frame_idx = i

    cum_tx = np.array(cum_tx)
    cum_ty = np.array(cum_ty)
    cum_theta = np.array(cum_theta)
    cum_log_scale = np.array(cum_log_scale)

    smooth_tx = l1_smooth_path(cum_tx, budget=20.0)
    smooth_ty = l1_smooth_path(cum_ty, budget=20.0)
    smooth_theta = l1_smooth_path(cum_theta, budget=math.radians(1.0))
    smooth_log_scale = l1_smooth_path(cum_log_scale, budget=0.01)

    inset_y, inset_x = compute_safe_inset(20.0, 20.0, 1.0, 0.01, w, h)
    mats = compose_stabilizing_transforms(
        cum_tx,
        cum_ty,
        cum_theta,
        cum_log_scale,
        smooth_tx,
        smooth_ty,
        smooth_theta,
        smooth_log_scale,
        inset_x=inset_x,
        inset_y=inset_y,
    )
    out_h, out_w = h - 2 * inset_y, w - 2 * inset_x
    json_path = tmp_path / "motion.json"
    write_motion_json(
        json_path,
        src_size=(h, w),
        output_size=(out_h, out_w),
        safe_inset=(inset_y, inset_x),
        transforms=mats,
        confidences=confidences,
    )
    stabilizer = FrameStabilizer.from_json(json_path)

    # Measure adjacent-frame drift on raw vs stabilized using phase
    # correlation on a fixed background ROI.
    def gray(arr):
        return cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY).astype(np.float32)

    raw_drift_y = []
    stab_drift_y = []
    prev_raw = None
    prev_stab = None
    for i, rgb in enumerate(frames):
        # ROI in raw vs stabilized frames (different shapes; use a relative
        # central band that exists in both).
        raw_roi = gray(rgb[20 : h - 20, 50 : w - 50])
        stab_rgb = stabilizer.apply(rgb, i)
        stab_roi = gray(stab_rgb[10 : out_h - 10, 30 : out_w - 30])
        if prev_raw is not None:
            (_, dy), _ = cv2.phaseCorrelate(prev_raw, raw_roi)
            raw_drift_y.append(abs(dy))
            (_, dy), _ = cv2.phaseCorrelate(prev_stab, stab_roi)
            stab_drift_y.append(abs(dy))
        prev_raw = raw_roi
        prev_stab = stab_roi

    raw_mean = float(np.mean(raw_drift_y))
    stab_mean = float(np.mean(stab_drift_y))
    # Stabilized adjacent-frame drift should be SMALLER than raw.
    # Without the inverse fix the bottom would be 2-10x WORSE (the
    # regression we're guarding against).
    assert stab_mean < raw_mean, (
        f"stabilizer is AMPLIFYING wobble: raw={raw_mean:.3f} stab={stab_mean:.3f}. "
        f"The sign on the residual transform is likely flipped."
    )
    # And by a substantive margin — at least 30% reduction on this clean
    # synthetic input.
    assert stab_mean < raw_mean * 0.7, (
        f"stabilization too weak: raw={raw_mean:.3f} stab={stab_mean:.3f}"
    )
