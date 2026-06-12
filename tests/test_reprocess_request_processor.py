"""Tests for the soccer-cam side of the cross-network reprocess flow.

The processor handles the discrete "claim + write marker" work.
Cancel propagation + progress reporting for in-flight rows are now
owned by TTTPoller — their tests live in test_ttt_poller.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from video_grouper.task_processors.reprocess_request_processor import (
    ReprocessRequestProcessor,
)
from video_grouper.task_processors.tasks.ttt.reprocess_request_task import (
    ReprocessRequestTask,
)


@pytest.fixture(autouse=True)
def mock_ffmpeg():  # noqa: PT004
    yield None


@pytest.fixture(autouse=True)
def mock_file_system():  # noqa: PT004
    yield None


def _mk_processor(storage_path, ttt):
    return ReprocessRequestProcessor(
        storage_path=str(storage_path),
        config=MagicMock(),
        ttt_client=ttt,
        resource_manager=None,
    )


def _mk_ttt():
    c = MagicMock()
    c.is_authenticated.return_value = True
    return c


def _seed_group(storage, file_group: str, state_status: str = "pipeline_complete"):
    g = storage / file_group
    g.mkdir(parents=True)
    (g / "state.json").write_text(json.dumps({"status": state_status}))
    return g


# ---------------------------------------------------------------------------
# process_item happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_item_claims_and_writes_marker(tmp_path: Path):
    group = _seed_group(tmp_path, "2026.06.06-15.01.33")
    ttt = _mk_ttt()
    ttt.claim_reprocess_request.return_value = {
        "id": "req-1",
        "status": "claimed",
        "recording_id": "rec-1",
        "stabilization_strength": "extreme",
        "skip_detect": True,
        "cancel_requested": False,
        "created_at": "2026-06-10T12:00:00Z",
        "requested_by": "user-uuid",
    }
    ttt.get_camera_recording.return_value = {
        "id": "rec-1",
        "file_group": "2026.06.06-15.01.33",
    }
    proc = _mk_processor(tmp_path, ttt)
    task = ReprocessRequestTask(ttt_id="req-1", payload={"id": "req-1"})

    await proc.process_item(task)

    ttt.claim_reprocess_request.assert_called_once_with("req-1")
    local = json.loads((group / "reprocess_request.json").read_text())
    assert local["stabilization_strength"] == "extreme"
    assert local["skip_detect"] is True
    assert local["requested_by"] == "ttt:user-uuid"
    state = json.loads((group / "state.json").read_text())
    assert state["status"] == "pipeline_queued_reprocess"


@pytest.mark.asyncio
async def test_process_item_claim_failure_is_quiet(tmp_path: Path):
    """If another camera-manager won the claim race, process_item
    returns without writing anything — not a crash."""
    _seed_group(tmp_path, "2026.06.06-15.01.33")
    ttt = _mk_ttt()
    ttt.claim_reprocess_request.side_effect = RuntimeError("409 already claimed")
    proc = _mk_processor(tmp_path, ttt)

    await proc.process_item(
        ReprocessRequestTask(ttt_id="req-1", payload={"id": "req-1"})
    )

    assert not (tmp_path / "2026.06.06-15.01.33" / "reprocess_request.json").exists()
    ttt.get_camera_recording.assert_not_called()


@pytest.mark.asyncio
async def test_process_item_missing_local_dir_reports_failure(tmp_path: Path):
    ttt = _mk_ttt()
    ttt.claim_reprocess_request.return_value = {
        "id": "req-1",
        "recording_id": "rec-1",
        "stabilization_strength": "heavy",
        "skip_detect": True,
        "cancel_requested": False,
        "requested_by": "u",
    }
    ttt.get_camera_recording.return_value = {
        "id": "rec-1",
        "file_group": "2026.06.06-15.01.33",  # not present locally
    }
    proc = _mk_processor(tmp_path, ttt)

    await proc.process_item(
        ReprocessRequestTask(ttt_id="req-1", payload={"id": "req-1"})
    )

    ttt.update_reprocess_status.assert_called_once()
    args, _ = ttt.update_reprocess_status.call_args
    assert args[0] == "req-1"
    assert args[1] == "failed"
    assert "local" in args[3].lower()


@pytest.mark.asyncio
async def test_process_item_raises_without_ttt_client(tmp_path: Path):
    proc = ReprocessRequestProcessor(
        storage_path=str(tmp_path), config=MagicMock(), ttt_client=None
    )
    with pytest.raises(RuntimeError, match="ttt_client"):
        await proc.process_item(
            ReprocessRequestTask(ttt_id="req-1", payload={"id": "req-1"})
        )


# ---------------------------------------------------------------------------
# add_work dedup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_work_dedups_on_ttt_id(tmp_path: Path):
    proc = _mk_processor(tmp_path, _mk_ttt())
    await proc.add_work(ReprocessRequestTask(ttt_id="r-1", payload={"id": "r-1"}))
    await proc.add_work(ReprocessRequestTask(ttt_id="r-1", payload={"id": "r-1"}))
    await proc.add_work(ReprocessRequestTask(ttt_id="r-2", payload={"id": "r-2"}))
    assert proc._queue.qsize() == 2


# ---------------------------------------------------------------------------
# Task round-trip
# ---------------------------------------------------------------------------


def test_task_round_trip():
    task = ReprocessRequestTask(ttt_id="r-1", payload={"id": "r-1", "x": "y"})
    data = task.serialize()
    assert data["task_type"] == "ttt_reprocess_request"
    restored = ReprocessRequestTask.deserialize(data)
    assert restored.ttt_id == "r-1"
    assert restored.payload == {"id": "r-1", "x": "y"}
