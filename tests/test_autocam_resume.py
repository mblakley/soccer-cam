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
    _autocam_still_writing,
    _execute_autocam_gui_automation,
    _find_autocam_gui_pids,
    _live_autocam_pids,
    _parse_wmi_datetime,
    _run_console,
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

    @pytest.mark.asyncio
    async def test_save_state_preserves_autocam_run(self, group_dir):
        """Status updates must not wipe the resume marker."""
        state = DirectoryState(group_dir)
        state.set_autocam_run({"launcher_pid": 1234, "gui_pids": [1234]})
        # Simulate a normal status update path that goes through
        # _save_state_nolock (e.g., StateAuditor demoting a state).
        await state.update_group_status("trimmed")
        # The autocam_run field must survive.
        assert DirectoryState(group_dir).get_autocam_run() is not None

    @pytest.mark.asyncio
    async def test_save_state_preserves_youtube_playlist_name(self, group_dir):
        """Same protection applies to the youtube_playlist_name field —
        previously, _save_state_nolock would silently wipe it."""
        state = DirectoryState(group_dir)
        state.set_youtube_playlist_name("Heat 2012s")
        await state.update_group_status("trimmed")
        assert DirectoryState(group_dir).get_youtube_playlist_name() == "Heat 2012s"


class TestRelativeGroupDirsResolveToOnePlace:
    """A relative group_dir must not repeat the storage path inside itself.

    The tray is handed the group directory as configured -- and
    ``[STORAGE] path`` is allowed to be relative. Deriving the storage path
    from an un-anchored directory_path made the two disagree, so state.json
    was looked for at <storage>/<storage>/<group>/ and every read and write
    failed with ENOENT while logging and continuing.
    """

    def test_state_file_lands_inside_the_group_dir(self, group_dir, monkeypatch):
        monkeypatch.chdir(os.path.dirname(group_dir))
        state = DirectoryState(os.path.basename(group_dir))

        assert os.path.dirname(state.state_file_path) == os.path.abspath(group_dir)

    def test_a_marker_written_relatively_is_readable_absolutely(
        self, group_dir, monkeypatch
    ):
        monkeypatch.chdir(os.path.dirname(group_dir))
        DirectoryState(os.path.basename(group_dir)).set_autocam_run(
            {"input_path": "C:/in.mp4", "gui_pids": [7]}
        )

        assert DirectoryState(group_dir).get_autocam_run() == {
            "input_path": "C:/in.mp4",
            "gui_pids": [7],
        }


class TestLiveAutocamPids:
    def test_returns_only_alive_gui_exe(self):
        # _live_autocam_pids makes a single tasklist call filtered on
        # IMAGENAME=GUI.exe (no PID filter), then intersects the result
        # against candidate_pids. PID 1 is in the tasklist output;
        # PIDs 2 and 99 are not (either wrong image name or not
        # running) so they get filtered out.
        tasklist_out = '"GUI.exe","1","Console","1","12,345 K"\n'
        with patch(
            "video_grouper.tray.autocam_automation._run_console",
            return_value=tasklist_out,
        ):
            assert _live_autocam_pids([1, 2, 99]) == [1]

    def test_empty_input_returns_empty(self):
        assert _live_autocam_pids([]) == []


class TestAnUnanswerableProbeIsNotADeadProcess:
    """The distinction that keeps a healthy render alive.

    ``tasklist`` timed out under load on 2026-08-23 while AutoCam was 5% into
    a 7680x2160 render. The empty result read as "AutoCam exited", the
    part-written output failed validation, and the step deleted it and gave
    up on a job that was working. "I could not check" and "it is gone" have
    to be different answers.
    """

    def test_a_failed_probe_reports_unknown_not_empty(self):
        with patch(
            "video_grouper.tray.autocam_automation._run_console",
            return_value=None,
        ):
            assert _live_autocam_pids([1, 2]) is None

    def test_liveness_is_one_call_so_it_cannot_half_succeed(self):
        """The reason the first attempt at this fix did not hold.

        With one `tasklist` per image name, a run on 2026-08-23 had the
        GUI.exe probe succeed and the AutocamGUI.exe probe time out. Some
        probe had run, so the empty intersection was taken as authoritative
        and the live render was failed again -- from exactly the ignorance
        the fix was meant to catch. One call cannot half-answer.
        """
        calls = []

        def record(cmd, timeout=10.0):
            calls.append(cmd)
            return "INFO: No tasks are running which match the criteria.\n"

        with patch(
            "video_grouper.tray.autocam_automation._run_console",
            side_effect=record,
        ):
            _live_autocam_pids([1, 2])

        assert len(calls) == 1, f"one probe per poll, got {calls}"
        assert "/FI" not in calls[0], (
            "an image-name filter reintroduces the per-name calls that could "
            "partly fail"
        )

    def test_a_pid_found_alive_is_believed_even_on_a_shaky_probe(self):
        """Positive evidence needs no corroboration; only a negative does."""
        with patch(
            "video_grouper.tray.autocam_automation._run_console",
            return_value='"AutocamGUI.exe","4242","Console","1","2,345 K"\n',
        ):
            assert _live_autocam_pids([4242, 7]) == [4242]

    def test_a_successful_probe_that_matches_nothing_still_means_gone(self):
        """The genuine exit case must keep working, or nothing ever completes."""
        with patch(
            "video_grouper.tray.autocam_automation._run_console",
            return_value="INFO: No tasks are running which match the criteria.\n",
        ):
            assert _live_autocam_pids([1, 2]) == []

    def test_still_writing_assumes_live_when_it_cannot_check(self, group_dir):
        """Guarding a delete: a wrong 'no' corrupts an in-flight render."""
        state = DirectoryState(group_dir)
        state.set_autocam_run({"input_path": "C:/in.mp4", "gui_pids": [4242]})

        with patch(
            "video_grouper.tray.autocam_automation._run_console",
            return_value=None,
        ):
            assert _autocam_still_writing(state, "C:/in.mp4") is True

    def test_run_console_distinguishes_timeout_from_no_output(self):
        import subprocess as sp

        with patch(
            "video_grouper.tray.autocam_automation.subprocess.run",
            side_effect=sp.TimeoutExpired(cmd="tasklist", timeout=10),
        ):
            assert _run_console(["tasklist"]) is None


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


# Verbatim AutoCam 3.1.1 status blobs (see video_grouper_tray.log). The
# idle one is what the reattach path used to mistake for a live render:
# its "Processing Setup" / "Start Processing" buttons contain the word
# "processing" even though nothing is running.
_IDLE_STATUS = (
    "c588e0da50fb | Source: | Browse files | None selected | Destination: | "
    "Browse file | None selected | Processing Setup | Add Logo | Start Processing"
)
_RUNNING_STATUS = _IDLE_STATUS + " | Status: | Running | Processed: | 3874"


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
            # The resume branch now refuses to reattach to an idle window,
            # so these tests must say what the GUI is actually showing.
            patch(
                "video_grouper.tray.autocam_automation._read_autocam_status"
            ) as read_status,
        ):
            read_status.return_value = _RUNNING_STATUS
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
                "read_status": read_status,
                "group_dir": group_dir,
            }

    def test_resume_refuses_idle_window_and_relaunches(self, patches):
        """Regression (2026-08-03): live PIDs + a live window are not
        proof of a render. A run that died during setup left an idle,
        unconfigured GUI; the resume branch attached to it and polled a
        render that never existed until the game hit pipeline_failed.
        An idle status must force the full kill+launch+setup path."""
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
        patches["live_pids"].return_value = [5678]
        patches["find_hwnd"].return_value = 0xCAFE
        patches["read_status"].return_value = _IDLE_STATUS

        _execute_autocam_gui_automation(
            "fake.exe",
            input_path,
            os.path.join(gd, "video", "out.mp4"),
            group_dir=gd,
        )

        # Full relaunch, not a reattach: the idle window is torn down and
        # AutoCam is started fresh so setup actually runs.
        patches["sub"].Popen.assert_called_once()

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


class TestWaitingForTheWindowIsBoundedByLivenessNotAClock:
    """A slow start and a dead start must not look the same.

    A flat 35s deadline failed a game on 2026-08-23 because AutoCam's cold
    start took 59 seconds under load -- Docker, the test suite and a previous
    render's teardown all on the box. The very next game on the same machine
    drew its window in 14s. No fixed number is right; what actually
    distinguishes "still coming up" from "never coming up" is whether AutoCam
    is still there.
    """

    @pytest.fixture
    def patches(self, group_dir):
        with (
            patch("video_grouper.tray.autocam_automation.subprocess") as sub,
            patch("video_grouper.tray.autocam_automation.Desktop") as desktop_cls,
            patch(
                "video_grouper.tray.autocam_automation._find_autocam_hwnd"
            ) as find_hwnd,
            patch(
                "video_grouper.tray.autocam_automation._running_autocam_pids"
            ) as gui_pids,
            patch(
                "video_grouper.tray.autocam_automation._wait_for_completion_and_cleanup"
            ) as wait_complete,
            patch("video_grouper.tray.autocam_automation.time.sleep"),
            patch("video_grouper.tray.autocam_automation.win32gui") as wg,
            patch(
                "video_grouper.tray.autocam_automation._read_autocam_status",
                return_value=_RUNNING_STATUS,
            ),
        ):
            wg.EnumWindows = MagicMock()
            wg.IsWindowVisible = MagicMock(return_value=False)
            desktop_cls.return_value.window.return_value = MagicMock()
            wait_complete.return_value = True
            # Alive by default; tests that care set returncode themselves.
            sub.Popen.return_value.pid = 4242
            sub.Popen.return_value.poll.return_value = None
            yield {
                "sub": sub,
                "find_hwnd": find_hwnd,
                "gui_pids": gui_pids,
                "wait_complete": wait_complete,
                "group_dir": group_dir,
            }

    def _input(self, gd):
        p = Path(gd) / "video" / "in-raw.mp4"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
        return str(p)

    def test_a_window_that_takes_a_minute_still_gets_used(self, patches):
        """The 2026-08-23 failure: 59s to draw, given 35."""
        gd = patches["group_dir"]
        # No window for the first 60 polls (one per second in production),
        # then it appears.
        patches["find_hwnd"].side_effect = [None] * 60 + [0xCAFE] * 10

        _execute_autocam_gui_automation(
            "fake.exe", self._input(gd), os.path.join(gd, "video", "out.mp4")
        )

        patches["wait_complete"].assert_called_once()

    def test_a_dead_launcher_with_no_autocam_left_fails_immediately(self, patches):
        """The genuine crash: don't sit out the backstop for a corpse."""
        gd = patches["group_dir"]
        patches["find_hwnd"].return_value = None
        patches["sub"].Popen.return_value.poll.return_value = 1
        patches["sub"].Popen.return_value.returncode = 1
        patches["gui_pids"].return_value = (set(), True)

        result = _execute_autocam_gui_automation(
            "fake.exe", self._input(gd), os.path.join(gd, "video", "out.mp4")
        )

        assert result is False
        # Bailed on the liveness check, never reached completion polling.
        patches["wait_complete"].assert_not_called()
        assert patches["find_hwnd"].call_count < 10

    def test_a_launcher_that_hands_off_and_exits_keeps_waiting(self, patches):
        """AutoCam 3.x's launcher exits once the real GUI process is up.

        Treating the launcher's own exit as failure would abort every
        normal run, so a surviving AutoCam process overrides it.
        """
        gd = patches["group_dir"]
        patches["sub"].Popen.return_value.poll.return_value = 0
        patches["sub"].Popen.return_value.returncode = 0
        patches["gui_pids"].return_value = ({9999}, True)
        patches["find_hwnd"].side_effect = [None] * 40 + [0xCAFE] * 10

        _execute_autocam_gui_automation(
            "fake.exe", self._input(gd), os.path.join(gd, "video", "out.mp4")
        )

        patches["wait_complete"].assert_called_once()
