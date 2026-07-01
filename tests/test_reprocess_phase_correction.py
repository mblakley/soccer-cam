"""S4: phase_correction reprocess kind — apply a human phase edit locally + re-push to TTT.

Verifies the new `kind` dispatch: a phase_correction row overwrites the local phases
(source=human) and re-confirms them to TTT, while an ordinary (stabilization) row is
unchanged.
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
def _mocks():  # noqa: PT004
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


def _seed_group(storage, file_group, status="pipeline_complete"):
    g = storage / file_group
    g.mkdir(parents=True)
    (g / "state.json").write_text(json.dumps({"status": status}))
    return g


def _claim(**over):
    base = {
        "id": "req-1",
        "status": "claimed",
        "recording_id": "rec-1",
        "kind": "phase_correction",
        "phases": {
            "kickoff": 45.0,
            "halftime": 2880.0,
            "second_half": 3480.0,
            "end": 5640.0,
        },
        "requested_by": "u",
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_phase_correction_applies_human_phases_and_repushes(tmp_path: Path):
    group = _seed_group(tmp_path, "grp-1")
    ttt = _mk_ttt()
    ttt.claim_reprocess_request.return_value = _claim()
    ttt.get_camera_recording.return_value = {"id": "rec-1", "file_group": "grp-1"}
    ttt.get_game_session_by_dir.return_value = {"id": "sess-1"}
    proc = _mk_processor(tmp_path, ttt)

    await proc.process_item(
        ReprocessRequestTask(ttt_id="req-1", payload={"id": "req-1"})
    )

    # local phases overwritten, source=human
    state = json.loads((group / "state.json").read_text())
    gp = state["game_phases"]
    assert gp["source"] == "human"
    assert gp["times"]["kickoff"] == 45.0
    assert gp["times"]["end"] == 5640.0
    # NOT the stabilization path
    assert not (group / "reprocess_request.json").exists()
    # re-push to TTT with the human offsets
    args, kwargs = ttt.update_game_session.call_args
    assert args[0] == "sess-1"
    assert kwargs["phase_kickoff_offset"] == 45.0
    assert kwargs["phase_source"] == "human"
    # completion reported
    sargs, _ = ttt.update_reprocess_status.call_args
    assert sargs[0] == "req-1"
    assert sargs[1] == "completed"


@pytest.mark.asyncio
async def test_phase_correction_no_times_reports_failure(tmp_path: Path):
    _seed_group(tmp_path, "grp-1")
    ttt = _mk_ttt()
    ttt.claim_reprocess_request.return_value = _claim(phases={})
    ttt.get_camera_recording.return_value = {"id": "rec-1", "file_group": "grp-1"}
    proc = _mk_processor(tmp_path, ttt)

    await proc.process_item(
        ReprocessRequestTask(ttt_id="req-1", payload={"id": "req-1"})
    )

    sargs, _ = ttt.update_reprocess_status.call_args
    assert sargs[1] == "failed"
    ttt.update_game_session.assert_not_called()


@pytest.mark.asyncio
async def test_phase_correction_repush_skipped_when_no_session(tmp_path: Path):
    group = _seed_group(tmp_path, "grp-1")
    ttt = _mk_ttt()
    ttt.claim_reprocess_request.return_value = _claim()
    ttt.get_camera_recording.return_value = {"id": "rec-1", "file_group": "grp-1"}
    ttt.get_game_session_by_dir.return_value = None  # no TTT session
    proc = _mk_processor(tmp_path, ttt)

    await proc.process_item(
        ReprocessRequestTask(ttt_id="req-1", payload={"id": "req-1"})
    )

    # still applied locally + completed, just no re-push
    assert (
        json.loads((group / "state.json").read_text())["game_phases"]["source"]
        == "human"
    )
    ttt.update_game_session.assert_not_called()
    assert ttt.update_reprocess_status.call_args[0][1] == "completed"


@pytest.mark.asyncio
async def test_stabilization_row_unchanged_by_dispatch(tmp_path: Path):
    """A row with no `kind` still takes the stabilization marker path."""
    group = _seed_group(tmp_path, "grp-1")
    ttt = _mk_ttt()
    ttt.claim_reprocess_request.return_value = {
        "id": "req-2",
        "recording_id": "rec-1",
        "stabilization_strength": "heavy",
        "skip_detect": False,
        "created_at": "t",
        "requested_by": "u",
    }
    ttt.get_camera_recording.return_value = {"id": "rec-1", "file_group": "grp-1"}
    proc = _mk_processor(tmp_path, ttt)

    await proc.process_item(
        ReprocessRequestTask(ttt_id="req-2", payload={"id": "req-2"})
    )

    marker = json.loads((group / "reprocess_request.json").read_text())
    assert marker["stabilization_strength"] == "heavy"
    ttt.update_game_session.assert_not_called()
