"""TTTRecordingSink: maps recording reports onto the TTT API. Best-effort --
every failure is swallowed, nothing is called while signed out."""

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from video_grouper.integrations.recordings import StepReport
from video_grouper.integrations.recordings.sources.ttt import (
    TTTRecordingSink,
    phases_to_session_fields,
)
from video_grouper.integrations.registry import IntegrationContext


def _client(signed_in=True):
    c = MagicMock()
    c.is_authenticated.return_value = signed_in
    c.get_team_assignments.return_value = [{"team_id": "team-1"}]
    c.register_recordings.return_value = [{"id": "rec-1"}]
    c.get_game_session_by_dir.return_value = {"id": "sess-1"}
    c.auto_match_video.return_value = {"matched": True, "message": "ok"}
    return c


def _group(tmp_path, recording_id=None) -> Path:
    g = tmp_path / "2026.06.01-10.00.00"
    g.mkdir()
    state = {"status": "downloaded", "files": {}}
    if recording_id:
        state["ttt_recording_id"] = recording_id
    (g / "state.json").write_text(json.dumps(state))
    return g


def _file(name="a.mp4", start=datetime(2026, 6, 1, 10, 0)):
    return SimpleNamespace(
        file_path=f"/x/{name}",
        metadata={"file_size": 123},
        start_time=start,
        end_time=start,
    )


# -- choosing the implementation ---------------------------------------------


def test_chosen_only_when_ttt_is_enabled():
    cfg_on = SimpleNamespace(ttt=SimpleNamespace(enabled=True, camera_id="cam-1"))
    cfg_off = SimpleNamespace(ttt=SimpleNamespace(enabled=False))
    sink = TTTRecordingSink.from_context(
        IntegrationContext(cfg_on, ".", {"ttt": _client()})
    )
    assert isinstance(sink, TTTRecordingSink)
    assert TTTRecordingSink.from_context(IntegrationContext(cfg_off, ".")) is None


# -- recording_found ----------------------------------------------------------


@pytest.mark.asyncio
async def test_registers_files_and_keeps_ttts_id(tmp_path):
    group = _group(tmp_path)
    client = _client()
    await TTTRecordingSink(client, "cam-1").recording_found(str(group), [_file()])

    camera, team, rows = client.register_recordings.call_args.args
    assert (camera, team) == ("cam-1", "team-1")
    assert rows[0]["file_name"] == "a.mp4"
    assert rows[0]["file_group"] == group.name
    assert rows[0]["file_size_bytes"] == 123
    assert json.loads((group / "state.json").read_text())["ttt_recording_id"] == "rec-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "camera_id, signed_in, teams",
    [
        (None, True, [{"team_id": "t"}]),
        ("cam", False, [{"team_id": "t"}]),
        ("cam", True, []),
    ],
)
async def test_registration_skipped_without_camera_session_or_team(
    tmp_path, camera_id, signed_in, teams
):
    client = _client(signed_in)
    client.get_team_assignments.return_value = teams
    await TTTRecordingSink(client, camera_id).recording_found(
        str(_group(tmp_path)), [_file()]
    )
    client.register_recordings.assert_not_called()


@pytest.mark.asyncio
async def test_registration_errors_are_swallowed(tmp_path):
    client = _client()
    client.register_recordings.side_effect = RuntimeError("503")
    await TTTRecordingSink(client, "cam").recording_found(
        str(_group(tmp_path)), [_file()]
    )


@pytest.mark.asyncio
async def test_team_id_is_looked_up_once(tmp_path):
    client = _client()
    sink = TTTRecordingSink(client, "cam")
    await sink.recording_found(str(_group(tmp_path)), [_file()])
    await sink.published(str(tmp_path), "https://youtu.be/v", datetime(2026, 6, 1))
    assert client.get_team_assignments.call_count == 1


# -- progress -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_progress_updates_the_registered_recording(tmp_path):
    client = _client()
    report = StepReport(
        step_id="upload",
        step_type="upload",
        label="YouTube Upload",
        status="complete",
        artifacts={"youtube_video_id": "v"},
        pipeline_preset="homegrown",
    )
    await TTTRecordingSink(client, "cam").progress(
        str(_group(tmp_path, recording_id="rec-9")), report
    )
    args, kwargs = client.update_recording_step.call_args
    assert args == ("rec-9",)
    assert kwargs["status"] == "complete"
    assert kwargs["artifacts"] == {"youtube_video_id": "v"}
    assert kwargs["pipeline_preset"] == "homegrown"


@pytest.mark.asyncio
async def test_progress_without_a_registered_recording_does_nothing(tmp_path):
    client = _client()
    await TTTRecordingSink(client, "cam").progress(
        str(_group(tmp_path)), StepReport("download", "download", "Download", "running")
    )
    client.update_recording_step.assert_not_called()


@pytest.mark.asyncio
async def test_progress_errors_are_swallowed(tmp_path):
    client = _client()
    client.update_recording_step.side_effect = RuntimeError("boom")
    await TTTRecordingSink(client, "cam").progress(
        str(_group(tmp_path, recording_id="r")),
        StepReport("download", "download", "Download", "failed"),
    )


# -- published ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_published_attaches_to_the_matched_ttt_game(tmp_path):
    group = _group(tmp_path)
    (group / "match_info.ini").write_text("[MATCH]\nttt_game_id = g-42\n")
    client = _client()
    await TTTRecordingSink(client, "cam").published(
        str(group), "https://youtu.be/v", datetime(2026, 6, 1, 10, 0)
    )
    args, kwargs = client.auto_match_video.call_args
    assert args == ("team-1", "https://youtu.be/v", "2026-06-01T10:00:00")
    assert kwargs == {"game_id": "g-42"}


@pytest.mark.asyncio
async def test_published_without_a_matched_game_lets_ttt_match_on_time(tmp_path):
    client = _client()
    await TTTRecordingSink(client, "cam").published(
        str(_group(tmp_path)), "https://youtu.be/v", datetime(2026, 6, 1)
    )
    assert client.auto_match_video.call_args.kwargs == {"game_id": None}


@pytest.mark.asyncio
async def test_nothing_is_called_while_signed_out(tmp_path):
    client = _client(signed_in=False)
    sink = TTTRecordingSink(client, "cam")
    group = str(_group(tmp_path, recording_id="r"))
    await sink.published(group, "https://youtu.be/v", datetime(2026, 6, 1))
    await sink.progress(group, StepReport("x", "x", "X", "running"))
    assert await sink.phases(group, {"times": {"kickoff": 1.0}}) is False
    client.auto_match_video.assert_not_called()
    client.update_recording_step.assert_not_called()
    client.update_game_session_phases.assert_not_called()


# -- phases -------------------------------------------------------------------


def test_phase_fields_mapping():
    fields = phases_to_session_fields(
        {
            "times": {
                "kickoff": 10,
                "halftime": 2700.5,
                "second_half": 3600,
                "end": 6300,
            },
            "ok": False,
            "source": "human",
            "truncated_start": True,
        }
    )
    assert fields == {
        "phase_kickoff_offset": 10.0,
        "phase_halftime_offset": 2700.5,
        "phase_second_half_offset": 3600.0,
        "phase_end_offset": 6300.0,
        "phase_source": "human",
        "phase_ok": False,
        "phase_truncated_start": True,
    }
    assert phases_to_session_fields({"times": {}}) == {}
    assert phases_to_session_fields(None) == {}
    assert phases_to_session_fields({"times": {"kickoff": 1}})["phase_source"] == (
        "phase_fused"
    )


@pytest.mark.asyncio
async def test_phases_write_the_session_found_by_dir_name(tmp_path):
    client = _client()
    ok = await TTTRecordingSink(client, "cam").phases(
        str(tmp_path / "grp-1"), {"times": {"kickoff": 45.0}, "source": "human"}
    )
    assert ok is True
    client.get_game_session_by_dir.assert_called_once_with("grp-1")
    args, kwargs = client.update_game_session_phases.call_args
    assert args == ("sess-1",)
    assert kwargs["phase_kickoff_offset"] == 45.0


@pytest.mark.asyncio
async def test_phases_without_a_session_or_times_write_nothing(tmp_path):
    client = _client()
    sink = TTTRecordingSink(client, "cam")
    assert await sink.phases("grp", {"times": {}}) is False
    client.get_game_session_by_dir.return_value = None
    assert await sink.phases("grp", {"times": {"end": 5.0}}) is False
    client.update_game_session_phases.assert_not_called()


@pytest.mark.asyncio
async def test_phase_verdicts(tmp_path):
    client = _client()
    sink = TTTRecordingSink(client, "cam")
    assert await sink.phase_verified("grp", "halftime", "correct") is True
    assert client.update_game_session_phases.call_args.kwargs == {
        "phase_halftime_verified": "correct"
    }
    assert await sink.phase_verified("grp", "nonsense", "correct") is False
    assert await sink.phase_verified("grp", "end", "maybe") is False


@pytest.mark.asyncio
async def test_session_errors_are_swallowed():
    client = _client()
    client.update_game_session_phases.side_effect = RuntimeError("500")
    assert (
        await TTTRecordingSink(client, "cam").phases("grp", {"times": {"end": 1.0}})
        is False
    )


@pytest.mark.asyncio
async def test_recordings_are_reported_under_the_camera_that_recorded_them(tmp_path):
    from video_grouper.integrations.ttt_cameras import TTTCameraIds

    cameras = TTTCameraIds(None, "typed")
    cameras.update({"front": "c-front", "back": "c-back"})
    client = _client()
    sink = TTTRecordingSink(client, cameras=cameras)
    await sink.recording_found(str(_group(tmp_path)), [_file()], camera="back")
    assert client.register_recordings.call_args.args[0] == "c-back"
