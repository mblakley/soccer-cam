"""Tests for TTTPoller — the single polling entrypoint that discovers
TTT work and enqueues onto each feature's QueueProcessor."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from video_grouper.task_processors.tasks.clip.clip_request_task import (
    ClipRequestTask,
)
from video_grouper.task_processors.tasks.ttt.highlight_reel_task import (
    HighlightReelTask,
)
from video_grouper.task_processors.tasks.ttt.reprocess_request_task import (
    ReprocessRequestTask,
)
from video_grouper.task_processors.tasks.ttt.ttt_job_task import TTTJobTask
from video_grouper.task_processors.ttt_poller import TTTPoller


@pytest.fixture(autouse=True)
def mock_ffmpeg():  # noqa: PT004
    yield None


@pytest.fixture(autouse=True)
def mock_file_system():  # noqa: PT004
    yield None


def _mk_config(*, ttt_enabled: bool = True, camera_id: str | None = None):
    cfg = MagicMock()
    cfg.ttt.enabled = ttt_enabled
    cfg.ttt.camera_id = camera_id
    cfg.ttt.machine_name = "test"
    cfg.pipeline.is_active.return_value = False
    cfg.camera.type = "dahua"
    cfg.camera.device_ip = "127.0.0.1"
    return cfg


def _mk_ttt(authenticated: bool = True):
    c = MagicMock()
    c.is_authenticated.return_value = authenticated
    return c


def _mk_processor():
    p = MagicMock()
    p.add_work = AsyncMock()
    return p


def _mk_poller(tmp_path, ttt, *, ttt_enabled=True, **procs):
    return TTTPoller(
        storage_path=str(tmp_path),
        config=_mk_config(ttt_enabled=ttt_enabled),
        ttt_client=ttt,
        **procs,
    )


# ---------------------------------------------------------------------------
# Gates: ttt.enabled + is_authenticated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_ttt_skips_every_feature(tmp_path):
    ttt = _mk_ttt()
    p_clip = _mk_processor()
    poller = _mk_poller(tmp_path, ttt, ttt_enabled=False, clip_request_processor=p_clip)
    await poller.discover_work()
    p_clip.add_work.assert_not_called()


@pytest.mark.asyncio
async def test_unauthenticated_skips_every_feature(tmp_path):
    ttt = _mk_ttt(authenticated=False)
    p_clip = _mk_processor()
    poller = _mk_poller(tmp_path, ttt, clip_request_processor=p_clip)
    await poller.discover_work()
    p_clip.add_work.assert_not_called()


# ---------------------------------------------------------------------------
# Per-feature enqueue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clip_request_poll_enqueues_pending(tmp_path):
    ttt = _mk_ttt()
    ttt.get_pending_clip_requests.return_value = [{"id": "c1"}, {"id": "c2"}]
    p_clip = _mk_processor()
    poller = _mk_poller(tmp_path, ttt, clip_request_processor=p_clip)

    await poller.discover_work()

    assert p_clip.add_work.await_count == 2
    enqueued = [call.args[0] for call in p_clip.add_work.await_args_list]
    assert all(isinstance(t, ClipRequestTask) for t in enqueued)
    assert sorted(t.ttt_id for t in enqueued) == ["c1", "c2"]


@pytest.mark.asyncio
async def test_highlight_reel_poll_enqueues_pending(tmp_path):
    ttt = _mk_ttt()
    ttt.get_pending_highlights.return_value = [{"id": "r1"}]
    p_hl = _mk_processor()
    poller = _mk_poller(tmp_path, ttt, highlight_reel_processor=p_hl)

    await poller.discover_work()

    p_hl.add_work.assert_awaited_once()
    task = p_hl.add_work.await_args.args[0]
    assert isinstance(task, HighlightReelTask)
    assert task.ttt_id == "r1"


@pytest.mark.asyncio
async def test_ttt_job_poll_registers_service_then_enqueues(tmp_path):
    ttt = _mk_ttt()
    ttt.register_service.return_value = {"id": "svc-1"}
    ttt.get_pending_jobs.return_value = [{"id": "j1"}]
    p_job = _mk_processor()
    poller = _mk_poller(tmp_path, ttt, ttt_job_processor=p_job)

    await poller.discover_work()

    ttt.register_service.assert_called_once()
    p_job.add_work.assert_awaited_once()
    task = p_job.add_work.await_args.args[0]
    assert isinstance(task, TTTJobTask)
    assert task.ttt_id == "j1"


@pytest.mark.asyncio
async def test_reprocess_pending_enqueues_task(tmp_path):
    ttt = _mk_ttt()
    ttt.get_reprocess_queue.return_value = [
        {"id": "rp1", "status": "pending"},
    ]
    p_rp = _mk_processor()
    poller = _mk_poller(tmp_path, ttt, reprocess_request_processor=p_rp)

    await poller.discover_work()

    p_rp.add_work.assert_awaited_once()
    task = p_rp.add_work.await_args.args[0]
    assert isinstance(task, ReprocessRequestTask)
    assert task.ttt_id == "rp1"


# ---------------------------------------------------------------------------
# Reprocess in-flight: cancel + progress reporting
# ---------------------------------------------------------------------------


def _seed_reprocess(storage, file_group: str, state_status: str):
    g = storage / file_group
    g.mkdir(parents=True)
    (g / "state.json").write_text(json.dumps({"status": state_status}))
    return g


@pytest.mark.asyncio
async def test_reprocess_cancel_writes_marker(tmp_path):
    group = _seed_reprocess(tmp_path, "2026.06.06-15.01.33", "running")
    ttt = _mk_ttt()
    ttt.get_reprocess_queue.return_value = [
        {
            "id": "rp1",
            "status": "running",
            "recording_id": "rec-1",
            "cancel_requested": True,
        }
    ]
    ttt.get_camera_recording.return_value = {
        "id": "rec-1",
        "file_group": "2026.06.06-15.01.33",
    }
    p_rp = _mk_processor()
    poller = _mk_poller(tmp_path, ttt, reprocess_request_processor=p_rp)

    await poller.discover_work()

    assert (group / "cancel_request.json").exists()
    p_rp.add_work.assert_not_called()


@pytest.mark.asyncio
async def test_reprocess_progress_reports_when_running(tmp_path):
    group = _seed_reprocess(tmp_path, "2026.06.06-15.01.33", "processing")
    (group / "pipeline_state.json").write_text(
        json.dumps({"steps": [{"step_id": "stabilize", "status": "running"}]})
    )
    ttt = _mk_ttt()
    ttt.get_reprocess_queue.return_value = [
        {
            "id": "rp1",
            "status": "claimed",
            "recording_id": "rec-1",
            "cancel_requested": False,
        }
    ]
    ttt.get_camera_recording.return_value = {
        "id": "rec-1",
        "file_group": "2026.06.06-15.01.33",
    }
    p_rp = _mk_processor()
    poller = _mk_poller(tmp_path, ttt, reprocess_request_processor=p_rp)

    await poller.discover_work()

    ttt.update_reprocess_status.assert_called_once()
    args, _ = ttt.update_reprocess_status.call_args
    assert args[0] == "rp1"
    assert args[1] == "running"
    assert args[2] == "stabilize"


# ---------------------------------------------------------------------------
# Exception isolation between features
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_feature_raising_does_not_starve_others(tmp_path, caplog):
    ttt = _mk_ttt()
    ttt.get_pending_clip_requests.side_effect = RuntimeError("kaboom")
    ttt.get_pending_highlights.return_value = [{"id": "r1"}]
    p_clip = _mk_processor()
    p_hl = _mk_processor()
    poller = _mk_poller(
        tmp_path,
        ttt,
        clip_request_processor=p_clip,
        highlight_reel_processor=p_hl,
    )

    await poller.discover_work()

    assert any("clip_requests" in rec.message for rec in caplog.records)
    p_hl.add_work.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_error_propagates(tmp_path):
    ttt = _mk_ttt()
    ttt.get_pending_clip_requests.side_effect = asyncio.CancelledError()
    p_clip = _mk_processor()
    poller = _mk_poller(tmp_path, ttt, clip_request_processor=p_clip)

    with pytest.raises(asyncio.CancelledError):
        await poller.discover_work()
