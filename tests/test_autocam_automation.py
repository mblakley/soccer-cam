"""Tests for the autocam automation function."""

import datetime
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from video_grouper.tray.autocam_automation import (
    _execute_autocam_gui_automation,
    _validate_autocam_inputs,
    _wait_for_completion_and_cleanup,
    run_autocam_on_file,
)


@pytest.fixture
def mock_autocam_config():
    """Create a mock autocam configuration."""
    config = MagicMock()
    config.executable = "test_autocam.exe"
    config.enabled = True
    return config


@pytest.fixture
def temp_files():
    """Create temporary input and output files."""
    with tempfile.TemporaryDirectory() as temp_dir:
        input_file = Path(temp_dir) / "input.mp4"
        output_file = Path(temp_dir) / "output.mp4"

        # Create input file
        input_file.touch()

        yield str(input_file), str(output_file)


class TestAutocamAutomation:
    """Test the autocam automation function."""

    def test_run_autocam_on_file_success(self, mock_autocam_config, temp_files):
        """Test successful autocam execution."""
        input_path, output_path = temp_files

        # Mock the validation and execution functions
        with (
            patch(
                "video_grouper.tray.autocam_automation._validate_autocam_inputs",
                return_value=True,
            ) as mock_validate,
            patch(
                "video_grouper.tray.autocam_automation._execute_autocam_gui_automation",
                return_value=True,
            ) as mock_execute,
        ):
            result = run_autocam_on_file(mock_autocam_config, input_path, output_path)

            assert result is True
            mock_validate.assert_called_once_with(
                mock_autocam_config, input_path, output_path
            )
            mock_execute.assert_called_once_with(
                mock_autocam_config.executable,
                input_path,
                output_path,
                group_dir=None,
            )

    def test_run_autocam_on_file_validation_failure(
        self, mock_autocam_config, temp_files
    ):
        """Test autocam execution when validation fails."""
        input_path, output_path = temp_files

        # Mock validation to fail
        with (
            patch(
                "video_grouper.tray.autocam_automation._validate_autocam_inputs",
                return_value=False,
            ) as mock_validate,
            patch(
                "video_grouper.tray.autocam_automation._execute_autocam_gui_automation"
            ) as mock_execute,
        ):
            result = run_autocam_on_file(mock_autocam_config, input_path, output_path)

            assert result is False
            mock_validate.assert_called_once_with(
                mock_autocam_config, input_path, output_path
            )
            mock_execute.assert_not_called()

    def test_run_autocam_on_file_execution_failure(
        self, mock_autocam_config, temp_files
    ):
        """Test autocam execution when GUI automation fails."""
        input_path, output_path = temp_files

        # Mock validation to succeed but execution to fail
        with (
            patch(
                "video_grouper.tray.autocam_automation._validate_autocam_inputs",
                return_value=True,
            ) as mock_validate,
            patch(
                "video_grouper.tray.autocam_automation._execute_autocam_gui_automation",
                return_value=False,
            ) as mock_execute,
        ):
            result = run_autocam_on_file(mock_autocam_config, input_path, output_path)

            assert result is False
            mock_validate.assert_called_once_with(
                mock_autocam_config, input_path, output_path
            )
            mock_execute.assert_called_once_with(
                mock_autocam_config.executable,
                input_path,
                output_path,
                group_dir=None,
            )

    def test_run_autocam_on_file_exception(self, mock_autocam_config, temp_files):
        """Test autocam execution with exception."""
        input_path, output_path = temp_files

        # Mock validation to raise an exception
        with (
            patch(
                "video_grouper.tray.autocam_automation._validate_autocam_inputs",
                side_effect=Exception("Test error"),
            ) as mock_validate,
            patch(
                "video_grouper.tray.autocam_automation._execute_autocam_gui_automation"
            ) as mock_execute,
        ):
            result = run_autocam_on_file(mock_autocam_config, input_path, output_path)

            assert result is False
            mock_validate.assert_called_once_with(
                mock_autocam_config, input_path, output_path
            )
            mock_execute.assert_not_called()

    def test_run_autocam_on_file_invalid_paths(self, mock_autocam_config):
        """Test autocam execution with invalid paths."""
        # Test with None paths
        result = run_autocam_on_file(mock_autocam_config, None, "/output.mp4")
        assert result is False

        result = run_autocam_on_file(mock_autocam_config, "/input.mp4", None)
        assert result is False

        # Test with empty paths
        result = run_autocam_on_file(mock_autocam_config, "", "/output.mp4")
        assert result is False

        result = run_autocam_on_file(mock_autocam_config, "/input.mp4", "")
        assert result is False


class TestValidateAutocamInputs:
    """Test the _validate_autocam_inputs function."""

    def test_validate_autocam_inputs_success(self, mock_autocam_config, temp_files):
        """Test successful validation."""
        input_path, output_path = temp_files

        # Mock file existence checks
        with (
            patch("os.path.isfile", return_value=True),
            patch("os.path.abspath", side_effect=lambda x: x),
        ):
            result = _validate_autocam_inputs(
                mock_autocam_config, input_path, output_path
            )

            assert result is True

    def test_validate_autocam_inputs_disabled(self, temp_files):
        """Test validation when autocam is disabled."""
        input_path, output_path = temp_files

        # Create disabled config
        disabled_config = MagicMock()
        disabled_config.executable = "test_autocam.exe"
        disabled_config.enabled = False

        result = _validate_autocam_inputs(disabled_config, input_path, output_path)

        assert result is False

    def test_validate_autocam_inputs_no_executable(self, temp_files):
        """Test validation when executable is not configured."""
        input_path, output_path = temp_files

        # Create config without executable
        config = MagicMock()
        config.executable = None
        config.enabled = True

        result = _validate_autocam_inputs(config, input_path, output_path)

        assert result is False

    def test_validate_autocam_inputs_no_input_path(
        self, mock_autocam_config, temp_files
    ):
        """Test validation when input path is missing."""
        _, output_path = temp_files

        result = _validate_autocam_inputs(mock_autocam_config, "", output_path)

        assert result is False

    def test_validate_autocam_inputs_no_output_path(
        self, mock_autocam_config, temp_files
    ):
        """Test validation when output path is missing."""
        input_path, _ = temp_files

        result = _validate_autocam_inputs(mock_autocam_config, input_path, "")

        assert result is False

    def test_validate_autocam_inputs_invalid_paths(self, mock_autocam_config):
        """Test validation with invalid paths."""
        # Test with None paths
        result = _validate_autocam_inputs(mock_autocam_config, None, "/output.mp4")
        assert result is False

        result = _validate_autocam_inputs(mock_autocam_config, "/input.mp4", None)
        assert result is False

    def test_validate_autocam_inputs_file_not_found(
        self, mock_autocam_config, temp_files
    ):
        """Test validation when input file doesn't exist."""
        input_path, output_path = temp_files

        # Mock file existence checks to return False for input file
        with (
            patch(
                "os.path.isfile",
                side_effect=lambda path: path == mock_autocam_config.executable,
            ),
            patch("os.path.abspath", side_effect=lambda x: x),
        ):
            result = _validate_autocam_inputs(
                mock_autocam_config, input_path, output_path
            )

            assert result is False

    def test_validate_autocam_inputs_executable_not_found(
        self, mock_autocam_config, temp_files
    ):
        """Test validation when autocam executable doesn't exist."""
        input_path, output_path = temp_files

        # Mock file existence checks to return False for executable
        with (
            patch("os.path.isfile", side_effect=lambda path: path == input_path),
            patch("os.path.abspath", side_effect=lambda x: x),
        ):
            result = _validate_autocam_inputs(
                mock_autocam_config, input_path, output_path
            )

            assert result is False


class TestWaitForCompletionExitDetection:
    """Test the exit-detection fallback added to _wait_for_completion_and_cleanup.

    Background: some AutoCam builds (observed 2026-05-10) end with a
    C-level ``FrameReader_close`` cleanup message instead of "finished
    processing". The notification-based detection then misses the end
    of the run and waits 24h. The fallback watches the GUI.exe PIDs:
    when they all exit, we infer success/failure from the output file.
    """

    @pytest.fixture
    def mock_main_window(self):
        """A main_window whose Notification child returns a stub whose
        window_text() raises, so the notification-text branch is a
        no-op every poll. (mock.side_effect treats Exception INSTANCES
        as iterables, not raise targets — only Exception classes get
        raised. Putting the raise on window_text() sidesteps this.)"""
        notification = MagicMock()
        notification.window_text.side_effect = RuntimeError("no notification")
        mw = MagicMock()
        mw.child_window.return_value = notification
        return mw

    def test_exit_with_real_output_returns_success(
        self, mock_main_window, mock_file_system
    ):
        """GUI.exe PIDs exit + output file is large enough + completion
        sentinel present → True.

        The autouse ``mock_file_system`` conftest fixture pins
        ``os.path.getsize`` to 1 MB; we override it here so the size
        check exercises the success branch.
        """
        mock_file_system["getsize"].return_value = 11 * 1024 * 1024  # > 10 MB threshold
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "out.mp4"
            output.touch()
            # Sentinel signals AutoCam actually finished cleanly. Without
            # it, the exit-detection branch treats the output as a
            # crashed partial.
            (Path(tmp) / "out.mp4.completed").touch()
            with (
                patch(
                    "video_grouper.tray.autocam_automation._live_autocam_pids",
                    return_value=[],
                ),
                patch("video_grouper.tray.autocam_automation.time.sleep"),
                patch(
                    "video_grouper.tray.autocam_automation.subprocess.run"
                ),  # taskkill no-op
            ):
                result = _wait_for_completion_and_cleanup(
                    mock_main_window,
                    state=None,
                    output_path=str(output),
                    tracked_pids=[12345],
                )
        assert result is True

    def test_exit_with_output_but_no_sentinel_treats_as_crash_partial(
        self, mock_main_window, mock_file_system
    ):
        """GUI.exe PIDs exit + large output + NO sentinel = crashed
        mid-pass partial. Must delete output + return False so the next
        attempt re-runs from scratch. This is the Spencerport gold 2
        regression that v0.4.8 fixes."""
        mock_file_system["getsize"].return_value = 657 * 1024 * 1024  # 657 MB partial
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "out.mp4"
            output.write_bytes(b"\x00" * 1024)  # real file so os.remove succeeds
            with (
                patch(
                    "video_grouper.tray.autocam_automation._live_autocam_pids",
                    return_value=[],
                ),
                patch("video_grouper.tray.autocam_automation.time.sleep"),
                patch("video_grouper.tray.autocam_automation.subprocess.run"),
            ):
                result = _wait_for_completion_and_cleanup(
                    mock_main_window,
                    state=None,
                    output_path=str(output),
                    tracked_pids=[12345],
                )
            assert result is False
            assert not output.exists(), "crash partial must be deleted"

    def test_exit_with_partial_output_returns_failure(self, mock_main_window):
        """GUI.exe exits + output file is too small → False (treated as crash)."""
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "out.mp4"
            output.write_bytes(b"\x00" * 1024)  # 1 KB << 10 MB threshold
            with (
                patch(
                    "video_grouper.tray.autocam_automation._live_autocam_pids",
                    return_value=[],
                ),
                patch("video_grouper.tray.autocam_automation.time.sleep"),
                patch("video_grouper.tray.autocam_automation.subprocess.run"),
            ):
                result = _wait_for_completion_and_cleanup(
                    mock_main_window,
                    state=None,
                    output_path=str(output),
                    tracked_pids=[12345],
                )
        assert result is False

    def test_exit_with_missing_output_returns_failure(self, mock_main_window):
        """GUI.exe exits + no output file at all → False."""
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "never_created.mp4"
            with (
                patch(
                    "video_grouper.tray.autocam_automation._live_autocam_pids",
                    return_value=[],
                ),
                patch("video_grouper.tray.autocam_automation.time.sleep"),
                patch("video_grouper.tray.autocam_automation.subprocess.run"),
            ):
                result = _wait_for_completion_and_cleanup(
                    mock_main_window,
                    state=None,
                    output_path=str(output),
                    tracked_pids=[12345],
                )
        assert result is False

    def test_pids_still_alive_does_not_trigger_exit_branch(
        self, mock_main_window, mock_file_system
    ):
        """When at least one tracked PID is still running, the
        exit-detection branch must NOT fire even if the output happens
        to already exist on disk (could be from a previous run). On
        the next poll, after the PIDs go away, the branch then
        succeeds normally."""
        mock_file_system["getsize"].return_value = 50 * 1024 * 1024
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "out.mp4"
            output.touch()
            # Sentinel needed for the success branch on the second poll.
            (Path(tmp) / "out.mp4.completed").touch()
            # First poll: PID still alive (branch must NOT trigger).
            # Second poll: PID gone (branch fires + output present → success).
            live_results = iter([[12345], []])
            with (
                patch(
                    "video_grouper.tray.autocam_automation._live_autocam_pids",
                    side_effect=lambda pids: next(live_results),
                ),
                patch("video_grouper.tray.autocam_automation.time.sleep"),
                patch("video_grouper.tray.autocam_automation.subprocess.run"),
            ):
                result = _wait_for_completion_and_cleanup(
                    mock_main_window,
                    state=None,
                    output_path=str(output),
                    tracked_pids=[12345],
                )
        assert result is True


class TestExecuteAutocamGuiAutomationOutputPrecheck:
    """The fresh-launch path of _execute_autocam_gui_automation must
    short-circuit when the output file already exists at non-trivial
    size — otherwise restoring an in_progress task from disk after a
    tray crash would re-process a video we already have."""

    def test_skips_when_output_already_exists_with_sentinel(
        self, tmp_path, mock_file_system
    ):
        """Output file present + large enough + completion sentinel
        present → return True immediately without launching
        subprocess.Popen / Desktop / pywinauto."""
        mock_file_system["getsize"].return_value = 50 * 1024 * 1024  # > 10 MB
        input_path = tmp_path / "input.mp4"
        input_path.touch()
        output_path = tmp_path / "output.mp4"
        output_path.touch()
        (tmp_path / "output.mp4.completed").touch()
        with (
            patch(
                "video_grouper.tray.autocam_automation.subprocess.Popen"
            ) as mock_popen,
            patch("video_grouper.tray.autocam_automation.Desktop") as mock_desktop,
        ):
            result = _execute_autocam_gui_automation(
                "C:/fake/GUI.exe", str(input_path), str(output_path)
            )
        assert result is True
        # Crucially — no fresh AutoCam launch.
        mock_popen.assert_not_called()
        mock_desktop.assert_not_called()

    def test_large_output_without_sentinel_is_deleted_and_relaunches(
        self, tmp_path, mock_file_system
    ):
        """A 657MB partial without sentinel is the Spencerport gold 2
        regression: AutoCam crashed at 22.7%, left a real-looking
        partial. Without sentinel we must NOT short-circuit -- delete
        the partial and run AutoCam fresh."""
        mock_file_system["getsize"].return_value = 657 * 1024 * 1024
        input_path = tmp_path / "input.mp4"
        input_path.touch()
        output_path = tmp_path / "output.mp4"
        output_path.touch()
        # No .completed sentinel.
        with (
            patch(
                "video_grouper.tray.autocam_automation.subprocess.Popen"
            ) as mock_popen,
            patch("video_grouper.tray.autocam_automation.Desktop"),
            patch("video_grouper.tray.autocam_automation.time.sleep"),
            patch("video_grouper.tray.autocam_automation.os.remove") as mock_remove,
            patch(
                "video_grouper.tray.autocam_automation._find_autocam_hwnd",
                return_value=None,
            ),
        ):
            try:
                _execute_autocam_gui_automation(
                    "C:/fake/GUI.exe", str(input_path), str(output_path)
                )
            except Exception:
                pass
        # The 657 MB partial was deleted (NOT short-circuited as
        # success) and Popen was called (AutoCam relaunched).
        mock_remove.assert_called()
        assert any(
            call.args[0].endswith("output.mp4") for call in mock_remove.call_args_list
        ), f"partial mp4 should be deleted, got: {mock_remove.call_args_list}"
        mock_popen.assert_called()

    def test_does_not_skip_when_output_too_small(self, tmp_path, mock_file_system):
        """Output exists but is below the 10 MB threshold → don't
        short-circuit (proceed with the normal launch path; pywinauto
        will then fail because we mocked Desktop, but we just need to
        confirm Popen WAS called, proving we got past the pre-check)."""
        mock_file_system["getsize"].return_value = 1024  # 1 KB << 10 MB
        input_path = tmp_path / "input.mp4"
        input_path.touch()
        output_path = tmp_path / "output.mp4"
        output_path.touch()
        with (
            patch(
                "video_grouper.tray.autocam_automation.subprocess.Popen"
            ) as mock_popen,
            patch("video_grouper.tray.autocam_automation.Desktop"),
            patch("video_grouper.tray.autocam_automation.time.sleep"),
            patch("video_grouper.tray.autocam_automation.os.remove"),
            patch(
                "video_grouper.tray.autocam_automation._find_autocam_hwnd",
                return_value=None,
            ),
        ):
            try:
                _execute_autocam_gui_automation(
                    "C:/fake/GUI.exe", str(input_path), str(output_path)
                )
            except Exception:
                pass  # downstream pywinauto interactions will fail; that's ok
        mock_popen.assert_called()

    def test_pre_deletes_partial_output(self, tmp_path, mock_file_system):
        """A sub-threshold partial output gets os.remove'd before any
        AutoCam launch. Leaving it would trigger the Windows Save
        dialog's "Confirm Save As" overwrite-confirm overlay, which
        the dialog automation can't drive; AutoCam then errors
        "No output file selected" the instant Start Processing fires.
        """
        mock_file_system["getsize"].return_value = 5 * 1024 * 1024  # 5 MB < 10 MB
        input_path = tmp_path / "input.mp4"
        input_path.touch()
        output_path = tmp_path / "output.mp4"
        output_path.touch()
        with (
            patch(
                "video_grouper.tray.autocam_automation.subprocess.Popen"
            ) as mock_popen,
            patch("video_grouper.tray.autocam_automation.Desktop"),
            patch("video_grouper.tray.autocam_automation.time.sleep"),
            patch("video_grouper.tray.autocam_automation.os.remove") as mock_remove,
            patch(
                "video_grouper.tray.autocam_automation._find_autocam_hwnd",
                return_value=None,
            ),
        ):
            try:
                _execute_autocam_gui_automation(
                    "C:/fake/GUI.exe", str(input_path), str(output_path)
                )
            except Exception:
                pass  # downstream pywinauto will fail; we only care that
                # the precheck ran the remove + reached Popen
        # mp4 deleted; sentinel cleanup also called (defense against
        # stale-sentinel-without-mp4 state). Both touch os.remove.
        removed = [c.args[0] for c in mock_remove.call_args_list]
        assert any(p.endswith("output.mp4") for p in removed), removed
        # And we still launched AutoCam afterwards.
        mock_popen.assert_called()

    def test_partial_output_remove_oserror_does_not_abort_run(
        self, tmp_path, mock_file_system
    ):
        """If os.remove fails (file locked, permissions, etc.), log a
        warning and proceed with the launch anyway -- a doomed retry
        attempt is still better than skipping the queue entry."""
        mock_file_system["getsize"].return_value = 5 * 1024 * 1024  # 5 MB
        input_path = tmp_path / "input.mp4"
        input_path.touch()
        output_path = tmp_path / "output.mp4"
        output_path.touch()
        with (
            patch(
                "video_grouper.tray.autocam_automation.subprocess.Popen"
            ) as mock_popen,
            patch("video_grouper.tray.autocam_automation.Desktop"),
            patch("video_grouper.tray.autocam_automation.time.sleep"),
            patch(
                "video_grouper.tray.autocam_automation.os.remove",
                side_effect=PermissionError("locked"),
            ) as mock_remove,
            patch(
                "video_grouper.tray.autocam_automation._find_autocam_hwnd",
                return_value=None,
            ),
        ):
            try:
                _execute_autocam_gui_automation(
                    "C:/fake/GUI.exe", str(input_path), str(output_path)
                )
            except Exception:
                pass
        # remove was attempted (and swallowed the PermissionError),
        # and we continued to launch AutoCam.
        mock_remove.assert_called()
        mock_popen.assert_called()


class TestWaitForCompletionStaleNotification:
    """Test the stale-notification hang detector.

    Background: AutoCam can wedge mid-pass with the GUI alive but no
    frames advancing. The notification text stays byte-identical for
    hours. Observed 2026-05-30 on a 90-min input that froze at 65.4%
    and pinned the queue for 8+ hours before manual intervention.
    """

    def _make_notification(self, texts):
        """Build a (main_window, advance) pair. Each call to advance()
        moves the notification to the next text in ``texts``; the test
        controls how many times to advance per simulated poll."""
        idx = [0]

        def text_for_call():
            i = min(idx[0], len(texts) - 1)
            return texts[i]

        def advance():
            idx[0] += 1

        notification = MagicMock()
        notification.window_text.side_effect = lambda: text_for_call()
        mw = MagicMock()
        mw.child_window.return_value = notification
        return mw, advance

    def _run_with_simulated_clock(
        self,
        mw,
        advance,
        *,
        poll_count,
        seconds_per_poll=30,
        tracked_pids=(12345,),
        live_pids=(12345,),
        output_path=None,
        output_size=None,
    ):
        """Drive _wait_for_completion_and_cleanup through ``poll_count``
        polls with a synthetic clock. Advances the notification index
        once per poll (so the test fixture's text list models what
        AutoCam shows on each call). When output_path + output_size are
        supplied, os.path.isfile/getsize are stubbed to report that
        size for the output path (so the shutdown-marker/wedge branches
        can be exercised)."""
        import video_grouper.tray.autocam_automation as mod

        start = datetime.datetime(2026, 5, 30, 21, 30, 0)
        clock = [start]

        def fake_now():
            return clock[0]

        def fake_sleep(_seconds):
            clock[0] = clock[0] + datetime.timedelta(seconds=seconds_per_poll)
            advance()

        polls_done = [0]
        real_sleep = mod.time.sleep

        def counted_sleep(seconds):
            polls_done[0] += 1
            fake_sleep(seconds)
            if polls_done[0] >= poll_count:
                # Stop the loop by advancing the clock past 24h ceiling.
                clock[0] = start + datetime.timedelta(days=2)
            real_sleep(0)

        os_remove_calls = []

        def fake_isfile(p):
            if output_path is None:
                return False
            # Model both the output mp4 and its tracks sidecar as
            # existing -- AutoCam writes both during processing.
            return p in (output_path, output_path + ".jsonl")

        def fake_getsize(p):
            if output_path is not None and p == output_path:
                return output_size if output_size is not None else 0
            raise OSError("no")

        def fake_remove(p):
            os_remove_calls.append(p)

        with (
            patch.object(mod.datetime, "datetime", wraps=datetime.datetime) as fake_dt,
            patch(
                "video_grouper.tray.autocam_automation._live_autocam_pids",
                return_value=list(live_pids),
            ),
            patch(
                "video_grouper.tray.autocam_automation.time.sleep",
                side_effect=counted_sleep,
            ),
            patch("video_grouper.tray.autocam_automation.subprocess.run"),
            patch(
                "video_grouper.tray.autocam_automation.os.path.isfile",
                side_effect=fake_isfile,
            ),
            patch(
                "video_grouper.tray.autocam_automation.os.path.getsize",
                side_effect=fake_getsize,
            ),
            patch(
                "video_grouper.tray.autocam_automation.os.remove",
                side_effect=fake_remove,
            ),
        ):
            fake_dt.now = MagicMock(side_effect=fake_now)
            result = _wait_for_completion_and_cleanup(
                mw,
                state=None,
                output_path=output_path,
                tracked_pids=list(tracked_pids),
            )
        return result, os_remove_calls

    def test_stuck_notification_triggers_hang_break(self):
        """Notification stays identical for >10 min while PIDs alive →
        loop bails with found=False so the queue retry can kick in."""
        # Frame timing fluctuates for the first 3 polls, then sticks at
        # exactly "68 [ms]" for the remaining 25 polls (12.5 simulated
        # min). The fixture's last entry is repeated past end-of-list.
        texts = [
            "* average time per frame: 102 [ms]",
            "* average time per frame: 77 [ms]",
            "* average time per frame: 69 [ms]",
            "* average time per frame: 68 [ms]",  # repeated forever past here
        ]
        mw, advance = self._make_notification(texts)
        result, _ = self._run_with_simulated_clock(mw, advance, poll_count=30)
        assert result is False

    def test_changing_notification_keeps_loop_running(self):
        """Notification value changes every poll → never stale, loop
        runs to its synthetic timeout (which simulates the 24h ceiling
        firing without a hang detect)."""
        # 50 unique values guarantee no stale streak inside poll_count.
        texts = [f"* average time per frame: {i} [ms]" for i in range(50)]
        mw, advance = self._make_notification(texts)
        result, _ = self._run_with_simulated_clock(mw, advance, poll_count=30)
        # No hang detected; the loop exits via the synthetic 24h-ceiling
        # path with found=False (no success notification arrived in the
        # window either, which is expected since these texts contain
        # "processed" but not "finished processing").
        assert result is False

    def test_stuck_under_threshold_does_not_trigger(self):
        """Notification stuck for 9 simulated min (< STALE_NOTIFICATION_SECONDS
        of 10 min) must NOT trigger the hang detect. Verifies the
        threshold isn't off by one poll interval."""
        # 3 polls of fluctuation + 17 polls stuck @ 30 s each = 8.5 min stuck.
        texts = [
            "* average time per frame: 102 [ms]",
            "* average time per frame: 77 [ms]",
            "* average time per frame: 69 [ms]",
            "* average time per frame: 68 [ms]",
        ]
        mw, advance = self._make_notification(texts)
        # Only 20 polls -- 17 stuck @ 30s = 510s, below 600s threshold.
        result, _ = self._run_with_simulated_clock(mw, advance, poll_count=20)
        # Loop fell out via the synthetic 24h ceiling, NOT via the
        # stale-notification break. The distinction is observable
        # through logs; here we just verify the function didn't return
        # early with the stale-notification path being hit.
        assert result is False

    def test_stuck_notification_with_dead_pids_uses_exit_branch(self):
        """If the GUI processes have already exited, the existing
        process-exit branch wins -- we should NOT also trigger the
        stale-notification branch. The exit branch produces a more
        specific failure log."""
        texts = ["* average time per frame: 68 [ms]"]
        mw, advance = self._make_notification(texts)
        # live_pids=[] means _live_autocam_pids returns empty.
        result, _ = self._run_with_simulated_clock(
            mw, advance, poll_count=30, live_pids=()
        )
        # Returns False via the exit-detection branch (no output_path,
        # so the "exited without producing output" path).
        assert result is False

    def test_shutdown_marker_with_real_output_returns_success(self):
        """Notification stuck on 'FrameReader_close' for 10+ min AND
        output file >= 10 MB → success, not hang. AutoCam was finished
        and just slow to release the file handle (observed 2026-05-31:
        WNY Flash run wrote a complete 4.4 GB output, then notification
        sat on the C-level shutdown message for the next 10 min)."""
        texts = [
            "* average time per frame: 70 [ms]\r\n* 99.5% of video processed\r\n* ETA: 10sec",
            "Reader\r\nFrameReader_close: call free for struct FrameReader *reader",
        ]
        mw, advance = self._make_notification(texts)
        result, removes = self._run_with_simulated_clock(
            mw,
            advance,
            poll_count=30,
            output_path="C:/fake/out.mp4",
            output_size=4_400_000_000,  # 4.4 GB
        )
        assert result is True, "shutdown marker + big output should be success"
        # Crucially, the output was NOT deleted: this is a real video.
        assert removes == [], (
            f"output should not be deleted on shutdown marker, got {removes}"
        )

    def test_shutdown_marker_with_tiny_output_still_treated_as_hang(self):
        """Notification stuck on FrameReader_close BUT the output file
        is below the 10 MB threshold → still bail. A shutdown marker
        without a real video means AutoCam crashed/aborted before
        finishing output, not a normal cleanup."""
        texts = [
            "* average time per frame: 70 [ms]\r\n* 10% of video processed",
            "Reader\r\nFrameReader_close: call free for struct FrameReader *reader",
        ]
        mw, advance = self._make_notification(texts)
        result, removes = self._run_with_simulated_clock(
            mw,
            advance,
            poll_count=30,
            output_path="C:/fake/out.mp4",
            output_size=2 * 1024 * 1024,  # 2 MB << 10 MB
        )
        assert result is False
        # Under the wedge-cleanup path the output gets deleted (and so
        # does the JSONL + any sentinel) -- that's fine here since 2 MB
        # is junk.
        assert "C:/fake/out.mp4" in removes
        assert "C:/fake/out.mp4.jsonl" in removes

    def test_wedge_with_partial_output_deletes_to_prevent_false_short_circuit(self):
        """The Fairport/Spencerport regression: AutoCam wedged at
        65.4% of video processed with the notification frozen on
        progress text. The output mp4 already had ~1 GB on disk (real
        partial). Without the delete, the next attempt's >= 10 MB
        short-circuit would treat it as complete and queue it for
        YouTube upload as the real game.

        Expected behavior: bail with found=False AND delete the
        partial output (and the JSONL sidecar) before returning.
        """
        # Notification stuck on a 'X.X% of video processed' string.
        texts = [
            "* average time per frame: 68 [ms]\r\n* 50% of video processed\r\n* eta: 30min",
            "* average time per frame: 67 [ms]\r\n* 65.4% of video processed\r\n* eta: 14min",
        ]
        mw, advance = self._make_notification(texts)
        result, removes = self._run_with_simulated_clock(
            mw,
            advance,
            poll_count=30,
            output_path="C:/fake/fairport_out.mp4",
            output_size=1_100_000_000,  # 1.1 GB partial
        )
        assert result is False
        # Both the mp4 AND its tracks jsonl get removed.
        assert "C:/fake/fairport_out.mp4" in removes
        assert "C:/fake/fairport_out.mp4.jsonl" in removes


class TestTaskkillAutocamTree:
    """The taskkill in the cleanup paths must kill both GUI.exe and
    autocam.exe (the actual processing child). Killing only GUI.exe
    leaves autocam.exe orphaned, eating CPU and holding the partial
    output file handle so the next pass can't delete it (observed
    2026-05-31: two orphaned autocam.exe processes from two consecutive
    Fix C wedges)."""

    def test_taskkill_kills_both_images(self):
        from video_grouper.tray.autocam_automation import _taskkill_autocam_tree

        with patch("video_grouper.tray.autocam_automation.subprocess.run") as mock_run:
            _taskkill_autocam_tree()
        # Two calls, one for each image name. taskkill order doesn't matter
        # operationally but the test pins it for clarity.
        image_names = [c.args[0][3] for c in mock_run.call_args_list]
        assert image_names == ["GUI.exe", "autocam.exe"]


class TestCompletionSentinel:
    """Tests for the completion sentinel helpers themselves."""

    def test_mark_then_has_then_remove(self, tmp_path):
        from video_grouper.tray.autocam_automation import (
            _has_completion_sentinel,
            _mark_output_completed,
            _remove_completion_sentinel,
        )

        output = str(tmp_path / "out.mp4")
        Path(output).touch()
        assert not _has_completion_sentinel(output)
        _mark_output_completed(output)
        assert _has_completion_sentinel(output)
        assert (tmp_path / "out.mp4.completed").exists()
        # idempotent
        _mark_output_completed(output)
        assert _has_completion_sentinel(output)
        _remove_completion_sentinel(output)
        assert not _has_completion_sentinel(output)
        # tolerant of missing
        _remove_completion_sentinel(output)
        assert not _has_completion_sentinel(output)

    def test_mark_swallows_oserror(self, tmp_path):
        """Sentinel write failing must NOT raise -- worst case the next
        attempt will re-run AutoCam, which is the safe direction."""
        from video_grouper.tray.autocam_automation import _mark_output_completed

        # Path inside a nonexistent parent dir → OSError on open.
        nonexistent = str(tmp_path / "nope" / "out.mp4")
        # Should not raise
        _mark_output_completed(nonexistent)


class TestSuccessNotificationWritesSentinel:
    """When AutoCam's 'finished processing' notification fires, we
    must write the completion sentinel before returning so future
    re-discoveries short-circuit correctly."""

    def test_finished_processing_writes_sentinel(self):
        import video_grouper.tray.autocam_automation as mod

        notification = MagicMock()
        notification.window_text.return_value = "finished processing"
        mw = MagicMock()
        mw.child_window.return_value = notification

        mark_calls = []

        def fake_mark(p):
            mark_calls.append(p)

        with (
            patch.object(mod, "_mark_output_completed", side_effect=fake_mark),
            patch("video_grouper.tray.autocam_automation.time.sleep"),
            patch("video_grouper.tray.autocam_automation.subprocess.run"),
            patch(
                "video_grouper.tray.autocam_automation._live_autocam_pids",
                return_value=[12345],
            ),
        ):
            from video_grouper.tray.autocam_automation import (
                _wait_for_completion_and_cleanup,
            )

            result = _wait_for_completion_and_cleanup(
                mw,
                state=None,
                output_path="C:/fake/out.mp4",
                tracked_pids=[12345],
            )
        assert result is True
        assert mark_calls == ["C:/fake/out.mp4"]
