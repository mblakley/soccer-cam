"""The event-tap anchor fetch is gated on TTT login.

soccer-cam only pulls parent event-taps from TTT when the install is logged in
as a TTT user (``_authed_client_and_session`` returns a client only when
``client.is_authenticated()``); otherwise it runs detector-only.
"""

from __future__ import annotations

from types import SimpleNamespace

import video_grouper.task_processors.phase_ttt_push as ttt_push
from video_grouper.task_processors.phase_game_start import _fetch_event_tap_anchors


class _FakeClient:
    def __init__(self, anchors):
        self._anchors = anchors
        self.calls = 0

    def get_sync_anchors(self, session_id):
        self.calls += 1
        return self._anchors


def test_not_logged_in_returns_empty(monkeypatch):
    """No authed TTT client (not a TTT user / logged out) -> no fetch."""
    monkeypatch.setattr(
        ttt_push, "_authed_client_and_session", lambda *a, **k: (None, None)
    )
    config = SimpleNamespace(ttt=None)
    assert _fetch_event_tap_anchors(config, "/tmp/group", None) == {}


def test_ttt_disabled_returns_empty(monkeypatch):
    """A real _authed_client_and_session returns (None, None) when TTT is
    disabled -> the fetch is a no-op without ever building a client."""
    config = SimpleNamespace(ttt=SimpleNamespace(model_dump=lambda: {"enabled": False}))
    # No monkeypatch: exercise the real gate with disabled config.
    assert _fetch_event_tap_anchors(config, "/tmp/group", None) == {}


def test_logged_in_builds_anchors_from_event_taps(monkeypatch):
    """Authed TTT user -> event_tap anchors fetched + built; other anchor types
    (chirp/network_probe) ignored."""
    raw = [
        {"anchor_type": "event_tap", "label": "kickoff", "video_time_seconds": 600.0},
        {"anchor_type": "event_tap", "label": "kickoff", "video_time_seconds": 604.0},
        {"anchor_type": "event_tap", "label": "game_end", "video_time_seconds": 6000.0},
        {"anchor_type": "chirp", "label": None, "video_time_seconds": 1.0},  # ignored
    ]
    client = _FakeClient(raw)
    monkeypatch.setattr(
        ttt_push,
        "_authed_client_and_session",
        lambda *a, **k: (client, {"id": "sess-1"}),
    )
    config = SimpleNamespace(ttt=SimpleNamespace(model_dump=lambda: {"enabled": True}))
    anchors = _fetch_event_tap_anchors(config, "/tmp/group", None)
    assert client.calls == 1
    assert set(anchors) == {"kickoff", "end"}
    assert anchors["kickoff"].confidence == "high"  # 2 agreeing taps
    assert anchors["kickoff"].video_time == 602.0  # median of the cluster
    assert anchors["end"].confidence == "low"  # lone tap


def test_fetch_never_raises_on_client_error(monkeypatch):
    """A get_sync_anchors failure is swallowed -> {} (detector-only)."""

    class _Boom:
        def get_sync_anchors(self, session_id):
            raise RuntimeError("network down")

    monkeypatch.setattr(
        ttt_push,
        "_authed_client_and_session",
        lambda *a, **k: (_Boom(), {"id": "sess-1"}),
    )
    config = SimpleNamespace(ttt=SimpleNamespace(model_dump=lambda: {"enabled": True}))
    assert _fetch_event_tap_anchors(config, "/tmp/group", None) == {}
