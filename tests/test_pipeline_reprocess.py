"""Tests for the per-recording reprocess override mechanism.

Two surfaces are covered:

1. :func:`apply_overrides` — pure spec-list transformation. Patches the
   stabilize step's config and (optionally) swaps detect for
   transform_detections.
2. End-to-end runner integration — a reprocess_request.json present in
   the group dir patches the live specs, fingerprint changes drive
   re-runs, and the preseed mechanism makes the previous detect's
   detections_path available to the cheap-reprocess shortcut.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

import video_grouper.pipeline.register_steps  # noqa: F401
from video_grouper.pipeline import register_step
from video_grouper.pipeline.base import PipelineStep, StepSpec
from video_grouper.pipeline.reprocess import (
    ReprocessRequest,
    apply_overrides,
    read_reprocess_request,
)

# Module-scope stub steps for the runner integration test. The pipeline
# registry stores (module, class) STRING refs and looks them up via
# ``importlib``, so test-local nested classes can't be found that way.
_CAPTURED_STRENGTHS: list[str | None] = []


class _StabilizeStubConfig(BaseModel):
    stabilization_strength: str | None = None


class _StabilizeStub(PipelineStep):
    name = "_reproc_stab_stub"
    config_model = _StabilizeStubConfig
    consumes = ("input_path",)
    produces = ("motion_path",)
    runtime = "any"
    requires: tuple = ()
    resources: tuple = ()

    async def run(self, manifest, ctx):
        _CAPTURED_STRENGTHS.append(self.config.stabilization_strength)
        out = ctx.group_dir / "motion.json"
        out.write_text("{}")
        manifest.put("motion_path", str(out))
        return True


class _RenderStubConfig(BaseModel):
    pass


class _RenderStub(PipelineStep):
    name = "_reproc_render_stub"
    config_model = _RenderStubConfig
    consumes = ("input_path",)
    produces = ("output_path",)
    runtime = "any"
    requires: tuple = ()
    resources: tuple = ()

    async def run(self, manifest, ctx):
        out_path = manifest.data["output_path"]
        Path(out_path).write_text("rendered")
        manifest.put("output_path", out_path)
        return True


def _write_pipeline_state(group_dir: Path, *, running: bool) -> None:
    """Seed pipeline_state.json with a single step at running/complete."""
    payload = {
        "version": 1,
        "input_path": str(group_dir / "src.mp4"),
        "output_path": str(group_dir / "out.mp4"),
        "artifacts": {},
        "steps": [
            {
                "step_id": "stabilize",
                "type": "stabilize",
                "status": "running" if running else "complete",
            }
        ],
    }
    (group_dir / "pipeline_state.json").write_text(json.dumps(payload))


# ---------------------------------------------------------------------------
# read_reprocess_request
# ---------------------------------------------------------------------------


def test_read_returns_none_when_missing(tmp_path: Path):
    assert read_reprocess_request(tmp_path) is None


def test_read_parses_full_request(tmp_path: Path):
    (tmp_path / "reprocess_request.json").write_text(
        json.dumps(
            {
                "stabilization_strength": "extreme",
                "skip_detect": True,
                "requested_at": "2026-06-10T12:00:00Z",
                "requested_by": "tray",
            }
        )
    )
    req = read_reprocess_request(tmp_path)
    assert req is not None
    assert req.stabilization_strength == "extreme"
    assert req.skip_detect is True
    assert req.requested_by == "tray"


def test_read_returns_none_on_garbage(tmp_path: Path):
    (tmp_path / "reprocess_request.json").write_text("not json at all {{{ ")
    assert read_reprocess_request(tmp_path) is None


def test_read_returns_none_on_schema_violation(tmp_path: Path):
    (tmp_path / "reprocess_request.json").write_text(
        json.dumps({"skip_detect": "not a bool"})
    )
    assert read_reprocess_request(tmp_path) is None


# ---------------------------------------------------------------------------
# apply_overrides
# ---------------------------------------------------------------------------


def _base_specs() -> list[StepSpec]:
    """A standard broadcast_stabilized-like spec list."""
    return [
        StepSpec("stitch_correct", "stitch_correct", {}),
        StepSpec("stabilize", "stabilize", {"stabilization_strength": "heavy"}),
        StepSpec("detect", "detect", {"detect_confidence": 0.45}),
        StepSpec("track", "track", {}),
        StepSpec("render", "render", {"render_mode": "broadcast"}),
    ]


def test_no_request_returns_inputs_unchanged():
    specs = _base_specs()
    req = ReprocessRequest()
    new_specs, preseed = apply_overrides(specs, req)
    # Same step ids in same order, same configs.
    assert [s.step_id for s in new_specs] == [s.step_id for s in specs]
    assert [s.type for s in new_specs] == [s.type for s in specs]
    assert preseed == []


def test_strength_patch_replaces_stabilization_strength_only():
    """The stabilize step's config gets the new strength; other fields
    + other steps are untouched."""
    specs = _base_specs()
    req = ReprocessRequest(stabilization_strength="extreme")
    new_specs, preseed = apply_overrides(specs, req)
    stab = next(s for s in new_specs if s.type == "stabilize")
    assert stab.config["stabilization_strength"] == "extreme"
    # Other steps untouched.
    detect = next(s for s in new_specs if s.type == "detect")
    assert detect.config == {"detect_confidence": 0.45}
    assert preseed == []


def test_skip_detect_replaces_detect_with_transform_detections():
    """Cheap reprocess: detect spec swapped for transform_detections at
    the same step_id slot. Preseed list names the old detect step_id so
    its produced detections_path is replayed before the loop."""
    specs = _base_specs()
    req = ReprocessRequest(skip_detect=True)
    new_specs, preseed = apply_overrides(specs, req)
    types = [s.type for s in new_specs]
    # detect → transform_detections
    assert "detect" not in types
    assert "transform_detections" in types
    # Same step_id slot ("detect") so subsequent step lookups stay stable.
    swapped = next(s for s in new_specs if s.type == "transform_detections")
    assert swapped.step_id == "detect"
    # Step order preserved.
    assert types == [
        "stitch_correct",
        "stabilize",
        "transform_detections",
        "track",
        "render",
    ]
    assert preseed == ["detect"]


def test_both_overrides_compose():
    """Strength + skip_detect together produce both patches independently."""
    specs = _base_specs()
    req = ReprocessRequest(stabilization_strength="extreme", skip_detect=True)
    new_specs, preseed = apply_overrides(specs, req)
    stab = next(s for s in new_specs if s.type == "stabilize")
    assert stab.config["stabilization_strength"] == "extreme"
    assert any(s.type == "transform_detections" for s in new_specs)
    assert preseed == ["detect"]


def test_skip_detect_without_detect_in_specs_is_noop():
    """Robustness: if the spec list has no detect step (e.g. autocam
    preset), skip_detect can't replace anything — preseed stays empty."""
    specs = [
        StepSpec("autocam", "autocam", {}),
    ]
    req = ReprocessRequest(skip_detect=True)
    new_specs, preseed = apply_overrides(specs, req)
    assert [s.type for s in new_specs] == ["autocam"]
    assert preseed == []


# ---------------------------------------------------------------------------
# End-to-end runner integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_honours_reprocess_strength_patch(tmp_path: Path):
    """End-to-end: writing reprocess_request.json with a new strength
    makes the runner pass that strength into the stabilize step's
    config (which changes its fingerprint and forces a re-run).

    Uses the module-level stub step types so we don't haul in PyAV /
    ONNX, but exercises the real registry + runner + manifest layers.
    """
    from video_grouper.pipeline import _STEP_REGISTRY
    from video_grouper.pipeline.base import StepContext
    from video_grouper.pipeline.runner import PipelineRunner

    # The reprocess override patches by step TYPE — register the stubs
    # under the real production names so the override actually picks
    # them up. Save + restore the registry around the test.
    orig = dict(_STEP_REGISTRY)
    try:
        register_step("stabilize", _StabilizeStub, _StabilizeStubConfig)
        register_step("render", _RenderStub, _RenderStubConfig)

        in_path = tmp_path / "in.mp4"
        in_path.write_text("source")
        out_path = tmp_path / "out.mp4"
        ctx = StepContext(group_dir=tmp_path, team_name=None, storage_path=tmp_path)

        specs = [
            StepSpec("stabilize", "stabilize", {"stabilization_strength": "standard"}),
            StepSpec("render", "render", {}),
        ]
        runner = PipelineRunner(specs, runtime="any")
        _CAPTURED_STRENGTHS.clear()

        # Run 1: no override file. Stabilize sees the "standard" preset value.
        result = await runner.run(str(in_path), str(out_path), ctx)
        assert result.status == "complete", result
        assert _CAPTURED_STRENGTHS == ["standard"]

        # Run 2: override file present. Stabilize must re-run with
        # "extreme", proving both the override application AND the
        # fingerprint mismatch driving the re-run.
        (tmp_path / "reprocess_request.json").write_text(
            json.dumps({"stabilization_strength": "extreme"})
        )
        _CAPTURED_STRENGTHS.clear()
        result = await runner.run(str(in_path), str(out_path), ctx)
        assert result.status == "complete", result
        assert _CAPTURED_STRENGTHS == ["extreme"]

        # Run 3: same override file, no other change. Fingerprint now
        # matches the recorded "extreme" run, so stabilize must NOT re-run.
        _CAPTURED_STRENGTHS.clear()
        result = await runner.run(str(in_path), str(out_path), ctx)
        assert result.status == "complete", result
        assert _CAPTURED_STRENGTHS == []
    finally:
        _STEP_REGISTRY.clear()
        _STEP_REGISTRY.update(orig)


# ---------------------------------------------------------------------------
# is_pipeline_running + cancel marker
# ---------------------------------------------------------------------------


class TestRunningDetection:
    """The web layer + dashboard ask `is_pipeline_running` to decide
    whether to reject a new reprocess and show a Cancel button instead."""

    def test_no_state_file_means_not_running(self, tmp_path: Path):
        from video_grouper.pipeline.reprocess import is_pipeline_running

        assert is_pipeline_running(tmp_path) is False

    def test_running_step_returns_true(self, tmp_path: Path):
        from video_grouper.pipeline.reprocess import is_pipeline_running

        _write_pipeline_state(tmp_path, running=True)
        assert is_pipeline_running(tmp_path) is True

    def test_only_complete_steps_returns_false(self, tmp_path: Path):
        from video_grouper.pipeline.reprocess import is_pipeline_running

        _write_pipeline_state(tmp_path, running=False)
        assert is_pipeline_running(tmp_path) is False

    def test_garbage_state_file_is_not_running(self, tmp_path: Path):
        from video_grouper.pipeline.reprocess import is_pipeline_running

        (tmp_path / "pipeline_state.json").write_text("not json {")
        # Tolerant: a parse failure is reported as not-running so the
        # dashboard doesn't get stuck showing Cancel forever.
        assert is_pipeline_running(tmp_path) is False


class TestCancelMarker:
    def test_write_then_observe_then_consume(self, tmp_path: Path):
        from video_grouper.pipeline.reprocess import (
            cancel_requested,
            consume_cancel_request,
            write_cancel_request,
        )

        assert cancel_requested(tmp_path) is False
        write_cancel_request(tmp_path)
        assert cancel_requested(tmp_path) is True
        consume_cancel_request(tmp_path)
        assert cancel_requested(tmp_path) is False

    def test_consume_missing_marker_is_noop(self, tmp_path: Path):
        from video_grouper.pipeline.reprocess import consume_cancel_request

        # Should not raise on a missing marker.
        consume_cancel_request(tmp_path)


@pytest.mark.asyncio
async def test_runner_returns_cancelled_when_marker_present(tmp_path: Path):
    """End-to-end: a cancel marker written between steps makes the
    runner exit with PipelineResult('cancelled') instead of running
    the next step. The marker is consumed so it doesn't poison the
    next run."""
    from video_grouper.pipeline import _STEP_REGISTRY
    from video_grouper.pipeline.base import StepContext
    from video_grouper.pipeline.reprocess import (
        cancel_requested,
        write_cancel_request,
    )
    from video_grouper.pipeline.runner import PipelineRunner

    orig = dict(_STEP_REGISTRY)
    try:
        register_step("stabilize", _StabilizeStub, _StabilizeStubConfig)
        register_step("render", _RenderStub, _RenderStubConfig)
        in_path = tmp_path / "in.mp4"
        in_path.write_text("source")
        out_path = tmp_path / "out.mp4"
        ctx = StepContext(group_dir=tmp_path, team_name=None, storage_path=tmp_path)
        # Plant the cancel marker BEFORE the first step runs.
        write_cancel_request(tmp_path)

        specs = [
            StepSpec("stabilize", "stabilize", {}),
            StepSpec("render", "render", {}),
        ]
        _CAPTURED_STRENGTHS.clear()
        result = await PipelineRunner(specs, runtime="any").run(
            str(in_path), str(out_path), ctx
        )
        assert result.status == "cancelled"
        # The cancel observation cleared the marker so the next run
        # isn't poisoned.
        assert cancel_requested(tmp_path) is False
        # No steps ran.
        assert _CAPTURED_STRENGTHS == []
    finally:
        _STEP_REGISTRY.clear()
        _STEP_REGISTRY.update(orig)
