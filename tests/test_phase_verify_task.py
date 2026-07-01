"""S3: PhaseVerifyTask — per-boundary Correct/Not-Correct + TTT write-back + no-secret metadata."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from video_grouper.task_processors.tasks.ntfy.phase_verify_task import (
    PhaseVerifyTask,
    sanitize_ttt_conn,
)

TTT_CONN_WITH_SECRETS = {
    "enabled": True,
    "supabase_url": "u",
    "anon_key": "a",
    "api_base_url": "b",
    "email": "e@x",
    "password": "hunter2",
}


def _config():
    return SimpleNamespace(
        ntfy=SimpleNamespace(
            topic="my-topic", server_url="https://ntfy.sh", enabled=True
        )
    )


def _task(remaining, ttt_conn=None):
    return PhaseVerifyTask(
        group_dir="/data/grp-1",
        config=_config(),
        ntfy_service=MagicMock(),
        video_path=None,  # no screenshot in tests
        remaining=remaining,
        recording_group_dir="grp-1",
        storage_path="/data",
        ttt_conn=ttt_conn if ttt_conn is not None else {"enabled": True},
    )


def test_sanitize_drops_credentials():
    clean = sanitize_ttt_conn(TTT_CONN_WITH_SECRETS)
    assert "email" not in clean and "password" not in clean
    assert clean["supabase_url"] == "u" and clean["api_base_url"] == "b"


def test_metadata_never_carries_secrets():
    task = _task([["kickoff", 10.0]], ttt_conn=TTT_CONN_WITH_SECRETS)
    # metadata is persisted to state.json — must not contain credentials
    assert "password" not in task.metadata["ttt_conn"]
    assert "email" not in task.metadata["ttt_conn"]
    assert "hunter2" not in str(task.metadata)


@pytest.mark.asyncio
@patch("video_grouper.task_processors.phase_ttt_push.push_phase_verified")
async def test_process_response_correct_chains(mock_push):
    task = _task([["kickoff", 10.0], ["end", 5640.0]])
    result = await task.process_response("Correct: kickoff at 0:10")
    assert result.success and result.should_continue
    assert result.metadata["next_remaining"] == [["end", 5640.0]]
    args = mock_push.call_args.args
    assert args[2] == "kickoff" and args[3] == "correct"


@pytest.mark.asyncio
@patch("video_grouper.task_processors.phase_ttt_push.push_phase_verified")
async def test_process_response_not_correct_last_stops(mock_push):
    task = _task([["end", 5640.0]])
    result = await task.process_response("Not correct: end at 94:00")
    assert result.success and not result.should_continue
    args = mock_push.call_args.args
    assert args[2] == "end" and args[3] == "not_correct"


@pytest.mark.asyncio
@patch("video_grouper.task_processors.phase_ttt_push.push_phase_verified")
async def test_process_response_unrecognized(mock_push):
    task = _task([["kickoff", 10.0]])
    result = await task.process_response("huh?")
    assert not result.success
    mock_push.assert_not_called()


@pytest.mark.asyncio
async def test_create_question_has_verify_actions():
    task = _task([["halftime", 2880.0]])
    q = await task.create_question()
    labels = [a["label"] for a in q["actions"]]
    assert labels == ["Correct", "Not Correct"]
    assert "halftime" in q["tags"]


def test_create_next_task_advances():
    task = _task([["kickoff", 10.0], ["halftime", 2880.0]])
    nxt = PhaseVerifyTask.create_next_task(task, [["halftime", 2880.0]])
    assert nxt.remaining == [["halftime", 2880.0]]
    assert nxt.recording_group_dir == "grp-1"
