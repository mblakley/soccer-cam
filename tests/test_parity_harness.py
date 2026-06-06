"""iOS-port parity-harness wiring tests.

Validates the determinism plumbing and the ``dump_intermediates_dir`` side-
effects without needing a real model + video on disk (those run in W.4 against
real Reolink panoramas to produce the checked-in baselines).

Covers:
- ``StepContext`` carries the dump dir
- ``determinism`` helpers produce expected ONNX options and seed behavior
- ``detect`` step copies detections.json into the dump dir when set
- ``track`` step copies trajectory.json into the dump dir when set
- ``detect`` step requests a deterministic ONNX session when dump dir is set
- ``track`` step emits JSON with ``sort_keys=True`` (byte-identical re-runs)
"""

from __future__ import annotations

import json
import os

import pytest

import video_grouper.pipeline.register_steps  # noqa: F401
from video_grouper.inference.determinism import (
    PARITY_PROVIDERS,
    make_deterministic_sess_options,
    seed_everything,
)
from video_grouper.pipeline import create_step
from video_grouper.pipeline.base import StepContext
from video_grouper.pipeline.manifest import PipelineManifest


def _ctx(tmp_path, *, dump: bool = False):
    dump_dir = (tmp_path / "parity") if dump else None
    return StepContext(
        group_dir=tmp_path,
        team_name=None,
        storage_path=tmp_path,
        dump_intermediates_dir=dump_dir,
    )


# ----------------------------------------------------------------------
# Determinism helpers
# ----------------------------------------------------------------------


def test_step_context_accepts_dump_intermediates_dir(tmp_path):
    ctx = StepContext(
        group_dir=tmp_path,
        team_name=None,
        storage_path=tmp_path,
        dump_intermediates_dir=tmp_path / "parity",
    )
    assert ctx.dump_intermediates_dir == tmp_path / "parity"


def test_step_context_dump_dir_defaults_to_none(tmp_path):
    ctx = StepContext(group_dir=tmp_path, team_name=None, storage_path=tmp_path)
    assert ctx.dump_intermediates_dir is None


def test_parity_providers_is_cpu_only():
    # The whole determinism story hinges on CPU EP — assert nothing else slips in.
    assert PARITY_PROVIDERS == ("CPUExecutionProvider",)


def test_make_deterministic_sess_options_returns_expected_settings():
    import onnxruntime as ort

    opts = make_deterministic_sess_options()
    assert opts.execution_mode == ort.ExecutionMode.ORT_SEQUENTIAL
    assert opts.intra_op_num_threads == 1
    assert opts.inter_op_num_threads == 1
    assert opts.enable_cpu_mem_arena is False
    assert opts.enable_mem_pattern is False


def test_seed_everything_sets_hashseed_env():
    seed_everything(42)
    assert os.environ.get("PYTHONHASHSEED") == "42"


# ----------------------------------------------------------------------
# detect step — dump-intermediates + deterministic kwarg wiring
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_dumps_detections_when_dump_dir_set(tmp_path, monkeypatch):
    captured = {}

    def fake_create_session(model_path, use_gpu=False, deterministic=False):
        captured["deterministic"] = deterministic
        captured["use_gpu"] = use_gpu
        return object()

    def fake_detect_video(video_path, session, frame_interval=1, conf_threshold=0.0):
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
    step = create_step("detect", {"model_path": "m.onnx"})
    ok = await step.run(manifest, _ctx(tmp_path, dump=True))
    assert ok is True

    # Production path: detections next to the input
    assert (tmp_path / "detections.json").exists()
    # Parity dump: the same file lives in the dump dir for the iOS port to consume
    dump_path = tmp_path / "parity" / "detections.json"
    assert dump_path.exists()
    assert json.loads(dump_path.read_text(encoding="utf-8"))[0]["conf"] == 0.9

    # dump-intermediates ⇒ deterministic CPU session, never GPU
    assert captured["deterministic"] is True
    assert captured["use_gpu"] is False


@pytest.mark.asyncio
async def test_detect_uses_gpu_when_no_dump_dir(tmp_path, monkeypatch):
    captured = {}

    def fake_create_session(model_path, use_gpu=False, deterministic=False):
        captured["deterministic"] = deterministic
        captured["use_gpu"] = use_gpu
        return object()

    def fake_detect_video(video_path, session, frame_interval=1, conf_threshold=0.0):
        return []

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
    step = create_step("detect", {"model_path": "m.onnx", "device": "cuda:0"})
    ok = await step.run(manifest, _ctx(tmp_path, dump=False))
    assert ok is True

    # No dump dir ⇒ production path: GPU enabled, no determinism override
    assert captured["deterministic"] is False
    assert captured["use_gpu"] is True
    # And no parity copy exists
    assert not (tmp_path / "parity").exists()


# ----------------------------------------------------------------------
# track step — dump-intermediates + deterministic JSON
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_track_dumps_trajectory_when_dump_dir_set(tmp_path):
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
    ok = await step.run(manifest, _ctx(tmp_path, dump=True))
    assert ok is True

    dump_path = tmp_path / "parity" / "trajectory.json"
    assert dump_path.exists()
    assert (tmp_path / "trajectory.json").exists()


@pytest.mark.asyncio
async def test_track_trajectory_byte_identical_across_runs(tmp_path):
    """Two runs over the same detections → byte-identical trajectory.json.

    This is the Phase 4 parity guarantee: the Swift Kalman port can compare its
    Phase 4 trajectory.json output to a checked-in Python baseline using a
    file hash, not a tolerance.
    """
    detections = [
        {"frame_idx": i, "cx": 100.0 + i * 5, "cy": 100.0, "w": 8, "h": 8, "conf": 0.9}
        for i in range(10)
    ]
    det_path = tmp_path / "detections.json"
    det_path.write_text(json.dumps(detections), encoding="utf-8")

    out_paths = []
    for run_idx in range(2):
        run_dir = tmp_path / f"run_{run_idx}"
        run_dir.mkdir()
        # detections live in run_dir so track writes trajectory.json there too
        run_det = run_dir / "detections.json"
        run_det.write_text(det_path.read_text(encoding="utf-8"), encoding="utf-8")

        manifest = PipelineManifest.load_or_init(
            run_dir, str(run_dir / "game.mp4"), str(run_dir / "out.mp4")
        )
        manifest.put("detections_path", str(run_det))

        step = create_step("track", {})
        ok = await step.run(manifest, _ctx(run_dir, dump=False))
        assert ok is True
        out_paths.append((run_dir / "trajectory.json").read_bytes())

    assert out_paths[0] == out_paths[1], "track output is not byte-deterministic"


@pytest.mark.asyncio
async def test_track_dump_disabled_skips_parity_copy(tmp_path):
    detections = [
        {"frame_idx": 0, "cx": 100.0, "cy": 100.0, "w": 8, "h": 8, "conf": 0.9},
    ]
    det_path = tmp_path / "detections.json"
    det_path.write_text(json.dumps(detections), encoding="utf-8")

    manifest = PipelineManifest.load_or_init(
        tmp_path, str(tmp_path / "game.mp4"), str(tmp_path / "out.mp4")
    )
    manifest.put("detections_path", str(det_path))

    step = create_step("track", {})
    ok = await step.run(manifest, _ctx(tmp_path, dump=False))
    assert ok is True

    assert (tmp_path / "trajectory.json").exists()
    assert not (tmp_path / "parity").exists()
