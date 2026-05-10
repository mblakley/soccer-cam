"""Tests for the dashboard's Tray status section.

Verifies that ``_render_tray_section`` correctly handles the three
states the tray's marker file (``<storage>/.tray_log_path``) can be in:
absent, present-but-stale, present-with-fresh-data.
"""

import time

import pytest

from video_grouper.web.auth_server import _render_tray_section


@pytest.fixture
def storage(tmp_path):
    return tmp_path


def test_render_no_marker(storage):
    """No marker file → 'Tray not detected' with the explainer."""
    out = _render_tray_section(storage)
    assert "Tray not detected" in out
    assert ".tray_log_path" in out


def test_render_marker_pointing_at_missing_file(storage):
    """Marker exists but points at a path that's been deleted → warn state."""
    (storage / ".tray_log_path").write_text(
        str(storage / "missing.log"), encoding="utf-8"
    )
    out = _render_tray_section(storage)
    assert "missing" in out.lower()
    assert "status-dot bad" in out


def test_render_fresh_log_shows_recent_activity(storage):
    """Marker points at a real log written recently → active state, log tail."""
    log = storage / "tray.log"
    log.write_text(
        "2026-05-10 12:00:00 | INFO | tray.something:42 - line 1\n"
        "2026-05-10 12:00:01 | INFO | tray.something:43 - line 2 with <html> escape\n",
        encoding="utf-8",
    )
    (storage / ".tray_log_path").write_text(str(log), encoding="utf-8")
    out = _render_tray_section(storage)
    assert "active" in out
    assert "status-dot on" in out
    # html escaping
    assert "&lt;html&gt;" in out


def test_render_stale_log_warns(storage, monkeypatch):
    """Log mtime > 10 min old → 'stale' warning."""
    log = storage / "tray.log"
    log.write_text("old entry\n", encoding="utf-8")
    (storage / ".tray_log_path").write_text(str(log), encoding="utf-8")
    # Push the log's mtime 30 min into the past
    old = time.time() - 30 * 60
    import os

    os.utime(log, (old, old))
    out = _render_tray_section(storage)
    assert "stale" in out
    assert "status-dot bad" in out


# Defensive OSError-on-read paths in _render_tray_section are exercised by
# the runtime safety net rather than by tests — they're hard to mock
# without monkeypatching pathlib internals, and the cost of a bad page
# render in that edge case is small.
