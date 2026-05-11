"""Tests for the autocam automation function."""

import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

from video_grouper.tray.autocam_automation import (
    run_autocam_on_file,
    _validate_autocam_inputs,
    _wait_for_completion_and_cleanup,
    _execute_autocam_gui_automation,
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
        """GUI.exe PIDs exit + output file is large enough → True.

        The autouse ``mock_file_system`` conftest fixture pins
        ``os.path.getsize`` to 1 MB; we override it here so the size
        check exercises the success branch.
        """
        mock_file_system["getsize"].return_value = 11 * 1024 * 1024  # > 10 MB threshold
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "out.mp4"
            output.touch()
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

    def test_skips_when_output_already_exists(self, tmp_path, mock_file_system):
        """Output file present + large enough → return True immediately
        without launching subprocess.Popen / Desktop / pywinauto."""
        mock_file_system["getsize"].return_value = 50 * 1024 * 1024  # > 10 MB
        input_path = tmp_path / "input.mp4"
        input_path.touch()
        output_path = tmp_path / "output.mp4"
        output_path.touch()
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
