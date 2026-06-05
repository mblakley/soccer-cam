"""Tests for the BallTrackingProcessor (renamed/redesigned from AutocamProcessor)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from video_grouper.task_processors.ball_tracking_processor import (
    BallTrackingProcessor,
)
from video_grouper.task_processors.tasks.ball_tracking.ball_tracking_task import (
    BallTrackingTask,
)


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


@pytest.fixture
def mock_config():
    cfg = MagicMock()
    cfg.youtube.enabled = False
    return cfg


@pytest.fixture
def processor(storage_path, mock_config):
    return BallTrackingProcessor(
        storage_path=str(storage_path),
        config=mock_config,
        upload_processor=None,
    )


def _make_task(group_dir):
    return BallTrackingTask(
        group_dir=group_dir,
        input_path=str(group_dir / "input-raw.mp4"),
        output_path=str(group_dir / "output.mp4"),
        provider_name="autocam_gui",
        provider_config={"executable": "fake.exe"},
    )


class TestProcessItem:
    @pytest.mark.asyncio
    async def test_success_updates_state_to_complete(self, processor, group_dir):
        task = _make_task(group_dir)
        task.execute = AsyncMock(return_value=True)

        await processor.process_item(task)

        state = json.loads((group_dir / "state.json").read_text())
        assert state["status"] == "ball_tracking_complete"

    @pytest.mark.asyncio
    async def test_failure_raises_and_records_failure_count(
        self, processor, group_dir
    ):
        task = _make_task(group_dir)
        task.execute = AsyncMock(return_value=False)

        with pytest.raises(RuntimeError, match="Ball tracking task returned False"):
            await processor.process_item(task)

        state = json.loads((group_dir / "state.json").read_text())
        assert state["status"] == "trimmed"  # unchanged
        assert state["ball_tracking_failures"] == 1
        assert "last_ball_tracking_failure" in state

    @pytest.mark.asyncio
    async def test_success_queues_youtube_upload_when_enabled(
        self, storage_path, group_dir
    ):
        cfg = MagicMock()
        cfg.youtube.enabled = True
        upload = MagicMock()
        upload.add_work = AsyncMock()
        proc = BallTrackingProcessor(
            storage_path=str(storage_path), config=cfg, upload_processor=upload
        )
        task = _make_task(group_dir)
        task.execute = AsyncMock(return_value=True)

        await proc.process_item(task)

        upload.add_work.assert_awaited_once()


class TestProcessItemPrecheck:
    """process_item must skip tasks for groups already past
    ball_tracking_complete — common when a tray crash leaves an
    in_progress task on disk and a fresh tray restores it after the
    state has already advanced (most often because the upload pipeline
    completed it via the recovery path while the tray was down)."""

    @pytest.mark.asyncio
    async def test_skips_ball_tracking_complete_groups(self, processor, group_dir):
        (group_dir / "state.json").write_text(
            json.dumps({"status": "ball_tracking_complete"})
        )
        task = _make_task(group_dir)
        task.execute = AsyncMock(return_value=True)
        await processor.process_item(task)
        # Crucially — execute is NOT called.
        task.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_complete_groups(self, processor, group_dir):
        (group_dir / "state.json").write_text(json.dumps({"status": "complete"}))
        task = _make_task(group_dir)
        task.execute = AsyncMock(return_value=True)
        await processor.process_item(task)
        task.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_runs_for_trimmed_groups(self, processor, group_dir):
        # Sanity — the pre-check must not over-fire on the normal case.
        # (group_dir fixture sets state.json to {"status": "trimmed"}.)
        task = _make_task(group_dir)
        task.execute = AsyncMock(return_value=True)
        await processor.process_item(task)
        task.execute.assert_awaited_once()


class TestGetItemKey:
    def test_keys_same_for_same_group_different_provider(self, processor, group_dir):
        """One group = one ball tracking task, regardless of provider."""
        a = _make_task(group_dir)
        b = BallTrackingTask(
            group_dir=group_dir,
            input_path=a.input_path,
            output_path=a.output_path,
            provider_name="homegrown",
            provider_config={},
        )
        assert processor.get_item_key(a) == processor.get_item_key(b)

    def test_keys_equal_for_identical_tasks(self, processor, group_dir):
        a = _make_task(group_dir)
        b = _make_task(group_dir)
        assert processor.get_item_key(a) == processor.get_item_key(b)
