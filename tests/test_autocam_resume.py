"""Tests for the AutoCam resume-on-restart path.

The full ``_execute_autocam_gui_automation`` runs pywinauto + win32gui +
psutil against a live desktop, so we exercise it indirectly: directly
test the tiny helpers (``_live_autocam_pids``, the resume-marker
read/write on DirectoryState), and assert the documented behavior of
the wider function via patches on its dependencies.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from video_grouper.models.directory_state import DirectoryState
from video_grouper.tray.autocam_automation import (
    _execute_autocam_gui_automation,
    _live_autocam_pids,
)


@pytest.fixture
def group_dir():
    """Create a video group directory with a date-formatted name.

    DirectoryState only loads/saves state for directories matching the
    YYYY.MM.DD-HH.MM.SS convention; using a non-conforming name leaves
    the model in a degraded "skip everything" state, which would silently
    pass these tests against the wrong code path.
    """
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "2026.05.07-18.28.00"
        d.mkdir()
        yield str(d)


class TestSetClearAutocamRun:
    def test_set_then_get_round_trips(self, group_dir):
        state = DirectoryState(group_dir)
        run = {
            "launcher_pid": 1234,
            "gui_pids": [1234, 5678],
            "input_path": "C:\\test\\in-raw.mp4",
            "output_path": "C:\\test\\out.mp4",
            "started_at": "2026-05-09T20:00:00Z",
        }
        state.set_autocam_run(run)
        # Re-read from disk via a fresh DirectoryState (resume scenario).
        fresh = DirectoryState(group_dir)
        assert fresh.get_autocam_run() == run

    def test_clear_removes_field(self, group_dir):
        state = DirectoryState(group_dir)
        state.set_autocam_run({"launcher_pid": 1234, "gui_pids": [1234]})
        assert state.get_autocam_run() is not None
        state.clear_autocam_run()
        assert DirectoryState(group_dir).get_autocam_run() is None

    def test_save_state_preserves_autocam_run(self, group_dir):
        """Status updates must not wipe the resume marker."""
        state = DirectoryState(group_dir)
        state.set_autocam_run({"launcher_pid": 1234, "gui_pids": [1234]})
        # Simulate a normal status update path that goes through
        # _save_state_nolock (e.g., StateAuditor demoting a state).
        import asyncio

        asyncio.run(state.update_group_status("trimmed"))
        # The autocam_run field must survive.
        assert DirectoryState(group_dir).get_autocam_run() is not None

    def test_save_state_preserves_youtube_playlist_name(self, group_dir):
        """Same protection applies to the youtube_playlist_name field —
        previously, _save_state_nolock would silently wipe it."""
        state = DirectoryState(group_dir)
        state.set_youtube_playlist_name("Heat 2012s")
        import asyncio

        asyncio.run(state.update_group_status("trimmed"))
        assert DirectoryState(group_dir).get_youtube_playlist_name() == "Heat 2012s"


class TestLiveAutocamPids:
    def test_returns_only_alive_gui_exe(self):
        # Build two psutil.Process mocks: one alive GUI.exe, one alive Notepad.exe,
        # and one dead PID (NoSuchProcess).
        alive_gui = MagicMock()
        alive_gui.is_running.return_value = True
        alive_gui.name.return_value = "GUI.exe"
        alive_other = MagicMock()
        alive_other.is_running.return_value = True
        alive_other.name.return_value = "notepad.exe"

        import psutil as real_psutil

        def fake_process(pid):
            if pid == 1:
                return alive_gui
            if pid == 2:
                return alive_other
            raise real_psutil.NoSuchProcess(pid)

        with patch(
            "video_grouper.tray.autocam_automation.psutil.Process",
            side_effect=fake_process,
        ):
            assert _live_autocam_pids([1, 2, 99]) == [1]

    def test_empty_input_returns_empty(self):
        assert _live_autocam_pids([]) == []


class TestResumeBranch:
    """Patch DirectoryState + win32 + psutil + pywinauto to verify the
    resume vs fresh-launch branching in _execute_autocam_gui_automation."""

    @pytest.fixture
    def patches(self, group_dir):
        """Common patches used by the resume tests. Yields a dict of mocks."""
        with (
            patch("video_grouper.tray.autocam_automation.subprocess") as sub,
            patch("video_grouper.tray.autocam_automation.Desktop") as desktop_cls,
            patch(
                "video_grouper.tray.autocam_automation._find_autocam_hwnd"
            ) as find_hwnd,
            patch(
                "video_grouper.tray.autocam_automation._live_autocam_pids"
            ) as live_pids,
            patch(
                "video_grouper.tray.autocam_automation._wait_for_completion_and_cleanup"
            ) as wait_complete,
            # The production code does time.sleep(5)/sleep(1) to let AutoCam
            # spin up; in the test, every dependency is already patched so
            # those sleeps just slow the suite down.
            patch("video_grouper.tray.autocam_automation.time.sleep"),
            # _execute_autocam_gui_automation defines an inner helper
            # _find_settings_hwnd that calls win32gui.EnumWindows on the
            # real desktop; without patching it, the fresh-launch test
            # spins for the full 15s "wait for SettingsWindow" deadline.
            patch("video_grouper.tray.autocam_automation.win32gui") as wg,
        ):
            wg.EnumWindows = MagicMock()
            wg.IsWindowVisible = MagicMock(return_value=False)
            # Default desktop().window() returns a usable mock window.
            window = MagicMock()
            window.wait.return_value = None
            desktop_cls.return_value.window.return_value = window
            wait_complete.return_value = True
            yield {
                "sub": sub,
                "desktop_cls": desktop_cls,
                "find_hwnd": find_hwnd,
                "live_pids": live_pids,
                "wait_complete": wait_complete,
                "window": window,
                "group_dir": group_dir,
            }

    def test_resume_skips_launch_when_marker_and_processes_alive(self, patches):
        """When state.json has a marker AND PIDs are alive AND window is
        present AND input matches, skip subprocess.Popen entirely."""
        gd = patches["group_dir"]
        # Pre-populate state.json with a matching marker.
        state = DirectoryState(gd)
        input_p = Path(gd) / "video" / "in-raw.mp4"
        input_p.parent.mkdir(parents=True, exist_ok=True)
        input_p.touch()
        input_path = str(input_p)
        state.set_autocam_run(
            {
                "launcher_pid": 1234,
                "gui_pids": [1234, 5678],
                "input_path": os.path.abspath(input_path),
                "output_path": os.path.join(gd, "video", "out.mp4"),
                "started_at": "2026-05-09T20:00:00Z",
            }
        )

        patches["live_pids"].return_value = [5678]
        patches["find_hwnd"].return_value = 0xCAFE
        patches["wait_complete"].return_value = True

        result = _execute_autocam_gui_automation(
            "fake.exe",
            input_path,
            os.path.join(gd, "video", "out.mp4"),
            group_dir=gd,
        )

        assert result is True
        # No taskkill, no Popen — the resume branch took over.
        patches["sub"].run.assert_not_called()
        patches["sub"].Popen.assert_not_called()
        patches["wait_complete"].assert_called_once()

    def test_stale_marker_with_no_live_pids_falls_through_to_launch(self, patches):
        gd = patches["group_dir"]
        state = DirectoryState(gd)
        input_p = Path(gd) / "video" / "in-raw.mp4"
        input_p.parent.mkdir(parents=True, exist_ok=True)
        input_p.touch()
        input_path = str(input_p)
        state.set_autocam_run(
            {
                "launcher_pid": 1234,
                "gui_pids": [1234, 5678],
                "input_path": os.path.abspath(input_path),
                "output_path": os.path.join(gd, "video", "out.mp4"),
                "started_at": "2026-05-09T20:00:00Z",
            }
        )

        # No live PIDs — marker is stale.
        patches["live_pids"].return_value = []
        # Fresh-launch path: window appears after Popen.
        patches["find_hwnd"].return_value = 0xCAFE
        # Make Popen return a launcher with a known pid.
        launcher = MagicMock()
        launcher.pid = 9999
        patches["sub"].Popen.return_value = launcher

        with patch(
            "video_grouper.tray.autocam_automation._find_autocam_gui_pids",
            return_value=[9999, 12345],
        ):
            result = _execute_autocam_gui_automation(
                "fake.exe",
                input_path,
                os.path.join(gd, "video", "out.mp4"),
                group_dir=gd,
            )

        assert result is True
        # taskkill (existing instance kill) + Popen (fresh launch) both called.
        patches["sub"].Popen.assert_called_once()
        # State.json now reflects the new run (cleared inside _wait_for_completion_and_cleanup,
        # which was patched — but the launch path wrote it before that ran).
        run = state.get_autocam_run()
        assert run is not None
        assert run["launcher_pid"] == 9999
        assert 9999 in run["gui_pids"]

    def test_no_group_dir_disables_resume_tracking(self, patches):
        """With group_dir=None, no state.json reads/writes happen and the
        function takes the legacy launch-from-scratch path."""
        # Even with state-like patches, none should be touched.
        patches["find_hwnd"].return_value = 0xCAFE
        launcher = MagicMock()
        launcher.pid = 9999
        patches["sub"].Popen.return_value = launcher

        with patch(
            "video_grouper.tray.autocam_automation._find_autocam_gui_pids",
            return_value=[9999],
        ):
            result = _execute_autocam_gui_automation(
                "fake.exe", "input.mp4", "output.mp4", group_dir=None
            )

        assert result is True
        patches["sub"].Popen.assert_called_once()

    def test_resume_input_mismatch_falls_through_to_launch(self, patches):
        """If the marker's input_path doesn't match our task, treat as stale."""
        gd = patches["group_dir"]
        state = DirectoryState(gd)
        # Marker recorded a DIFFERENT input file.
        state.set_autocam_run(
            {
                "launcher_pid": 1234,
                "gui_pids": [1234],
                "input_path": "C:\\old\\different-raw.mp4",
                "output_path": "C:\\old\\different.mp4",
                "started_at": "2026-05-09T20:00:00Z",
            }
        )

        patches["find_hwnd"].return_value = 0xCAFE
        launcher = MagicMock()
        launcher.pid = 9999
        patches["sub"].Popen.return_value = launcher

        with patch(
            "video_grouper.tray.autocam_automation._find_autocam_gui_pids",
            return_value=[9999],
        ):
            result = _execute_autocam_gui_automation(
                "fake.exe",
                os.path.join(gd, "in-raw.mp4"),
                os.path.join(gd, "out.mp4"),
                group_dir=gd,
            )

        assert result is True
        # Resume branch's input mismatch -> fall through to fresh launch (Popen called).
        patches["sub"].Popen.assert_called_once()
        # The new run replaces the old marker.
        new_run = state.get_autocam_run()
        assert new_run is not None
        assert new_run["launcher_pid"] == 9999
