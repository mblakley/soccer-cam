"""Tests for the tray-side UpdateStatusPoller.

The poller is the only client of /api/update/status. Tests pin the
transition rules:

  - pending appearing fires on_pending once with (version, auto_update)
  - pending changing fires on_pending again with the new version
  - pending clearing fires on_cleared once
  - no spurious callbacks when status stays the same
"""

from __future__ import annotations

from unittest.mock import MagicMock

from video_grouper.tray.main import UpdateStatusPoller


def _make_poller():
    on_pending = MagicMock()
    on_cleared = MagicMock()
    poller = UpdateStatusPoller(
        status_url="http://x/status",
        on_pending=on_pending,
        on_cleared=on_cleared,
    )
    return poller, on_pending, on_cleared


def test_pending_appearing_fires_on_pending():
    poller, on_pending, on_cleared = _make_poller()
    poller._handle_status({"pending_version": "0.3.7", "auto_update": True})
    on_pending.assert_called_once_with("0.3.7", True)
    on_cleared.assert_not_called()


def test_pending_repeated_does_not_refire():
    poller, on_pending, on_cleared = _make_poller()
    poller._handle_status({"pending_version": "0.3.7", "auto_update": True})
    poller._handle_status({"pending_version": "0.3.7", "auto_update": True})
    on_pending.assert_called_once()


def test_pending_version_change_refires():
    poller, on_pending, on_cleared = _make_poller()
    poller._handle_status({"pending_version": "0.3.7", "auto_update": True})
    poller._handle_status({"pending_version": "0.3.8", "auto_update": True})
    assert on_pending.call_count == 2
    on_pending.assert_called_with("0.3.8", True)


def test_pending_clearing_fires_on_cleared():
    poller, on_pending, on_cleared = _make_poller()
    poller._handle_status({"pending_version": "0.3.7", "auto_update": True})
    poller._handle_status({"pending_version": None, "auto_update": True})
    on_cleared.assert_called_once()


def test_no_pending_initially_does_nothing():
    poller, on_pending, on_cleared = _make_poller()
    poller._handle_status({"pending_version": None, "auto_update": True})
    on_pending.assert_not_called()
    on_cleared.assert_not_called()


def test_auto_update_flag_passed_through():
    poller, on_pending, on_cleared = _make_poller()
    poller._handle_status({"pending_version": "0.3.7", "auto_update": False})
    on_pending.assert_called_once_with("0.3.7", False)
