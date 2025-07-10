"""Tests for the autocam automation function."""

import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

from video_grouper.tray.autocam_automation import (
    run_autocam_on_file,
    _validate_autocam_inputs,
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
                mock_autocam_config.executable, input_path, output_path
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
                mock_autocam_config.executable, input_path, output_path
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
