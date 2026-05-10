"""Tests for the dashboard's Tray status section.

The tray writes to ``<storage>/logs/video_grouper_tray.log`` after
config loads (same convention as the service log). The dashboard reads
it directly from that path — no marker file involved.
"""

import os
import time

import pytest

from video_grouper.web.auth_server import _render_tray_section


@pytest.fixture
def storage(tmp_path):
    return tmp_path


def _write_tray_log(storage, contents: str):
    log_dir = storage / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / "video_grouper_tray.log"
    log.write_text(contents, encoding="utf-8")
    return log


def test_render_no_log_file(storage):
    """Tray log doesn't exist yet → 'Tray log not found' explainer."""
    out = _render_tray_section(storage)
    assert "Tray log not found" in out
    assert "video_grouper_tray.log" in out


def test_render_fresh_log_shows_recent_activity(storage):
    """Recent log → active state, last lines shown, html-escaped."""
    _write_tray_log(
        storage,
        "2026-05-10 12:00:00 | INFO | tray.something:42 - line 1\n"
        "2026-05-10 12:00:01 | INFO | tray.something:43 - line 2 with <html> escape\n",
    )
    out = _render_tray_section(storage)
    assert "active" in out
    assert "status-dot on" in out
    assert "&lt;html&gt;" in out


def test_render_stale_log_warns(storage):
    """Log mtime > 10 min old → 'stale' warning."""
    log = _write_tray_log(storage, "old entry\n")
    old = time.time() - 30 * 60
    os.utime(log, (old, old))
    out = _render_tray_section(storage)
    assert "stale" in out
    assert "status-dot bad" in out
