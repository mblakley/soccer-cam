"""Tests for the PipelineProcessor (config-driven pipeline queue processor)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from video_grouper.pipeline.runner import PipelineResult
from video_grouper.task_processors.pipeline_processor import PipelineProcessor
from video_grouper.task_processors.tasks.pipeline import PipelineTask


@pytest.fixture
def storage_path():
    with tempfile.TemporaryDirectory() as temp_dir:
        yield Path(temp_dir)


@pytest.fixture
def group_dir(storage_path):
    g = storage_path / "flash__2024.06.01_vs_IYSA_home"
    g.mkdir()
    state = g / "state.json"
    state.write_text(json.dumps({"status": "trimmed"}))
    return g


def _make_config(youtube_enabled=False):
    cfg = MagicMock()
    cfg.youtube.enabled = youtube_enabled
    cfg.ttt.enabled = False
    cfg.pipeline.gpu_concurrency = 1
    cfg.pipeline.ram_heavy_concurrency = 1
    cfg.pipeline.ordered_steps.return_value = []
    return cfg


@pytest.fixture
def mock_config():
    return _make_config(youtube_enabled=False)


@pytest.fixture
def processor(storage_path, mock_config):
    return PipelineProcessor(
        storage_path=str(storage_path),
        config=mock_config,
        upload_processor=None,
        runtime="service",
    )


def _make_task(group_dir):
    return PipelineTask(
        group_dir=group_dir,
        input_path=str(group_dir / "input-raw.mp4"),
        output_path=str(group_dir / "output.mp4"),
        team_name="flash",
        storage_path=str(group_dir.parent),
    )


def _patch_runner(result: PipelineResult):
    """Patch PipelineRunner so .run returns *result* without touching disk."""
    runner = MagicMock()
    runner.run = AsyncMock(return_value=result)
    return patch("video_grouper.pipeline.runner.PipelineRunner", return_value=runner)


class TestProcessItem:
    @pytest.mark.asyncio
    async def test_complete_sets_pipeline_complete(self, processor, group_dir):
        task = _make_task(group_dir)
        with _patch_runner(PipelineResult("complete")):
            await processor.process_item(task)
        state = json.loads((group_dir / "state.json").read_text())
        assert state["status"] == "pipeline_complete"

    @pytest.mark.asyncio
    async def test_complete_queues_youtube_upload_when_enabled(
        self, storage_path, group_dir
    ):
        cfg = _make_config(youtube_enabled=True)
        upload = MagicMock()
        upload.add_work = AsyncMock()
        proc = PipelineProcessor(
            storage_path=str(storage_path),
            config=cfg,
            upload_processor=upload,
            runtime="service",
        )
        task = _make_task(group_dir)
        with _patch_runner(PipelineResult("complete")):
            await proc.process_item(task)
        upload.add_work.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_awaiting_leaves_group_untouched_no_error(self, processor, group_dir):
        task = _make_task(group_dir)
        with _patch_runner(PipelineResult("awaiting", awaiting_runtime="tray")):
            await processor.process_item(task)
        # State stays at trimmed (the other runtime resumes); no exception.
        state = json.loads((group_dir / "state.json").read_text())
        assert state["status"] == "trimmed"

    @pytest.mark.asyncio
    async def test_failed_sets_error_state(self, processor, group_dir):
        task = _make_task(group_dir)
        with _patch_runner(
            PipelineResult("failed", failed_step="ball_detect", error="boom")
        ):
            await processor.process_item(task)
        state = json.loads((group_dir / "state.json").read_text())
        assert state["status"] == "pipeline_failed"
        assert state["pipeline_error"] == "boom"


class TestProcessItemPrecheck:
    """process_item must skip groups already past completion — a stale task
    restored from disk after a crash would otherwise re-run hours of work."""

    @pytest.mark.asyncio
    async def test_skips_pipeline_complete_groups(self, processor, group_dir):
        (group_dir / "state.json").write_text(
            json.dumps({"status": "pipeline_complete"})
        )
        task = _make_task(group_dir)
        with _patch_runner(PipelineResult("complete")) as runner_factory:
            await processor.process_item(task)
        # The runner is never constructed — we short-circuited on status.
        runner_factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_legacy_ball_tracking_complete_groups(
        self, processor, group_dir
    ):
        (group_dir / "state.json").write_text(
            json.dumps({"status": "ball_tracking_complete"})
        )
        task = _make_task(group_dir)
        with _patch_runner(PipelineResult("complete")) as runner_factory:
            await processor.process_item(task)
        runner_factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_runs_for_trimmed_groups(self, processor, group_dir):
        task = _make_task(group_dir)
        with _patch_runner(PipelineResult("complete")) as runner_factory:
            await processor.process_item(task)
        runner_factory.assert_called_once()


class TestCorruptionRecovery:
    """Reactive recovery when a pipeline decode step fails on a corrupt input.

    Covers the trigger (decode_corruption failure -> recover -> re-run), and the
    BOUND that stops the N=16 retry-storm: recovery is attempted at most once per
    game, and a corruption it can't repair fails terminally instead of looping.
    """

    def _failed_corruption(self):
        return PipelineResult(
            "failed",
            failed_step="render",
            error="exception: Invalid data found",
            error_kind="decode_corruption",
        )

    def _patch_runner_seq(self, results):
        runner = MagicMock()
        runner.run = AsyncMock(side_effect=list(results))
        return (
            patch("video_grouper.pipeline.runner.PipelineRunner", return_value=runner),
            runner,
        )

    @pytest.mark.asyncio
    async def test_decode_corruption_recovers_and_reruns(self, processor, group_dir):
        from video_grouper.task_processors.corrupt_recovery import RecoveryOutcome

        task = _make_task(group_dir)
        runner_patch, runner = self._patch_runner_seq(
            [self._failed_corruption(), PipelineResult("complete")]
        )
        recover = AsyncMock(
            return_value=RecoveryOutcome(repaired=True, lost_seconds=13.0)
        )
        with (
            runner_patch,
            patch(
                "video_grouper.task_processors.corrupt_recovery.recover_pipeline_input",
                new=recover,
            ),
        ):
            await processor.process_item(task)

        recover.assert_awaited_once()
        assert runner.run.await_count == 2  # original run + one re-run on repair
        state = json.loads((group_dir / "state.json").read_text())
        assert state["status"] == "pipeline_complete"
        assert state["corrupt_recovery_attempted"] is True

    @pytest.mark.asyncio
    async def test_recovery_not_retried_when_already_attempted(
        self, processor, group_dir
    ):
        # The bound: a game already marked recovered must NOT recover again — it
        # fails terminally instead of looping.
        (group_dir / "state.json").write_text(
            json.dumps({"status": "trimmed", "corrupt_recovery_attempted": True})
        )
        task = _make_task(group_dir)
        runner_patch, runner = self._patch_runner_seq([self._failed_corruption()])
        recover = AsyncMock()
        with (
            runner_patch,
            patch(
                "video_grouper.task_processors.corrupt_recovery.recover_pipeline_input",
                new=recover,
            ),
        ):
            await processor.process_item(task)

        recover.assert_not_awaited()
        assert runner.run.await_count == 1  # no re-run
        state = json.loads((group_dir / "state.json").read_text())
        assert state["status"] == "pipeline_failed"

    @pytest.mark.asyncio
    async def test_unrepairable_corruption_fails_terminally(self, processor, group_dir):
        from video_grouper.task_processors.corrupt_recovery import RecoveryOutcome

        task = _make_task(group_dir)
        runner_patch, runner = self._patch_runner_seq([self._failed_corruption()])
        recover = AsyncMock(
            return_value=RecoveryOutcome(repaired=False, reason="no corruption found")
        )
        with (
            runner_patch,
            patch(
                "video_grouper.task_processors.corrupt_recovery.recover_pipeline_input",
                new=recover,
            ),
        ):
            await processor.process_item(task)

        recover.assert_awaited_once()
        assert runner.run.await_count == 1  # not repaired -> no re-run
        state = json.loads((group_dir / "state.json").read_text())
        assert state["status"] == "pipeline_failed"
        # Marked attempted (written before recovery) so it can't loop next pass.
        assert state["corrupt_recovery_attempted"] is True


class TestGetItemKey:
    def test_keys_equal_for_identical_tasks(self, processor, group_dir):
        a = _make_task(group_dir)
        b = _make_task(group_dir)
        assert processor.get_item_key(a) == processor.get_item_key(b)

    def test_keys_differ_per_output(self, processor, group_dir):
        a = _make_task(group_dir)
        b = PipelineTask(
            group_dir=group_dir,
            input_path=a.input_path,
            output_path=str(group_dir / "other.mp4"),
        )
        assert processor.get_item_key(a) != processor.get_item_key(b)
