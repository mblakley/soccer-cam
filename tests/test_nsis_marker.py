"""Tests for the NSIS install-phase marker reader."""

from __future__ import annotations

import pytest

from video_grouper.update.nsis_marker import (
    nsis_marker_path,
    read_and_clear_marker,
)


@pytest.fixture
def marker_dir(tmp_path, monkeypatch):
    """Redirect the marker path to a tmp file. The override env var
    is the only way to test cross-platform; the real default reads
    from %ProgramData% which we can't safely write to in tests."""
    marker = tmp_path / "nsis-phase.txt"
    monkeypatch.setenv("SOCCER_CAM_NSIS_MARKER_PATH", str(marker))
    return marker


def test_missing_marker_returns_none(marker_dir):
    assert read_and_clear_marker() is None


def test_complete_phase_round_trip(marker_dir):
    marker_dir.write_text("complete", encoding="utf-8")
    assert read_and_clear_marker() == "complete"
    # And the file is gone -- next service start won't double-log.
    assert not marker_dir.exists()


def test_partial_phase_round_trip(marker_dir):
    """A non-complete value signals a failed install. The reader
    doesn't interpret -- the processor decides what to journal."""
    marker_dir.write_text("scheduled-task-registered", encoding="utf-8")
    assert read_and_clear_marker() == "scheduled-task-registered"


def test_trailing_whitespace_stripped(marker_dir):
    """NSIS FileWrite drops content as-is; tolerate accidental
    trailing newlines or whitespace from manual edits."""
    marker_dir.write_text("complete\n", encoding="utf-8")
    assert read_and_clear_marker() == "complete"


def test_empty_marker_returns_none(marker_dir):
    marker_dir.write_text("", encoding="utf-8")
    assert read_and_clear_marker() is None


def test_default_path_uses_program_data(monkeypatch):
    monkeypatch.delenv("SOCCER_CAM_NSIS_MARKER_PATH", raising=False)
    monkeypatch.setenv("ProgramData", r"C:\TestProgramData")
    path = nsis_marker_path()
    assert "TestProgramData" in str(path)
    assert path.name == "nsis-phase.txt"
    assert "VideoGrouper" in path.parts
    assert "update" in path.parts
