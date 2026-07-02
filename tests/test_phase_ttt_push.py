"""S2: push detected phases to TTT — field mapping + best-effort push behavior."""

from unittest.mock import patch

from video_grouper.task_processors.phase_ttt_push import (
    phases_to_session_fields,
    push_phases_to_ttt,
)

TTT_CFG = {
    "enabled": True,
    "supabase_url": "u",
    "anon_key": "a",
    "api_base_url": "b",
    "email": "e@x",
    "password": "p",
}


def _payload(**times):
    return {"ok": True, "source": "phase_fused", "times": times}


# ---- phases_to_session_fields ----


def test_fields_full_mapping():
    f = phases_to_session_fields(
        _payload(kickoff=10.0, halftime=2880.0, second_half=3480.0, end=5640.0)
    )
    assert f == {
        "phase_kickoff_offset": 10.0,
        "phase_halftime_offset": 2880.0,
        "phase_second_half_offset": 3480.0,
        "phase_end_offset": 5640.0,
        "phase_source": "phase_fused",
        "phase_ok": True,
    }


def test_fields_empty_when_no_times():
    assert phases_to_session_fields({"ok": False, "times": {}}) == {}
    assert phases_to_session_fields({}) == {}


def test_fields_partial_and_ok_false():
    f = phases_to_session_fields(
        {"ok": False, "source": "human", "times": {"kickoff": 5.0}}
    )
    assert f == {
        "phase_kickoff_offset": 5.0,
        "phase_source": "human",
        "phase_ok": False,
    }


def test_fields_carry_truncation_flags():
    f = phases_to_session_fields(
        {
            "ok": True,
            "times": {"kickoff": 0.0, "end": 5640.0},
            "truncated_start": True,
            "truncated_end": False,
        }
    )
    assert f["phase_truncated_start"] is True
    assert f["phase_truncated_end"] is False
    assert f["phase_kickoff_offset"] == 0.0


def test_fields_omit_truncation_when_absent():
    f = phases_to_session_fields({"ok": True, "times": {"kickoff": 10.0}})
    assert "phase_truncated_start" not in f
    assert "phase_truncated_end" not in f


# ---- push_phases_to_ttt (best-effort) ----


def test_push_skips_when_ttt_disabled():
    assert push_phases_to_ttt(None, "grp", _payload(kickoff=1.0), "/s") is False
    assert (
        push_phases_to_ttt({"enabled": False}, "grp", _payload(kickoff=1.0), "/s")
        is False
    )


def test_push_skips_when_no_phases():
    assert push_phases_to_ttt(TTT_CFG, "grp", {"times": {}}, "/s") is False


@patch("video_grouper.api_integrations.ttt_api.TTTApiClient")
def test_push_updates_session_with_offset_fields(MockClient):
    client = MockClient.return_value
    client.is_authenticated.return_value = True
    client.get_game_session_by_dir.return_value = {"id": "sess-1"}

    ok = push_phases_to_ttt(
        TTT_CFG,
        "grp-dir",
        _payload(kickoff=10.0, halftime=2880.0, second_half=3480.0, end=5640.0),
        "/s",
    )

    assert ok is True
    client.get_game_session_by_dir.assert_called_once_with("grp-dir")
    args, kwargs = client.update_game_session.call_args
    assert args[0] == "sess-1"
    assert kwargs["phase_kickoff_offset"] == 10.0
    assert kwargs["phase_end_offset"] == 5640.0
    assert kwargs["phase_source"] == "phase_fused"
    assert kwargs["phase_ok"] is True


@patch("video_grouper.api_integrations.ttt_api.TTTApiClient")
def test_push_logs_in_when_not_authenticated(MockClient):
    client = MockClient.return_value
    client.is_authenticated.side_effect = [
        False,
        True,
    ]  # not auth'd, then auth'd post-login
    client.get_game_session_by_dir.return_value = {"id": "sess-2"}

    ok = push_phases_to_ttt(TTT_CFG, "grp", _payload(kickoff=1.0), "/s")

    assert ok is True
    client.login.assert_called_once_with("e@x", "p")


@patch("video_grouper.api_integrations.ttt_api.TTTApiClient")
def test_push_skips_when_no_session(MockClient):
    client = MockClient.return_value
    client.is_authenticated.return_value = True
    client.get_game_session_by_dir.return_value = None

    ok = push_phases_to_ttt(TTT_CFG, "grp", _payload(kickoff=1.0), "/s")

    assert ok is False
    client.update_game_session.assert_not_called()


@patch("video_grouper.api_integrations.ttt_api.TTTApiClient")
def test_push_skips_when_unauthenticated_no_creds(MockClient):
    client = MockClient.return_value
    client.is_authenticated.return_value = False  # stays false; no login possible
    cfg = {**TTT_CFG, "email": "", "password": ""}

    ok = push_phases_to_ttt(cfg, "grp", _payload(kickoff=1.0), "/s")

    assert ok is False
    client.get_game_session_by_dir.assert_not_called()


@patch("video_grouper.api_integrations.ttt_api.TTTApiClient")
def test_push_never_raises_on_error(MockClient):
    client = MockClient.return_value
    client.is_authenticated.return_value = True
    client.get_game_session_by_dir.side_effect = RuntimeError("boom")

    assert push_phases_to_ttt(TTT_CFG, "grp", _payload(kickoff=1.0), "/s") is False
