"""Unit tests for the built-in pipeline steps (registration + wiring)."""

from __future__ import annotations

import json

import pytest

# Importing register_steps registers all built-ins as a side effect. In the dev
# venv the ONNX/cv2/av stack is present, so all of them register.
import video_grouper.pipeline.register_steps  # noqa: F401
from video_grouper.pipeline import create_step, get_step_meta, list_steps
from video_grouper.pipeline.base import StepContext
from video_grouper.pipeline.manifest import PipelineManifest
from video_grouper.pipeline.steps.ball_detect import BallDetectStep
from video_grouper.pipeline.steps.stitch_correct import StitchCorrectStep

BUILTINS = {
    "autocam",
    "stitch_correct",
    "field_detect",
    "ball_detect",
    "ball_select",
    "plan_camera",
    "render",
}

POLY = [
    [100.0, 1000.0],
    [500.0, 1010.0],
    [960.0, 1015.0],
    [1420.0, 1010.0],
    [1820.0, 1000.0],
    [1600.0, 300.0],
    [1280.0, 295.0],
    [960.0, 290.0],
    [640.0, 295.0],
    [320.0, 300.0],
]


def _ctx(tmp_path):
    return StepContext(group_dir=tmp_path, team_name=None, storage_path=tmp_path)


def test_register_steps_registers_all_builtins():
    assert BUILTINS <= set(list_steps())
    assert "track" not in list_steps()  # replaced by ball_select


def test_step_metadata():
    detect = get_step_meta("ball_detect")
    assert detect.runtime == "service"
    assert detect.resources == ("gpu",)
    assert detect.requires == ("onnxruntime", "cv2")
    assert detect.available is True  # deps present in dev venv

    autocam = get_step_meta("autocam")
    assert autocam.runtime == "tray"
    assert autocam.resources == ("autocam_ui",)
    assert autocam.requires == ()

    render = get_step_meta("render")
    # The cylindrical broadcast render uses cv2.remap (de-fisheye) in addition
    # to av, so it requires both.
    assert render.requires == ("av", "cv2")
    assert render.resources == ("ram_heavy",)

    select = get_step_meta("ball_select")
    assert select.runtime == "service"
    assert select.resources == ()


def test_create_detect_step_validates_config():
    step = create_step(
        "ball_detect", {"model_path": "m.onnx", "detect_confidence": 0.3}
    )
    assert isinstance(step, BallDetectStep)
    assert step.config.model_path == "m.onnx"
    assert step.config.detect_confidence == 0.3
    assert step.config.device == "cuda:0"  # default
    assert step.config.model_key is None


@pytest.mark.asyncio
async def test_stitch_passthrough_when_no_profile(tmp_path):
    manifest = PipelineManifest.load_or_init(
        tmp_path, str(tmp_path / "game.mp4"), str(tmp_path / "out.mp4")
    )
    step = create_step("stitch_correct", {})  # no stitch_profile_path
    assert isinstance(step, StitchCorrectStep)
    ok = await step.run(manifest, _ctx(tmp_path))
    assert ok is True
    assert manifest.get("stitched_path") is None
    # input_path unchanged on pass-through
    assert manifest.get("input_path") == str(tmp_path / "game.mp4")


@pytest.mark.asyncio
async def test_detect_step_local_path(tmp_path, monkeypatch):
    captured = {}

    def fake_create_session(model_path, use_gpu=False):
        captured["model_path"] = str(model_path)
        captured["use_gpu"] = use_gpu
        return object()

    def fake_detect_video_candidates(video_path, session, polygon, **kw):
        captured["polygon_len"] = len(polygon)
        captured["stride"] = kw["stride"]
        captured["threshold"] = kw["threshold"]
        return {0: [(1.0, 2.0, 0.9)]}, {
            "src_w": 1920,
            "src_h": 1080,
            "fps": 20.0,
            "n_frames": 1,
        }

    monkeypatch.setattr(
        "video_grouper.pipeline.steps.ball_detect.create_session", fake_create_session
    )
    monkeypatch.setattr(
        "video_grouper.pipeline.steps.ball_detect.detect_video_candidates",
        fake_detect_video_candidates,
    )

    in_path = tmp_path / "game.mp4"
    manifest = PipelineManifest.load_or_init(
        tmp_path, str(in_path), str(tmp_path / "out.mp4")
    )
    poly_path = tmp_path / "field.json"
    poly_path.write_text(json.dumps({"polygon": POLY}), encoding="utf-8")
    manifest.put("field_polygon_path", str(poly_path))
    step = create_step(
        "ball_detect",
        {"model_path": "m.onnx", "detect_confidence": 0.3, "detect_frame_interval": 4},
    )
    ok = await step.run(manifest, _ctx(tmp_path))
    assert ok is True

    det_path = tmp_path / "detections.json"
    assert det_path.exists()
    art = json.loads(det_path.read_text(encoding="utf-8"))
    assert art["schema"] == "candidates/1"
    assert art["frames"]["0"][0][2] == 0.9
    assert manifest.get("detections_path") == str(det_path)
    # config threaded through to the detector
    assert captured["threshold"] == 0.3
    assert captured["stride"] == 4
    assert captured["polygon_len"] == 10


@pytest.mark.asyncio
async def test_detect_step_requires_field_polygon(tmp_path):
    manifest = PipelineManifest.load_or_init(
        tmp_path, str(tmp_path / "game.mp4"), str(tmp_path / "out.mp4")
    )
    step = create_step("ball_detect", {"model_path": "m.onnx"})
    with pytest.raises(RuntimeError, match="field polygon"):
        await step.run(manifest, _ctx(tmp_path))


@pytest.mark.asyncio
async def test_detect_step_errors_without_model(tmp_path):
    manifest = PipelineManifest.load_or_init(
        tmp_path, str(tmp_path / "game.mp4"), str(tmp_path / "out.mp4")
    )
    poly_path = tmp_path / "field.json"
    poly_path.write_text(json.dumps({"polygon": POLY}), encoding="utf-8")
    manifest = PipelineManifest.load_or_init(
        tmp_path, str(tmp_path / "game.mp4"), str(tmp_path / "out.mp4")
    )
    manifest.put("field_polygon_path", str(poly_path))
    step = create_step("ball_detect", {})  # neither model_key nor model_path
    with pytest.raises(RuntimeError, match="neither model_key nor model_path"):
        await step.run(manifest, _ctx(tmp_path))
