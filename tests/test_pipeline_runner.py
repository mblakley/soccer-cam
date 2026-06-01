"""Unit tests for PipelineRunner (ordering, resume, handoff, validation)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from video_grouper.pipeline import register_step
from video_grouper.pipeline.base import PipelineStep, StepContext, StepSpec
from video_grouper.pipeline.manifest import PipelineManifest
from video_grouper.pipeline.runner import PipelineRunner

RUN_COUNTS: dict[str, int] = {}


class _Cfg(BaseModel):
    v: int = 0


def _ctx(tmp_path):
    return StepContext(group_dir=tmp_path, team_name=None, storage_path=tmp_path)


def _spec(step_id: str, type_: str, **cfg) -> StepSpec:
    return StepSpec(step_id=step_id, type=type_, config=cfg)


class StepA(PipelineStep):
    name = "t_a"
    config_model = _Cfg
    produces = ("a_path",)
    runtime = "service"

    async def run(self, manifest, ctx):
        RUN_COUNTS["t_a"] = RUN_COUNTS.get("t_a", 0) + 1
        p = Path(ctx.group_dir) / "a.txt"
        p.write_text("a")
        manifest.put("a_path", str(p))
        return True


class StepB(PipelineStep):
    name = "t_b"
    config_model = _Cfg
    consumes = ("a_path",)
    produces = ("output_path",)
    runtime = "service"

    async def run(self, manifest, ctx):
        RUN_COUNTS["t_b"] = RUN_COUNTS.get("t_b", 0) + 1
        Path(manifest.get("output_path")).write_text("out")
        return True


class StepTray(PipelineStep):
    name = "t_tray"
    config_model = _Cfg
    produces = ("tray_path",)
    runtime = "tray"

    async def run(self, manifest, ctx):
        RUN_COUNTS["t_tray"] = RUN_COUNTS.get("t_tray", 0) + 1
        p = Path(ctx.group_dir) / "tray.txt"
        p.write_text("t")
        manifest.put("tray_path", str(p))
        return True


class StepNeedsMissing(PipelineStep):
    name = "t_needs_missing"
    config_model = _Cfg
    consumes = ("missing_key",)
    runtime = "service"

    async def run(self, manifest, ctx):  # pragma: no cover - never reached
        return True


class StepBadOutput(PipelineStep):
    name = "t_bad"
    config_model = _Cfg
    produces = ("never_written",)
    runtime = "service"

    async def run(self, manifest, ctx):
        return True  # claims an output but writes nothing


class StepReturnsFalse(PipelineStep):
    name = "t_false"
    config_model = _Cfg
    runtime = "service"

    async def run(self, manifest, ctx):
        return False


class StepRebind(PipelineStep):
    """Optional rebinding step (no declared output) — mimics stitch_correct."""

    name = "t_rebind"
    config_model = _Cfg
    consumes = ("input_path",)
    produces = ()
    runtime = "service"

    async def run(self, manifest, ctx):
        RUN_COUNTS["t_rebind"] = RUN_COUNTS.get("t_rebind", 0) + 1
        corrected = Path(ctx.group_dir) / "corrected.txt"
        corrected.write_text("corrected")
        manifest.put("corrected_path", str(corrected))
        manifest.put("input_path", str(corrected))  # rebind
        return True


class StepReader(PipelineStep):
    name = "t_reader"
    config_model = _Cfg
    consumes = ("input_path",)
    produces = ("output_path",)
    runtime = "service"

    async def run(self, manifest, ctx):
        RUN_COUNTS["t_reader"] = RUN_COUNTS.get("t_reader", 0) + 1
        # Raises if the rebound input was lost and not regenerated.
        content = Path(manifest.get("input_path")).read_text()
        Path(manifest.get("output_path")).write_text("out:" + content)
        return True


class StepUnavail(PipelineStep):
    name = "t_unavail"
    config_model = _Cfg
    runtime = "service"
    requires = ("totally_missing_mod_xyz",)

    async def run(self, manifest, ctx):  # pragma: no cover - refused before run
        return True


for _s in (
    StepA,
    StepB,
    StepTray,
    StepNeedsMissing,
    StepBadOutput,
    StepReturnsFalse,
    StepRebind,
    StepReader,
    StepUnavail,
):
    register_step(_s.name, _s, _Cfg)


@pytest.fixture(autouse=True)
def _reset_counts():
    RUN_COUNTS.clear()
    yield


@pytest.mark.asyncio
async def test_runs_all_in_order_and_completes(tmp_path):
    runner = PipelineRunner([_spec("a", "t_a"), _spec("b", "t_b")])
    result = await runner.run(
        str(tmp_path / "game.mp4"), str(tmp_path / "out.mp4"), _ctx(tmp_path)
    )
    assert result.ok
    assert RUN_COUNTS == {"t_a": 1, "t_b": 1}
    assert (tmp_path / "out.mp4").exists()


@pytest.mark.asyncio
async def test_resume_skips_completed(tmp_path):
    specs = [_spec("a", "t_a"), _spec("b", "t_b")]
    in_path, out_path = str(tmp_path / "game.mp4"), str(tmp_path / "out.mp4")
    assert (await PipelineRunner(specs).run(in_path, out_path, _ctx(tmp_path))).ok
    assert (await PipelineRunner(specs).run(in_path, out_path, _ctx(tmp_path))).ok
    assert RUN_COUNTS == {"t_a": 1, "t_b": 1}  # second run skipped both


@pytest.mark.asyncio
async def test_changed_step_invalidates_downstream(tmp_path):
    in_path, out_path = str(tmp_path / "game.mp4"), str(tmp_path / "out.mp4")
    await PipelineRunner([_spec("a", "t_a", v=1), _spec("b", "t_b")]).run(
        in_path, out_path, _ctx(tmp_path)
    )
    assert RUN_COUNTS == {"t_a": 1, "t_b": 1}
    # changing a's config re-runs a, and b must re-run too (upstream changed)
    await PipelineRunner([_spec("a", "t_a", v=2), _spec("b", "t_b")]).run(
        in_path, out_path, _ctx(tmp_path)
    )
    assert RUN_COUNTS == {"t_a": 2, "t_b": 2}


@pytest.mark.asyncio
async def test_missing_consumes_fails(tmp_path):
    result = await PipelineRunner([_spec("x", "t_needs_missing")]).run(
        str(tmp_path / "g.mp4"), str(tmp_path / "o.mp4"), _ctx(tmp_path)
    )
    assert result.status == "failed"
    assert result.failed_step == "x"
    assert "missing required inputs" in result.error


@pytest.mark.asyncio
async def test_declared_output_missing_fails(tmp_path):
    result = await PipelineRunner([_spec("x", "t_bad")]).run(
        str(tmp_path / "g.mp4"), str(tmp_path / "o.mp4"), _ctx(tmp_path)
    )
    assert result.status == "failed"
    assert "never_written" in result.error


@pytest.mark.asyncio
async def test_step_returns_false_fails(tmp_path):
    result = await PipelineRunner([_spec("x", "t_false")]).run(
        str(tmp_path / "g.mp4"), str(tmp_path / "o.mp4"), _ctx(tmp_path)
    )
    assert result.status == "failed"


@pytest.mark.asyncio
async def test_unknown_step_type_fails(tmp_path):
    result = await PipelineRunner([_spec("x", "no_such_type")]).run(
        str(tmp_path / "g.mp4"), str(tmp_path / "o.mp4"), _ctx(tmp_path)
    )
    assert result.status == "failed"
    assert "Unknown pipeline step" in result.error


@pytest.mark.asyncio
async def test_final_output_missing_marks_last_step_failed(tmp_path):
    # t_a completes but nothing writes output_path -> final check fails, and the
    # failure is attributed to the last step (so resume re-runs it).
    result = await PipelineRunner([_spec("a", "t_a")]).run(
        str(tmp_path / "g.mp4"), str(tmp_path / "o.mp4"), _ctx(tmp_path)
    )
    assert result.status == "failed"
    assert result.failed_step == "a"
    assert "missing or empty" in result.error
    m = PipelineManifest.load_or_init(tmp_path, "g.mp4", "o.mp4")
    assert m._find("a")["status"] == "failed"


@pytest.mark.asyncio
async def test_cross_session_handoff(tmp_path):
    specs = [_spec("a", "t_a"), _spec("tray", "t_tray"), _spec("b", "t_b")]
    in_path, out_path = str(tmp_path / "game.mp4"), str(tmp_path / "out.mp4")
    ctx = _ctx(tmp_path)

    # service: runs a, hits tray step -> awaiting tray, stops
    r1 = await PipelineRunner(specs, runtime="service").run(in_path, out_path, ctx)
    assert r1.status == "awaiting" and r1.awaiting_runtime == "tray"
    assert RUN_COUNTS.get("t_a") == 1 and "t_tray" not in RUN_COUNTS

    # tray: skips a, runs tray, hits b (service) -> awaiting service
    r2 = await PipelineRunner(specs, runtime="tray").run(in_path, out_path, ctx)
    assert r2.status == "awaiting" and r2.awaiting_runtime == "service"
    assert RUN_COUNTS.get("t_tray") == 1 and RUN_COUNTS.get("t_a") == 1

    # service resumes: skips a + tray, runs b -> complete
    r3 = await PipelineRunner(specs, runtime="service").run(in_path, out_path, ctx)
    assert r3.ok
    assert RUN_COUNTS.get("t_b") == 1
    assert (tmp_path / "out.mp4").exists()


@pytest.mark.asyncio
async def test_deleted_output_forces_rerun(tmp_path):
    specs = [_spec("a", "t_a"), _spec("b", "t_b")]
    in_path, out_path = str(tmp_path / "game.mp4"), str(tmp_path / "out.mp4")
    await PipelineRunner(specs).run(in_path, out_path, _ctx(tmp_path))
    assert RUN_COUNTS == {"t_a": 1, "t_b": 1}
    # delete a's recorded output -> a must re-run (and b too via dirty cascade)
    (tmp_path / "a.txt").unlink()
    await PipelineRunner(specs).run(in_path, out_path, _ctx(tmp_path))
    assert RUN_COUNTS == {"t_a": 2, "t_b": 2}


@pytest.mark.asyncio
async def test_rebind_step_reruns_after_output_deleted(tmp_path):
    # Pins the resume fix: an optional rebinding step (produces=()) whose rebound
    # output is deleted must re-run from the original source, not loop forever.
    specs = [_spec("r", "t_rebind"), _spec("rd", "t_reader")]
    in_path, out_path = str(tmp_path / "game.mp4"), str(tmp_path / "out.mp4")
    r1 = await PipelineRunner(specs).run(in_path, out_path, _ctx(tmp_path))
    assert r1.ok
    assert RUN_COUNTS == {"t_rebind": 1, "t_reader": 1}

    (tmp_path / "corrected.txt").unlink()
    r2 = await PipelineRunner(specs).run(in_path, out_path, _ctx(tmp_path))
    assert r2.ok  # recovered, not stuck
    assert RUN_COUNTS == {"t_rebind": 2, "t_reader": 2}
    assert (tmp_path / "corrected.txt").exists()


@pytest.mark.asyncio
async def test_unavailable_step_refused(tmp_path):
    result = await PipelineRunner([_spec("u", "t_unavail")]).run(
        str(tmp_path / "g.mp4"), str(tmp_path / "o.mp4"), _ctx(tmp_path)
    )
    assert result.status == "failed"
    assert "unavailable in this bundle" in result.error


@pytest.mark.asyncio
async def test_failure_records_correct_step_type(tmp_path):
    # mark_failed on a pre-mark_running path must record the real type, not step_id.
    await PipelineRunner([_spec("u", "t_unavail")]).run(
        str(tmp_path / "g.mp4"), str(tmp_path / "o.mp4"), _ctx(tmp_path)
    )
    m = PipelineManifest.load_or_init(tmp_path, "g.mp4", "o.mp4")
    rec = m._find("u")
    assert rec["status"] == "failed"
    assert rec["type"] == "t_unavail"  # not the step_id "u"
