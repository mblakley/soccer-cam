"""Tests for the soccer-cam side of the cross-network reprocess flow.

The processor's job is small: translate rows in the TTT
``reprocess_requests`` table into files on the local filesystem that
the existing pipeline-runner re-entry mechanism already honors, and
translate the resulting local state-transitions back into TTT status
updates. These tests assert that translation per-scenario.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from video_grouper.task_processors.reprocess_request_processor import (
    ReprocessRequestProcessor,
)


@pytest.fixture(autouse=True)
def mock_ffmpeg():  # noqa: PT004 — override conftest's autouse
    yield None


@pytest.fixture(autouse=True)
def mock_file_system():  # noqa: PT004
    yield None


def _mk_processor(storage_path, ttt_client, config=None):
    config = config or MagicMock()
    p = ReprocessRequestProcessor(
        storage_path=str(storage_path), config=config, ttt_client=ttt_client
    )
    return p


def _mk_ttt(authenticated=True):
    c = MagicMock()
    c.is_authenticated.return_value = authenticated
    return c


def _seed_group(storage, file_group: str, state_status: str = "pipeline_complete"):
    g = storage / file_group
    g.mkdir(parents=True)
    (g / "state.json").write_text(json.dumps({"status": state_status}))
    return g


# ---------------------------------------------------------------------------
# Pending → claim → write reprocess_request.json
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_request_is_claimed_and_written_locally(tmp_path: Path):
    group = _seed_group(tmp_path, "2026.06.06-15.01.33")
    ttt = _mk_ttt()
    ttt.get_reprocess_queue.return_value = [
        {
            "id": "req-1",
            "status": "pending",
            "recording_id": "rec-1",
            "stabilization_strength": "extreme",
            "skip_detect": True,
            "cancel_requested": False,
        }
    ]
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
    p = _mk_processor(tmp_path, ttt)

    await p.discover_work()

    ttt.claim_reprocess_request.assert_called_once_with("req-1")
    local_request = json.loads((group / "reprocess_request.json").read_text())
    assert local_request["stabilization_strength"] == "extreme"
    assert local_request["skip_detect"] is True
    assert local_request["requested_by"] == "ttt:user-uuid"
    # State was nudged off the terminal status so the discovery loop re-queues.
    state = json.loads((group / "state.json").read_text())
    assert state["status"] == "pipeline_queued_reprocess"


@pytest.mark.asyncio
async def test_failed_claim_does_not_write_local_files(tmp_path: Path):
    """If another camera-manager won the claim race, the processor must
    not leave a half-applied local file."""
    _seed_group(tmp_path, "2026.06.06-15.01.33")
    ttt = _mk_ttt()
    ttt.get_reprocess_queue.return_value = [
        {
            "id": "req-1",
            "status": "pending",
            "recording_id": "rec-1",
            "stabilization_strength": "heavy",
            "skip_detect": True,
            "cancel_requested": False,
        }
    ]
    ttt.claim_reprocess_request.side_effect = RuntimeError("409 already claimed")
    p = _mk_processor(tmp_path, ttt)

    await p.discover_work()

    assert not (tmp_path / "2026.06.06-15.01.33" / "reprocess_request.json").exists()
    ttt.get_camera_recording.assert_not_called()


@pytest.mark.asyncio
async def test_missing_local_dir_reports_failure_back_to_ttt(tmp_path: Path):
    """If TTT thinks we have the recording but we don't, report it as a
    failure so the user sees something other than a silent stuck state."""
    ttt = _mk_ttt()
    ttt.get_reprocess_queue.return_value = [
        {
            "id": "req-1",
            "status": "pending",
            "recording_id": "rec-1",
            "stabilization_strength": "heavy",
            "skip_detect": True,
            "cancel_requested": False,
        }
    ]
    ttt.claim_reprocess_request.return_value = {
        "id": "req-1",
        "status": "claimed",
        "recording_id": "rec-1",
        "stabilization_strength": "heavy",
        "skip_detect": True,
        "cancel_requested": False,
        "requested_by": "user-uuid",
    }
    ttt.get_camera_recording.return_value = {
        "id": "rec-1",
        "file_group": "2026.06.06-15.01.33",  # not present locally
    }
    p = _mk_processor(tmp_path, ttt)

    await p.discover_work()

    ttt.update_reprocess_status.assert_called_once()
    args, _ = ttt.update_reprocess_status.call_args
    assert args[0] == "req-1"
    assert args[1] == "failed"
    assert "local" in args[3].lower()


@pytest.mark.asyncio
async def test_unauthenticated_skips_poll(tmp_path: Path):
    ttt = _mk_ttt(authenticated=False)
    p = _mk_processor(tmp_path, ttt)
    await p.discover_work()
    ttt.get_reprocess_queue.assert_not_called()


# ---------------------------------------------------------------------------
# Cancel propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_requested_writes_local_marker(tmp_path: Path):
    group = _seed_group(tmp_path, "2026.06.06-15.01.33", state_status="running")
    ttt = _mk_ttt()
    # Already-claimed request now has cancel_requested set. The processor
    # didn't track it (e.g. tray restart), so it has to re-resolve.
    ttt.get_reprocess_queue.return_value = [
        {
            "id": "req-1",
            "status": "running",
            "recording_id": "rec-1",
            "stabilization_strength": "extreme",
            "skip_detect": True,
            "cancel_requested": True,
        }
    ]
    ttt.get_camera_recording.return_value = {
        "id": "rec-1",
        "file_group": "2026.06.06-15.01.33",
    }
    p = _mk_processor(tmp_path, ttt)

    await p.discover_work()

    # cancel_request.json marker written, runner picks it up between steps.
    assert (group / "cancel_request.json").exists()


# ---------------------------------------------------------------------------
# Progress reporting back to TTT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_running_step_reported_back_to_ttt(tmp_path: Path):
    group = _seed_group(tmp_path, "2026.06.06-15.01.33", state_status="processing")
    # Simulate a running step in the pipeline manifest.
    (group / "pipeline_state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "steps": [
                    {"step_id": "stabilize", "status": "running"},
                ],
            }
        )
    )
    ttt = _mk_ttt()
    # Pre-seed the tracker so we look like we own this request.
    p = _mk_processor(tmp_path, ttt)
    p._tracked["req-1"] = {"group_dir": group, "status": "claimed"}
    ttt.get_reprocess_queue.return_value = [
        {
            "id": "req-1",
            "status": "claimed",
            "recording_id": "rec-1",
            "stabilization_strength": "extreme",
            "skip_detect": True,
            "cancel_requested": False,
        }
    ]

    await p.discover_work()

    ttt.update_reprocess_status.assert_called_once()
    args, _ = ttt.update_reprocess_status.call_args
    assert args[0] == "req-1"
    assert args[1] == "running"
    assert args[2] == "stabilize"


@pytest.mark.asyncio
async def test_pipeline_complete_reported_and_request_untracked(tmp_path: Path):
    group = _seed_group(
        tmp_path, "2026.06.06-15.01.33", state_status="pipeline_complete"
    )
    ttt = _mk_ttt()
    p = _mk_processor(tmp_path, ttt)
    p._tracked["req-1"] = {"group_dir": group, "status": "running"}
    ttt.get_reprocess_queue.return_value = [
        {
            "id": "req-1",
            "status": "running",
            "recording_id": "rec-1",
            "stabilization_strength": "heavy",
            "skip_detect": True,
            "cancel_requested": False,
        }
    ]

    await p.discover_work()

    args, _ = ttt.update_reprocess_status.call_args
    assert args[1] == "completed"
    # Once terminal, the tracker forgets it — a future poll wouldn't
    # double-report.
    assert "req-1" not in p._tracked


@pytest.mark.asyncio
async def test_pipeline_failed_reported_with_error_message(tmp_path: Path):
    group = _seed_group(tmp_path, "2026.06.06-15.01.33", state_status="pipeline_failed")
    (group / "pipeline_state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "steps": [
                    {
                        "step_id": "stabilize",
                        "status": "failed",
                        "error": "ffmpeg crash",
                    },
                ],
            }
        )
    )
    ttt = _mk_ttt()
    p = _mk_processor(tmp_path, ttt)
    p._tracked["req-1"] = {"group_dir": group, "status": "running"}
    ttt.get_reprocess_queue.return_value = [
        {
            "id": "req-1",
            "status": "running",
            "recording_id": "rec-1",
            "stabilization_strength": "heavy",
            "skip_detect": True,
            "cancel_requested": False,
        }
    ]

    await p.discover_work()

    args, _ = ttt.update_reprocess_status.call_args
    assert args[1] == "failed"
    assert "ffmpeg crash" in (args[3] or "")
