"""Unit tests for the field_detect pipeline step."""

from __future__ import annotations

import json

import pytest

# Importing register_steps registers all built-ins as a side effect.
import video_grouper.pipeline.register_steps  # noqa: F401
from video_grouper.pipeline import create_step, get_step_meta
from video_grouper.pipeline.base import StepContext
from video_grouper.pipeline.manifest import PipelineManifest
from video_grouper.pipeline.steps.field_detect import (
    FieldDetectStep,
    _sample_times,
)


def _ctx(tmp_path):
    return StepContext(group_dir=tmp_path, team_name=None, storage_path=tmp_path)


def _manifest(tmp_path):
    return PipelineManifest.load_or_init(
        tmp_path, str(tmp_path / "game.mp4"), str(tmp_path / "out.mp4")
    )


def test_field_detect_registered_with_expected_metadata():
    meta = get_step_meta("field_detect")
    assert meta.runtime == "service"
    assert meta.resources == ("gpu",)
    assert meta.requires == ("onnxruntime", "cv2", "av")


def test_create_field_detect_step_validates_config():
    step = create_step(
        "field_detect", {"model_path": "f.onnx", "field_sample_frames": 3}
    )
    assert isinstance(step, FieldDetectStep)
    assert step.config.model_path == "f.onnx"
    assert step.config.field_sample_frames == 3
    assert step.config.model_key is None
    assert step.config.field_score_threshold == 0.5  # default
    assert step.config.field_min_keypoints == 6  # default


def test_sample_times_spread_across_middle():
    times = _sample_times(100.0, 5)
    assert len(times) == 5
    assert times[0] == pytest.approx(10.0)  # 10% in
    assert times[-1] == pytest.approx(90.0)  # 90% in
    assert times == sorted(times)


def test_sample_times_degenerate_durations():
    # Unknown / zero duration falls back to a single midpoint sample.
    assert _sample_times(0.0, 5) == [0.0]
    assert _sample_times(60.0, 1) == [30.0]


@pytest.mark.asyncio
async def test_field_detect_full_frame_without_model(tmp_path, monkeypatch):
    """No model_key/model_path: the step still produces a polygon — the
    neutral full-frame rectangle (downstream steps require the artifact)."""
    monkeypatch.setattr(
        "video_grouper.pipeline.steps.field_detect._video_dims",
        lambda path: (5120, 1440),
    )
    manifest = _manifest(tmp_path)
    step = create_step("field_detect", {})
    ok = await step.run(manifest, _ctx(tmp_path))
    assert ok is True

    polygon_path = tmp_path / "field_polygon.json"
    assert manifest.get("field_polygon_path") == str(polygon_path)
    payload = json.loads(polygon_path.read_text(encoding="utf-8"))
    assert payload["source"] == "full_frame"
    assert payload["polygon"] == [
        [0.0, 0.0],
        [5120.0, 0.0],
        [5120.0, 1440.0],
        [0.0, 1440.0],
    ]


@pytest.mark.asyncio
async def test_field_detect_writes_polygon_and_manifest(tmp_path, monkeypatch):
    """With a model configured and a polygon found, the step writes
    field_polygon.json and records field_polygon_path — the artifact key the
    track and render steps read."""
    captured = {}
    polygon = [[float(i * 100), float(100 + (i % 5))] for i in range(10)]

    def fake_create_session(model_path, use_gpu=False):
        captured["model_path"] = str(model_path)
        captured["use_gpu"] = use_gpu
        return object()

    def fake_detect_polygon(
        video_path, session, score_threshold, min_keypoints, sample_frames
    ):
        captured["video_path"] = video_path
        captured["score_threshold"] = score_threshold
        captured["min_keypoints"] = min_keypoints
        captured["sample_frames"] = sample_frames
        return polygon, 0.91

    monkeypatch.setattr(
        "video_grouper.pipeline.steps.field_detect.create_field_session",
        fake_create_session,
    )
    monkeypatch.setattr(
        "video_grouper.pipeline.steps.field_detect._detect_field_polygon",
        fake_detect_polygon,
    )

    manifest = _manifest(tmp_path)
    step = create_step(
        "field_detect",
        {"model_path": "f.onnx", "device": "cpu", "field_sample_frames": 3},
    )
    ok = await step.run(manifest, _ctx(tmp_path))
    assert ok is True

    polygon_path = tmp_path / "field_polygon.json"
    assert manifest.get("field_polygon_path") == str(polygon_path)
    payload = json.loads(polygon_path.read_text(encoding="utf-8"))
    assert payload["polygon"] == polygon
    assert payload["mean_score"] == 0.91
    assert payload["source"] == "model_path"
    # config threaded through
    assert captured["use_gpu"] is False
    assert captured["sample_frames"] == 3
    # The render/track loaders read payload["polygon"] — same shape they parse.
    assert len(payload["polygon"]) == 10


@pytest.mark.asyncio
async def test_field_detect_no_polygon_found_falls_back_to_full_frame(
    tmp_path, monkeypatch
):
    """A configured model that finds no usable polygon must not fail the
    pipeline — the step falls back to the neutral full-frame polygon."""
    monkeypatch.setattr(
        "video_grouper.pipeline.steps.field_detect.create_field_session",
        lambda *a, **k: object(),
    )
    monkeypatch.setattr(
        "video_grouper.pipeline.steps.field_detect._detect_field_polygon",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "video_grouper.pipeline.steps.field_detect._video_dims",
        lambda path: (1920, 1080),
    )

    manifest = _manifest(tmp_path)
    step = create_step("field_detect", {"model_path": "f.onnx"})
    ok = await step.run(manifest, _ctx(tmp_path))
    assert ok is True

    payload = json.loads((tmp_path / "field_polygon.json").read_text(encoding="utf-8"))
    assert payload["source"] == "full_frame"
    assert payload["mean_score"] == 0.0
    assert len(payload["polygon"]) == 4


def test_render_defaults_to_full_frame_polygon():
    """Render's polygon requirement: absent a real polygon it synthesizes the
    full-frame rectangle (one geometry code path either way)."""
    import numpy as np

    from video_grouper.pipeline.steps.render import _polygon_or_full_frame

    full = _polygon_or_full_frame(None, 5120, 1440)
    assert full.dtype == np.float32
    assert full.tolist() == [
        [0.0, 0.0],
        [5120.0, 0.0],
        [5120.0, 1440.0],
        [0.0, 1440.0],
    ]
    # A real polygon passes through untouched.
    real = np.array([[1, 2], [3, 4], [5, 6], [7, 8]], dtype=np.float32)
    assert _polygon_or_full_frame(real, 5120, 1440) is real


@pytest.mark.asyncio
async def test_field_detect_model_key_requires_ttt_config(tmp_path):
    """model_key without TTT config must raise (same contract as detect)."""
    manifest = _manifest(tmp_path)
    step = create_step("field_detect", {"model_key": "some-key"})
    with pytest.raises(RuntimeError, match="TTT integration is disabled"):
        await step.run(manifest, _ctx(tmp_path))  # ctx has ttt_config=None
