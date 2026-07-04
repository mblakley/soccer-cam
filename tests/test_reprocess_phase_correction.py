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
    args, kwargs = ttt.update_game_session_phases.call_args
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
    ttt.update_game_session_phases.assert_not_called()


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
    ttt.update_game_session_phases.assert_not_called()
    assert ttt.update_reprocess_status.call_args[0][1] == "completed"


@pytest.mark.asyncio
async def test_verify_phases_calls_callback_and_completes(tmp_path: Path):
    group = _seed_group(tmp_path, "grp-1")
    ttt = _mk_ttt()
    ttt.claim_reprocess_request.return_value = {
        "id": "req-v",
        "recording_id": "rec-1",
        "kind": "verify_phases",
    }
    ttt.get_camera_recording.return_value = {"id": "rec-1", "file_group": "grp-1"}
    called = {}

    async def _cb(gd):
        called["group_dir"] = gd

    proc = _mk_processor(tmp_path, ttt)
    proc.on_verify_phases = _cb

    await proc.process_item(
        ReprocessRequestTask(ttt_id="req-v", payload={"id": "req-v"})
    )

    assert str(called["group_dir"]) == str(group)
    assert ttt.update_reprocess_status.call_args[0][1] == "completed"
    ttt.update_game_session_phases.assert_not_called()


@pytest.mark.asyncio
async def test_verify_phases_without_callback_reports_failure(tmp_path: Path):
    _seed_group(tmp_path, "grp-1")
    ttt = _mk_ttt()
    ttt.claim_reprocess_request.return_value = {
        "id": "req-v",
        "recording_id": "rec-1",
        "kind": "verify_phases",
    }
    ttt.get_camera_recording.return_value = {"id": "rec-1", "file_group": "grp-1"}
    proc = _mk_processor(tmp_path, ttt)  # on_verify_phases stays None

    await proc.process_item(
        ReprocessRequestTask(ttt_id="req-v", payload={"id": "req-v"})
    )

    assert ttt.update_reprocess_status.call_args[0][1] == "failed"


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
    ttt.update_game_session_phases.assert_not_called()


@pytest.mark.asyncio
async def test_truncated_start_reruns_and_repushes(tmp_path: Path, monkeypatch):
    """kind=truncated_start -> re-run detector (truncated_start=True), trim 0,
    persist + re-push phases carrying phase_truncated_start, report completion."""
    group = _seed_group(tmp_path, "grp-1")
    (group / "combined.mp4").write_bytes(b"x")
    ttt = _mk_ttt()
    ttt.claim_reprocess_request.return_value = _claim(
        kind="truncated_start", phases=None
    )
    ttt.get_camera_recording.return_value = {"id": "rec-1", "file_group": "grp-1"}
    ttt.get_game_session_by_dir.return_value = {"id": "sess-1"}

    import video_grouper.task_processors.phase_game_start as pgs

    seen = {}

    async def fake_run(group_dir, video, *, truncated_start=False, truncated_end=False):
        seen["ts"], seen["te"] = truncated_start, truncated_end
        return {
            "ok": True,
            "times": {"kickoff": 0.0, "halftime": 2000.0, "end": 5000.0},
            "truncated_start": True,
            "truncated_end": False,
        }

    monkeypatch.setattr(pgs, "_run_detector", fake_run)
    proc = _mk_processor(tmp_path, ttt)
    await proc.process_item(
        ReprocessRequestTask(ttt_id="req-1", payload={"id": "req-1"})
    )

    assert seen == {"ts": True, "te": False}
    gp = json.loads((group / "state.json").read_text())["game_phases"]
    assert gp["truncated_start"] is True
    assert gp["times"]["kickoff"] == 0.0
    _, kwargs = ttt.update_game_session_phases.call_args
    assert kwargs["phase_truncated_start"] is True
    assert kwargs["phase_kickoff_offset"] == 0.0
    assert not (group / "reprocess_request.json").exists()  # not the stabilization path
    sargs, _ = ttt.update_reprocess_status.call_args
    assert sargs[1] == "completed"


@pytest.mark.asyncio
async def test_truncated_end_reruns_with_end_flag(tmp_path: Path, monkeypatch):
    """kind=truncated_end -> re-run detector with truncated_end=True; re-push carries
    phase_truncated_end. The start trim is left alone."""
    group = _seed_group(tmp_path, "grp-1")
    (group / "combined.mp4").write_bytes(b"x")
    ttt = _mk_ttt()
    ttt.claim_reprocess_request.return_value = _claim(kind="truncated_end", phases=None)
    ttt.get_camera_recording.return_value = {"id": "rec-1", "file_group": "grp-1"}
    ttt.get_game_session_by_dir.return_value = {"id": "sess-1"}

    import video_grouper.task_processors.phase_game_start as pgs

    seen = {}

    async def fake_run(group_dir, video, *, truncated_start=False, truncated_end=False):
        seen["ts"], seen["te"] = truncated_start, truncated_end
        return {
            "ok": True,
            "times": {"kickoff": 60.0, "end": 5400.0},
            "truncated_start": False,
            "truncated_end": True,
        }

    monkeypatch.setattr(pgs, "_run_detector", fake_run)
    proc = _mk_processor(tmp_path, ttt)
    await proc.process_item(
        ReprocessRequestTask(ttt_id="req-1", payload={"id": "req-1"})
    )

    assert seen == {"ts": False, "te": True}
    _, kwargs = ttt.update_game_session_phases.call_args
    assert kwargs["phase_truncated_end"] is True
    sargs, _ = ttt.update_reprocess_status.call_args
    assert sargs[1] == "completed"


# ---------------------------------------------------------------------------
# Restart dispatch tests (kind="restart")
# ---------------------------------------------------------------------------


def _restart_claim(from_step: str, config_preset: str | None = None, **over):
    base = {
        "id": "req-r",
        "status": "claimed",
        "recording_id": "rec-1",
        "kind": "restart",
        "phases": {"from_step": from_step},
        "requested_by": "u",
    }
    if config_preset is not None:
        base["phases"]["config_preset"] = config_preset
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_restart_trim_sets_combined_state(tmp_path: Path, monkeypatch):
    """kind=restart from_step=trim -> state set to 'combined' so TrimTask re-queues."""
    group = _seed_group(tmp_path, "grp-1", status="pipeline_complete")
    ttt = _mk_ttt()
    ttt.claim_reprocess_request.return_value = _restart_claim("trim")
    ttt.get_camera_recording.return_value = {"id": "rec-1", "file_group": "grp-1"}

    import video_grouper.models as models_mod

    class _FakeMI:
        def is_populated(self):
            return True

    monkeypatch.setattr(
        models_mod,
        "MatchInfo",
        type(
            "MatchInfo",
            (),
            {"get_or_create": staticmethod(lambda gd, **kw: (_FakeMI(), None))},
        ),
    )

    proc = _mk_processor(tmp_path, ttt)
    await proc.process_item(
        ReprocessRequestTask(ttt_id="req-r", payload={"id": "req-r"})
    )

    state = json.loads((group / "state.json").read_text())
    assert state["status"] == "combined"
    sargs, _ = ttt.update_reprocess_status.call_args
    assert sargs[1] == "completed"
    assert not (group / "reprocess_request.json").exists()


@pytest.mark.asyncio
async def test_restart_trim_fails_when_match_info_not_populated(
    tmp_path: Path, monkeypatch
):
    """from_step=trim with unpopulated MatchInfo reports failure (not crash)."""
    _seed_group(tmp_path, "grp-1")
    ttt = _mk_ttt()
    ttt.claim_reprocess_request.return_value = _restart_claim("trim")
    ttt.get_camera_recording.return_value = {"id": "rec-1", "file_group": "grp-1"}

    import video_grouper.models as models_mod

    class _EmptyMI:
        def is_populated(self):
            return False

    monkeypatch.setattr(
        models_mod,
        "MatchInfo",
        type(
            "MatchInfo",
            (),
            {"get_or_create": staticmethod(lambda gd, **kw: (_EmptyMI(), None))},
        ),
    )

    proc = _mk_processor(tmp_path, ttt)
    await proc.process_item(
        ReprocessRequestTask(ttt_id="req-r", payload={"id": "req-r"})
    )

    sargs, _ = ttt.update_reprocess_status.call_args
    assert sargs[1] == "failed"


@pytest.mark.asyncio
async def test_restart_pipeline_step_invalidates_manifest_and_queues(
    tmp_path: Path, monkeypatch
):
    """from_step=ball_detect -> manifest invalidated + state -> pipeline_queued_reprocess."""
    import json as _json

    group = _seed_group(tmp_path, "grp-1", status="pipeline_complete")
    ttt = _mk_ttt()
    ttt.claim_reprocess_request.return_value = _restart_claim("ball_detect")
    ttt.get_camera_recording.return_value = {"id": "rec-1", "file_group": "grp-1"}

    # Seed a minimal pipeline_state.json with ball_detect + render steps
    manifest_data = {
        "version": 1,
        "input_path": str(group / "combined.mp4"),
        "output_path": str(group / "out.mp4"),
        "artifacts": {
            "input_path": str(group / "combined.mp4"),
            "output_path": str(group / "out.mp4"),
        },
        "steps": [
            {
                "step_id": "field_detect",
                "type": "field_detect",
                "status": "complete",
                "config_fingerprint": "fp1",
            },
            {
                "step_id": "ball_detect",
                "type": "ball_detect",
                "status": "complete",
                "config_fingerprint": "fp2",
            },
            {
                "step_id": "render",
                "type": "render",
                "status": "complete",
                "config_fingerprint": "fp3",
            },
        ],
    }
    (group / "pipeline_state.json").write_text(_json.dumps(manifest_data))

    proc = _mk_processor(tmp_path, ttt)
    await proc.process_item(
        ReprocessRequestTask(ttt_id="req-r", payload={"id": "req-r"})
    )

    # Manifest: field_detect stays complete; ball_detect + render reset to pending
    saved = _json.loads((group / "pipeline_state.json").read_text())
    steps = {s["step_id"]: s for s in saved["steps"]}
    assert steps["field_detect"]["status"] == "complete"
    assert steps["ball_detect"]["status"] == "pending"
    assert "config_fingerprint" not in steps["ball_detect"]
    assert steps["render"]["status"] == "pending"

    # State -> pipeline_queued_reprocess
    state = _json.loads((group / "state.json").read_text())
    assert state["status"] == "pipeline_queued_reprocess"

    # reprocess_request.json written
    rr = _json.loads((group / "reprocess_request.json").read_text())
    assert "ttt:restart:ball_detect" in rr["requested_by"]

    sargs, _ = ttt.update_reprocess_status.call_args
    assert sargs[1] == "completed"


@pytest.mark.asyncio
async def test_restart_pipeline_step_with_preset(tmp_path: Path, monkeypatch):
    """config_preset is validated and stored in reprocess_request.json."""
    import json as _json

    group = _seed_group(tmp_path, "grp-1", status="pipeline_complete")
    ttt = _mk_ttt()
    ttt.claim_reprocess_request.return_value = _restart_claim(
        "render", config_preset="homegrown"
    )
    ttt.get_camera_recording.return_value = {"id": "rec-1", "file_group": "grp-1"}

    proc = _mk_processor(tmp_path, ttt)
    await proc.process_item(
        ReprocessRequestTask(ttt_id="req-r", payload={"id": "req-r"})
    )

    rr = _json.loads((group / "reprocess_request.json").read_text())
    assert rr.get("config_preset") == "homegrown"
    sargs, _ = ttt.update_reprocess_status.call_args
    assert sargs[1] == "completed"


@pytest.mark.asyncio
async def test_restart_pipeline_step_invalid_preset_reports_failure(tmp_path: Path):
    """An unknown preset name must report failure, not crash."""
    _seed_group(tmp_path, "grp-1", status="pipeline_complete")
    ttt = _mk_ttt()
    ttt.claim_reprocess_request.return_value = _restart_claim(
        "render", config_preset="nonexistent_preset"
    )
    ttt.get_camera_recording.return_value = {"id": "rec-1", "file_group": "grp-1"}

    proc = _mk_processor(tmp_path, ttt)
    await proc.process_item(
        ReprocessRequestTask(ttt_id="req-r", payload={"id": "req-r"})
    )

    sargs, _ = ttt.update_reprocess_status.call_args
    assert sargs[1] == "failed"


@pytest.mark.asyncio
async def test_restart_upload_sets_pipeline_complete(tmp_path: Path):
    """from_step=upload -> state -> pipeline_complete for upload recovery."""
    group = _seed_group(tmp_path, "grp-1", status="complete")
    ttt = _mk_ttt()
    ttt.claim_reprocess_request.return_value = _restart_claim("upload")
    ttt.get_camera_recording.return_value = {"id": "rec-1", "file_group": "grp-1"}

    proc = _mk_processor(tmp_path, ttt)
    await proc.process_item(
        ReprocessRequestTask(ttt_id="req-r", payload={"id": "req-r"})
    )

    state = json.loads((group / "state.json").read_text())
    assert state["status"] == "pipeline_complete"
    sargs, _ = ttt.update_reprocess_status.call_args
    assert sargs[1] == "completed"


@pytest.mark.asyncio
async def test_restart_missing_from_step_reports_failure(tmp_path: Path):
    """A restart request with no from_step must fail cleanly."""
    _seed_group(tmp_path, "grp-1")
    ttt = _mk_ttt()
    bad_claim = {
        "id": "req-r",
        "recording_id": "rec-1",
        "kind": "restart",
        "phases": {},  # no from_step
    }
    ttt.claim_reprocess_request.return_value = bad_claim
    ttt.get_camera_recording.return_value = {"id": "rec-1", "file_group": "grp-1"}

    proc = _mk_processor(tmp_path, ttt)
    await proc.process_item(
        ReprocessRequestTask(ttt_id="req-r", payload={"id": "req-r"})
    )

    sargs, _ = ttt.update_reprocess_status.call_args
    assert sargs[1] == "failed"


@pytest.mark.asyncio
async def test_restart_completion_always_reported(tmp_path: Path):
    """Completion status is always reported, even for unknown step IDs."""
    _seed_group(tmp_path, "grp-1", status="pipeline_complete")
    ttt = _mk_ttt()
    # "upload" is a known step that always succeeds
    ttt.claim_reprocess_request.return_value = _restart_claim("upload")
    ttt.get_camera_recording.return_value = {"id": "rec-1", "file_group": "grp-1"}

    proc = _mk_processor(tmp_path, ttt)
    await proc.process_item(
        ReprocessRequestTask(ttt_id="req-r", payload={"id": "req-r"})
    )

    ttt.update_reprocess_status.assert_called_once()
    sargs, _ = ttt.update_reprocess_status.call_args
    assert sargs[0] == "req-r"
    assert sargs[1] == "completed"
