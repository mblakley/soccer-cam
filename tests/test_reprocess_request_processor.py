"""Tests for the soccer-cam side of the cross-network reprocess flow.

The processor handles the discrete "claim + write marker" work, against the
work-source interface (FakeWorkSource). Cancel propagation + progress
reporting for in-flight rows are owned by TTTPoller — their tests live in
test_ttt_poller.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from video_grouper.integrations.work import REPROCESS
from video_grouper.integrations.work.fake import FakeWorkSource, WorkCall
from video_grouper.integrations.work.sources.ttt import TTTWorkSource
from video_grouper.task_processors.reprocess_request_processor import (
    ReprocessRequestProcessor,
)
from video_grouper.task_processors.tasks.ttt.reprocess_request_task import (
    ReprocessRequestTask,
)

_GROUP = "2026.06.06-15.01.33"


@pytest.fixture(autouse=True)
def mock_ffmpeg():  # noqa: PT004
    yield None


@pytest.fixture(autouse=True)
def mock_file_system():  # noqa: PT004
    yield None


def _mk_processor(storage_path, work=None):
    return ReprocessRequestProcessor(
        storage_path=str(storage_path),
        config=MagicMock(),
        work=work,
        resource_manager=None,
    )


def _claimed(**over):
    row = {
        "id": "req-1",
        "status": "claimed",
        "recording_id": "rec-1",
        "stabilization_strength": "extreme",
        "skip_detect": True,
        "cancel_requested": False,
        "created_at": "2026-06-10T12:00:00Z",
        "requested_by": "user-uuid",
    }
    row.update(over)
    return row


def _mk_work(claimed=None, file_group=_GROUP, **kw):
    claimed = claimed if claimed is not None else _claimed()
    return FakeWorkSource(
        claims={"req-1": claimed},
        details={"req-1": {"recording": {"id": "rec-1", "file_group": file_group}}},
        **kw,
    )


def _seed_group(storage, file_group: str, state_status: str = "pipeline_complete"):
    g = storage / file_group
    g.mkdir(parents=True)
    (g / "state.json").write_text(json.dumps({"status": state_status}))
    return g


def _task():
    return ReprocessRequestTask(ttt_id="req-1", payload={"id": "req-1"})


# ---------------------------------------------------------------------------
# process_item happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_item_claims_and_writes_marker(tmp_path: Path):
    group = _seed_group(tmp_path, _GROUP)
    work = _mk_work()
    proc = _mk_processor(tmp_path, work)

    await proc.process_item(_task())

    # claim, then the recording of the claimed request; a stabilization
    # request reports nothing more (the pipeline picks the marker up).
    assert work.calls == [
        WorkCall("claim", REPROCESS, "req-1"),
        WorkCall("details", REPROCESS, "req-1"),
    ]
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
    _seed_group(tmp_path, _GROUP)
    work = _mk_work(errors={"claim": RuntimeError("409 already claimed")})
    proc = _mk_processor(tmp_path, work)

    await proc.process_item(_task())

    assert not (tmp_path / _GROUP / "reprocess_request.json").exists()
    assert work.called("details") == []


@pytest.mark.asyncio
async def test_process_item_lost_claim_is_quiet(tmp_path: Path):
    _seed_group(tmp_path, _GROUP)
    work = FakeWorkSource(claims={"req-1": None})
    proc = _mk_processor(tmp_path, work)

    await proc.process_item(_task())

    assert not (tmp_path / _GROUP / "reprocess_request.json").exists()
    assert [c.verb for c in work.calls] == ["claim"]


@pytest.mark.asyncio
async def test_process_item_missing_local_dir_reports_failure(tmp_path: Path):
    # The recording's file_group is not present locally.
    work = _mk_work(_claimed(stabilization_strength="heavy"))
    proc = _mk_processor(tmp_path, work)

    await proc.process_item(_task())

    (call,) = work.called("fail")
    assert call.id == "req-1"
    assert "local" in call.args[0].lower()


@pytest.mark.asyncio
async def test_process_item_recording_fetch_error_reports_failure(tmp_path: Path):
    _seed_group(tmp_path, _GROUP)
    work = _mk_work(errors={"details": RuntimeError("404")})
    proc = _mk_processor(tmp_path, work)

    await proc.process_item(_task())

    assert len(work.called("fail")) == 1
    assert not (tmp_path / _GROUP / "reprocess_request.json").exists()


@pytest.mark.asyncio
async def test_failure_report_error_is_logged_not_raised(tmp_path: Path):
    work = _mk_work(errors={"fail": RuntimeError("502")})
    proc = _mk_processor(tmp_path, work)

    await proc.process_item(_task())  # must not raise

    assert len(work.called("fail")) == 1


@pytest.mark.asyncio
async def test_no_work_source_claims_nothing(tmp_path: Path):
    """With TTT off the processor has the Null work source: nothing to claim,
    nothing written."""
    _seed_group(tmp_path, _GROUP)
    proc = _mk_processor(tmp_path)

    await proc.process_item(_task())

    assert not (tmp_path / _GROUP / "reprocess_request.json").exists()


@pytest.mark.asyncio
async def test_through_the_ttt_work_source(tmp_path: Path):
    """Wiring: the recording is looked up from the CLAIMED row (the queued
    payload carries only the id) and the TTT calls are the ones made before
    the port."""
    group = _seed_group(tmp_path, _GROUP)
    client = MagicMock()
    client.claim_reprocess_request.return_value = _claimed(kind="restart", phases={})
    client.get_camera_recording.return_value = {"id": "rec-1", "file_group": _GROUP}
    proc = _mk_processor(tmp_path, TTTWorkSource(client, SimpleNamespace()))

    await proc.process_item(_task())

    client.claim_reprocess_request.assert_called_once_with("req-1")
    client.get_camera_recording.assert_called_once_with("rec-1")
    client.update_reprocess_status.assert_called_once_with(
        "req-1",
        "failed",
        None,
        "restart request missing required from_step field",
    )
    assert not (group / "reprocess_request.json").exists()


# ---------------------------------------------------------------------------
# add_work dedup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_work_dedups_on_ttt_id(tmp_path: Path):
    proc = _mk_processor(tmp_path, FakeWorkSource())
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
