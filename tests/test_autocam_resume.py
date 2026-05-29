"""Tests for the AutoCam resume-on-restart path.

The full ``_execute_autocam_gui_automation`` runs pywinauto + win32gui +
subprocess (tasklist/wmic) against a live desktop, so we exercise it
indirectly: directly test the tiny helpers (``_live_autocam_pids``,
``_find_autocam_gui_pids``, the resume-marker read/write on
DirectoryState), and assert the documented behavior of the wider
function via patches on its dependencies.
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
    _find_autocam_gui_pids,
    _live_autocam_pids,
    _parse_wmi_datetime,
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


def _pid_filter_from_cmd(cmd: list[str]) -> int | None:
    """Pull the integer PID out of a `tasklist /FI 'PID eq N' ...` cmd."""
    for i, arg in enumerate(cmd):
        if arg == "/FI" and i + 1 < len(cmd) and cmd[i + 1].startswith("PID eq "):
            try:
                return int(cmd[i + 1].split()[-1])
            except ValueError:
                return None
    return None


class TestLiveAutocamPids:
    def test_returns_only_alive_gui_exe(self):
        # PID 1 -> alive GUI.exe, PID 2 -> alive notepad.exe (filter excludes),
        # PID 99 -> not running. tasklist applies both PID and IMAGENAME filters
        # in a single call: a non-empty body means the row matched both.
        def fake_run_console(cmd, timeout=10.0):
            pid = _pid_filter_from_cmd(cmd)
            if pid == 1:
                return '"GUI.exe","1","Console","1","12,345 K"\n'
            # PID 2's IMAGENAME doesn't match -> tasklist returns its
            # "no tasks running" banner on stdout in some Win versions;
            # either way _parse_tasklist_csv_pids returns []. PID 99
            # doesn't exist: same outcome.
            return ""

        with patch(
            "video_grouper.tray.autocam_automation._run_console",
            side_effect=fake_run_console,
        ):
            assert _live_autocam_pids([1, 2, 99]) == [1]

    def test_empty_input_returns_empty(self):
        assert _live_autocam_pids([]) == []


class TestFindAutocamGuiPids:
    def test_no_filter_returns_tasklist_pids(self):
        tasklist_out = (
            '"GUI.exe","1234","Console","1","12,345 K"\n'
            '"GUI.exe","5678","Console","1","11,000 K"\n'
        )
        with patch(
            "video_grouper.tray.autocam_automation._run_console",
            return_value=tasklist_out,
        ):
            assert sorted(_find_autocam_gui_pids()) == [1234, 5678]

    def test_since_epoch_filters_out_old_pids(self):
        # tasklist reports both PIDs; wmic dates them on opposite sides
        # of the cutoff. The filter drops the older one.
        import datetime as _dt

        cutoff = _dt.datetime(2026, 3, 1, tzinfo=_dt.UTC).timestamp()
        tasklist_out = (
            '"GUI.exe","1234","Console","1","12,345 K"\n'
            '"GUI.exe","5678","Console","1","11,000 K"\n'
        )

        def fake_run_console(cmd, timeout=10.0):
            if cmd[0] == "tasklist":
                return tasklist_out
            assert cmd[0] == "wmic"
            pid = -1
            for arg in cmd:
                if arg.startswith("ProcessId="):
                    pid = int(arg.split("=", 1)[1])
                    break
            if pid == 1234:  # 2026-01-01 -- before cutoff
                return "Node,CreationDate\nDESKTOP,20260101000000.000000+000\n"
            if pid == 5678:  # 2026-06-01 -- after cutoff
                return "Node,CreationDate\nDESKTOP,20260601000000.000000+000\n"
            return ""

        with patch(
            "video_grouper.tray.autocam_automation._run_console",
            side_effect=fake_run_console,
        ):
            assert _find_autocam_gui_pids(since_epoch=cutoff) == [5678]

    def test_wmic_unavailable_falls_open(self):
        # wmic returning '' (timeout/missing) should keep the PID
        # rather than drop it -- a false positive is recoverable;
        # a false negative would skip the resume reattach.
        tasklist_out = '"GUI.exe","1234","Console","1","12,345 K"\n'

        def fake_run_console(cmd, timeout=10.0):
            if cmd[0] == "tasklist":
                return tasklist_out
            return ""  # wmic unavailable

        with patch(
            "video_grouper.tray.autocam_automation._run_console",
            side_effect=fake_run_console,
        ):
            assert _find_autocam_gui_pids(since_epoch=1.0) == [1234]


class TestParseWmiDatetime:
    def test_round_trips_utc_offset(self):
        # 2026-05-29 18:27:08.123456 UTC-5h
        epoch = _parse_wmi_datetime("20260529182708.123456-300")
        assert epoch is not None
        # 18:27:08 in UTC-5 == 23:27:08 UTC
        import datetime as _dt

        expected = _dt.datetime(
            2026, 5, 29, 23, 27, 8, 123456, tzinfo=_dt.UTC
        ).timestamp()
        assert epoch == pytest.approx(expected, abs=1e-3)

    def test_malformed_returns_none(self):
        assert _parse_wmi_datetime("") is None
        assert _parse_wmi_datetime("garbage") is None
        assert _parse_wmi_datetime("20260529182708.xxxxxx-300") is None


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
