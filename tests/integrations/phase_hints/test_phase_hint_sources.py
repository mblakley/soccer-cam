"""TTT and Null phase-hint sources, each on its own."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from video_grouper.integrations.phase_hints.sources.null import NullPhaseHintSource
from video_grouper.integrations.phase_hints.sources.ttt import TTTPhaseHintSource
from video_grouper.integrations.registry import IntegrationContext

TAPS = [
    {"anchor_type": "event_tap", "label": "kickoff", "video_time_seconds": 600.0},
    {"anchor_type": "chirp", "label": None, "video_time_seconds": 1.0},
    "not a dict",
]


def _client(signed_in=True, session=None):
    c = MagicMock()
    c.ensure_signed_in.return_value = signed_in
    c.get_game_session_by_dir.return_value = (
        {"id": "sess-1"} if session is None else session
    )
    c.get_sync_anchors.return_value = TAPS
    return c


@pytest.mark.asyncio
async def test_ttt_returns_only_event_taps_for_the_dirs_session():
    client = _client()
    taps = await TTTPhaseHintSource(client).event_taps("/storage/grp-1/")
    client.get_game_session_by_dir.assert_called_once_with("grp-1")
    client.get_sync_anchors.assert_called_once_with("sess-1")
    assert taps == [TAPS[0]]


@pytest.mark.asyncio
async def test_ttt_signed_out_asks_nothing():
    client = _client(signed_in=False)
    assert await TTTPhaseHintSource(client).event_taps("grp") == []
    client.get_game_session_by_dir.assert_not_called()


@pytest.mark.asyncio
async def test_ttt_without_a_session_has_no_taps():
    client = _client(session={})
    assert await TTTPhaseHintSource(client).event_taps("grp") == []
    client.get_sync_anchors.assert_not_called()


@pytest.mark.asyncio
async def test_ttt_errors_mean_no_hints():
    client = _client()
    client.get_sync_anchors.side_effect = RuntimeError("down")
    assert await TTTPhaseHintSource(client).event_taps("grp") == []


def test_ttt_chosen_only_when_enabled():
    on = IntegrationContext(
        SimpleNamespace(ttt=SimpleNamespace(enabled=True)), ".", {"ttt": _client()}
    )
    off = IntegrationContext(SimpleNamespace(ttt=SimpleNamespace(enabled=False)), ".")
    assert isinstance(TTTPhaseHintSource.from_context(on), TTTPhaseHintSource)
    assert TTTPhaseHintSource.from_context(off) is None


@pytest.mark.asyncio
async def test_null_has_no_hints():
    assert await NullPhaseHintSource().event_taps("grp") == []
    assert isinstance(NullPhaseHintSource.from_context(object()), NullPhaseHintSource)


@pytest.mark.asyncio
async def test_ttt_refreshes_an_expired_token_instead_of_going_quiet(expired_ttt):
    expired_ttt.routes[("GET", "/api/game-sessions")] = [{"id": "sess-1"}]
    expired_ttt.routes[("GET", "/api/sync-anchors")] = TAPS[:2]
    taps = await TTTPhaseHintSource(expired_ttt.client).event_taps("/s/grp-1")
    assert taps == [TAPS[0]]
    assert expired_ttt.refreshes == 1
