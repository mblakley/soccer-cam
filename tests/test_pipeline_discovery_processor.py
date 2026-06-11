"""Tests for the PipelineDiscoveryProcessor.

Covers the two discovery actions:
* ``trimmed`` groups -> enqueue a PipelineTask
* completion statuses (both ``pipeline_complete`` and the legacy
  ``ball_tracking_complete``) -> recover a missed YouTube upload exactly once.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from video_grouper.task_processors.pipeline_discovery_processor import (
    PipelineDiscoveryProcessor,
)
from video_grouper.task_processors.tasks.pipeline import PipelineTask


@pytest.fixture
def storage_path():
    with tempfile.TemporaryDirectory() as temp_dir:
        yield Path(temp_dir)


def _make_group(storage_path: Path, name: str, status: str, with_raw=True) -> Path:
    g = storage_path / name
    g.mkdir()
    (g / "state.json").write_text(json.dumps({"status": status}))
    if with_raw:
        # get_ball_tracking_io_paths searches for a *-raw.mp4 source.
        (g / f"{name}-raw.mp4").write_bytes(b"\x00" * 32)
    return g


def _make_config(youtube_enabled=True):
    cfg = MagicMock()
    cfg.youtube.enabled = youtube_enabled
    cfg.ttt.enabled = False
    return cfg


def _processor(storage_path, config, upload_processor=None):
    pipeline_processor = MagicMock()
    pipeline_processor.add_work = AsyncMock()
    pipeline_processor.upload_processor = upload_processor
    proc = PipelineDiscoveryProcessor(
        storage_path=str(storage_path),
        config=config,
        pipeline_processor=pipeline_processor,
    )
    return proc, pipeline_processor


@pytest.mark.asyncio
async def test_trimmed_enqueues_pipeline_task(storage_path):
    _make_group(storage_path, "flash__2024.06.01_vs_IYSA_home", "trimmed")
    proc, pipeline_processor = _processor(storage_path, _make_config())

    await proc.discover_work()

    pipeline_processor.add_work.assert_awaited_once()
    enqueued = pipeline_processor.add_work.await_args.args[0]
    assert isinstance(enqueued, PipelineTask)
    assert enqueued.input_path.endswith("-raw.mp4")


@pytest.mark.asyncio
async def test_trimmed_without_raw_does_not_enqueue(storage_path):
    _make_group(
        storage_path, "flash__2024.06.01_vs_IYSA_home", "trimmed", with_raw=False
    )
    proc, pipeline_processor = _processor(storage_path, _make_config())

    await proc.discover_work()

    pipeline_processor.add_work.assert_not_called()


@pytest.mark.asyncio
async def test_pipeline_complete_triggers_upload_recovery_once(storage_path):
    _make_group(storage_path, "flash__2024.06.01_vs_IYSA_home", "pipeline_complete")
    upload = MagicMock()
    upload.add_work = AsyncMock()
    proc, _ = _processor(storage_path, _make_config(), upload_processor=upload)

    await proc.discover_work()
    await proc.discover_work()  # second cycle must not re-enqueue

    upload.add_work.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_ball_tracking_complete_triggers_upload_recovery(storage_path):
    _make_group(
        storage_path, "heat__2024.05.31_vs_Fairport_home", "ball_tracking_complete"
    )
    upload = MagicMock()
    upload.add_work = AsyncMock()
    proc, _ = _processor(storage_path, _make_config(), upload_processor=upload)

    await proc.discover_work()

    upload.add_work.assert_awaited_once()


@pytest.mark.asyncio
async def test_recovery_skipped_when_youtube_disabled(storage_path):
    _make_group(storage_path, "flash__2024.06.01_vs_IYSA_home", "pipeline_complete")
    upload = MagicMock()
    upload.add_work = AsyncMock()
    proc, _ = _processor(
        storage_path, _make_config(youtube_enabled=False), upload_processor=upload
    )

    await proc.discover_work()

    upload.add_work.assert_not_called()


@pytest.mark.asyncio
async def test_no_processor_is_safe(storage_path):
    _make_group(storage_path, "flash__2024.06.01_vs_IYSA_home", "trimmed")
    config = _make_config()
    proc = PipelineDiscoveryProcessor(
        storage_path=str(storage_path), config=config, pipeline_processor=None
    )
    # Must not raise when there's no processor to enqueue to.
    await proc.discover_work()
