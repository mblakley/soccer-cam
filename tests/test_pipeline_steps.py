"""Unit tests for the five built-in pipeline steps."""

from __future__ import annotations

import json

import pytest

# Importing register_steps registers all built-ins as a side effect. In the dev
# venv the ONNX/cv2/av stack is present, so all five register.
import video_grouper.pipeline.register_steps  # noqa: F401
from video_grouper.pipeline import create_step, get_step_meta, list_steps
from video_grouper.pipeline.base import StepContext
from video_grouper.pipeline.manifest import PipelineManifest
from video_grouper.pipeline.steps.detect import DetectStep
from video_grouper.pipeline.steps.stitch_correct import StitchCorrectStep
from video_grouper.pipeline.steps.track import TrackStep

BUILTINS = {"autocam", "stitch_correct", "detect", "track", "render"}


def _ctx(tmp_path):
    return StepContext(group_dir=tmp_path, team_name=None, storage_path=tmp_path)


def test_register_steps_registers_all_builtins():
    assert BUILTINS <= set(list_steps())


def test_step_metadata():
    detect = get_step_meta("detect")
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

    track = get_step_meta("track")
    assert track.runtime == "service"
    assert track.resources == ()


def test_create_detect_step_validates_config():
    step = create_step("detect", {"model_path": "m.onnx", "detect_confidence": 0.3})
    assert isinstance(step, DetectStep)
    assert step.config.model_path == "m.onnx"
    assert step.config.detect_confidence == 0.3
    assert step.config.device == "cuda:0"  # default
    assert step.config.model_key is None


@pytest.mark.asyncio
async def test_track_step_real_run(tmp_path):
    detections = [
        {"frame_idx": 0, "cx": 100.0, "cy": 100.0, "w": 8, "h": 8, "conf": 0.9},
        {"frame_idx": 1, "cx": 105.0, "cy": 100.0, "w": 8, "h": 8, "conf": 0.9},
        {"frame_idx": 2, "cx": 110.0, "cy": 100.0, "w": 8, "h": 8, "conf": 0.9},
    ]
    det_path = tmp_path / "detections.json"
    det_path.write_text(json.dumps(detections), encoding="utf-8")

    manifest = PipelineManifest.load_or_init(
        tmp_path, str(tmp_path / "game.mp4"), str(tmp_path / "out.mp4")
    )
    manifest.put("detections_path", str(det_path))

    step = create_step("track", {})
    assert isinstance(step, TrackStep)
    ok = await step.run(manifest, _ctx(tmp_path))
    assert ok is True

    traj_path = tmp_path / "trajectory.json"
    assert traj_path.exists()
    traj = json.loads(traj_path.read_text(encoding="utf-8"))
    assert isinstance(traj, list)
    assert len(traj) == 3  # one row per frame 0..2
    assert manifest.get("trajectory_path") == str(traj_path)


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

    def fake_detect_video(video_path, session, frame_interval=1, conf_threshold=0.0):
        captured["frame_interval"] = frame_interval
        captured["conf_threshold"] = conf_threshold
        return [{"frame_idx": 0, "cx": 1.0, "cy": 2.0, "w": 3, "h": 4, "conf": 0.9}]

    monkeypatch.setattr(
        "video_grouper.pipeline.steps.detect.create_session", fake_create_session
    )
    monkeypatch.setattr(
        "video_grouper.pipeline.steps.detect.detect_video", fake_detect_video
    )

    in_path = tmp_path / "game.mp4"
    manifest = PipelineManifest.load_or_init(
        tmp_path, str(in_path), str(tmp_path / "out.mp4")
    )
    step = create_step(
        "detect",
        {"model_path": "m.onnx", "detect_confidence": 0.3, "detect_frame_interval": 4},
    )
    ok = await step.run(manifest, _ctx(tmp_path))
    assert ok is True

    det_path = tmp_path / "detections.json"
    assert det_path.exists()
    data = json.loads(det_path.read_text(encoding="utf-8"))
    assert data[0]["conf"] == 0.9
    assert manifest.get("detections_path") == str(det_path)
    # config threaded through to the detector
    assert captured["conf_threshold"] == 0.3
    assert captured["frame_interval"] == 4


@pytest.mark.asyncio
async def test_detect_step_errors_without_model(tmp_path):
    manifest = PipelineManifest.load_or_init(
        tmp_path, str(tmp_path / "game.mp4"), str(tmp_path / "out.mp4")
    )
    step = create_step("detect", {})  # neither model_key nor model_path
    with pytest.raises(RuntimeError, match="neither model_key nor model_path"):
        await step.run(manifest, _ctx(tmp_path))
