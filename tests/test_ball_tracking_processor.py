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
from video_grouper.task_processors.tasks.ball_tracking import BallTrackingTask


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
    async def test_failure_does_not_update_state(self, processor, group_dir):
        task = _make_task(group_dir)
        task.execute = AsyncMock(return_value=False)

        await processor.process_item(task)

        state = json.loads((group_dir / "state.json").read_text())
        assert state["status"] == "trimmed"  # unchanged

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


class TestGetItemKey:
    def test_keys_differ_per_provider(self, processor, group_dir):
        a = _make_task(group_dir)
        b = BallTrackingTask(
            group_dir=group_dir,
            input_path=a.input_path,
            output_path=a.output_path,
            provider_name="homegrown",
            provider_config={},
        )
        assert processor.get_item_key(a) != processor.get_item_key(b)

    def test_keys_equal_for_identical_tasks(self, processor, group_dir):
        a = _make_task(group_dir)
        b = _make_task(group_dir)
        assert processor.get_item_key(a) == processor.get_item_key(b)
