"""Tests for the AutocamTask."""

import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

from video_grouper.task_processors.tasks.autocam import AutocamTask
from video_grouper.task_processors.queue_type import QueueType


@pytest.fixture
def mock_autocam_config():
    """Create a mock autocam configuration."""
    config = MagicMock()
    config.executable = "test_autocam.exe"
    config.enabled = True
    return config


@pytest.fixture
def sample_group_dir():
    """Create a sample group directory for testing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        group_dir = Path(temp_dir) / "test_group"
        group_dir.mkdir()
        yield group_dir


class TestAutocamTask:
    """Test the AutocamTask."""

    def test_autocam_task_initialization(self, mock_autocam_config, sample_group_dir):
        """Test AutocamTask initialization."""
        task = AutocamTask(
            group_dir=sample_group_dir,
            input_path="/test/input.mp4",
            output_path="/test/output.mp4",
            autocam_config=mock_autocam_config,
        )

        assert task.group_dir == sample_group_dir
        assert task.input_path == "/test/input.mp4"
        assert task.output_path == "/test/output.mp4"
        assert task.autocam_config == mock_autocam_config

    def test_queue_type(self, mock_autocam_config, sample_group_dir):
        """Test queue type property."""
        task = AutocamTask(
            group_dir=sample_group_dir,
            input_path="/test/input.mp4",
            output_path="/test/output.mp4",
            autocam_config=mock_autocam_config,
        )

        assert task.queue_type() == QueueType.TRACKING

    def test_task_type(self, mock_autocam_config, sample_group_dir):
        """Test task type property."""
        task = AutocamTask(
            group_dir=sample_group_dir,
            input_path="/test/input.mp4",
            output_path="/test/output.mp4",
            autocam_config=mock_autocam_config,
        )

        assert task.task_type == "autocam_process"

    def test_get_item_path(self, mock_autocam_config, sample_group_dir):
        """Test get_item_path method."""
        task = AutocamTask(
            group_dir=sample_group_dir,
            input_path="/test/input.mp4",
            output_path="/test/output.mp4",
            autocam_config=mock_autocam_config,
        )

        assert task.get_item_path() == str(sample_group_dir)

    def test_serialize(self, mock_autocam_config, sample_group_dir):
        """Test serialize method."""
        task = AutocamTask(
            group_dir=sample_group_dir,
            input_path="/test/input.mp4",
            output_path="/test/output.mp4",
            autocam_config=mock_autocam_config,
        )

        serialized = task.serialize()

        assert serialized["task_type"] == "autocam_process"
        assert serialized["group_dir"] == str(sample_group_dir)
        assert serialized["input_path"] == "/test/input.mp4"
        assert serialized["output_path"] == "/test/output.mp4"
        assert "autocam_config" in serialized
        assert serialized["autocam_config"]["executable"] == "test_autocam.exe"
        assert serialized["autocam_config"]["enabled"] is True

    @pytest.mark.asyncio
    async def test_execute_success(self, mock_autocam_config, sample_group_dir):
        """Test successful task execution."""
        task = AutocamTask(
            group_dir=sample_group_dir,
            input_path="/test/input.mp4",
            output_path="/test/output.mp4",
            autocam_config=mock_autocam_config,
        )

        # Mock the autocam automation function and file validation
        with (
            patch(
                "video_grouper.task_processors.tasks.autocam.autocam_task.run_autocam_on_file"
            ) as mock_run,
            patch.object(task, "_validate_video_file", return_value=True),
        ):
            mock_run.return_value = True

            result = await task.execute()

            assert result is True
            mock_run.assert_called_once_with(
                mock_autocam_config, "/test/input.mp4", "/test/output.mp4"
            )

    @pytest.mark.asyncio
    async def test_execute_failure(self, mock_autocam_config, sample_group_dir):
        """Test failed task execution."""
        task = AutocamTask(
            group_dir=sample_group_dir,
            input_path="/test/input.mp4",
            output_path="/test/output.mp4",
            autocam_config=mock_autocam_config,
        )

        # Mock the autocam automation function to return False
        with (
            patch(
                "video_grouper.task_processors.tasks.autocam.autocam_task.run_autocam_on_file"
            ) as mock_run,
            patch.object(task, "_validate_video_file", return_value=True),
        ):
            mock_run.return_value = False

            result = await task.execute()

            assert result is False
            mock_run.assert_called_once_with(
                mock_autocam_config, "/test/input.mp4", "/test/output.mp4"
            )

    @pytest.mark.asyncio
    async def test_execute_exception(self, mock_autocam_config, sample_group_dir):
        """Test task execution with exception."""
        task = AutocamTask(
            group_dir=sample_group_dir,
            input_path="/test/input.mp4",
            output_path="/test/output.mp4",
            autocam_config=mock_autocam_config,
        )

        # Mock the autocam automation function to raise an exception
        with (
            patch(
                "video_grouper.task_processors.tasks.autocam.autocam_task.run_autocam_on_file"
            ) as mock_run,
            patch.object(task, "_validate_video_file", return_value=True),
        ):
            mock_run.side_effect = Exception("Autocam automation failed")

            result = await task.execute()

            assert result is False
            mock_run.assert_called_once_with(
                mock_autocam_config, "/test/input.mp4", "/test/output.mp4"
            )

    def test_string_representation(self, mock_autocam_config, sample_group_dir):
        """Test string representation of the task."""
        task = AutocamTask(
            group_dir=sample_group_dir,
            input_path="/test/input.mp4",
            output_path="/test/output.mp4",
            autocam_config=mock_autocam_config,
        )

        str_repr = str(task)
        assert "AutocamTask" in str_repr
        assert "test_group" in str_repr

    def test_equality_and_hash(self, mock_autocam_config, sample_group_dir):
        """Test task equality and hash functionality."""
        task1 = AutocamTask(
            group_dir=sample_group_dir,
            input_path="/test/input.mp4",
            output_path="/test/output.mp4",
            autocam_config=mock_autocam_config,
        )

        task2 = AutocamTask(
            group_dir=sample_group_dir,
            input_path="/test/input.mp4",
            output_path="/test/output.mp4",
            autocam_config=mock_autocam_config,
        )

        # Tasks with same parameters should be equal
        assert task1 == task2
        assert hash(task1) == hash(task2)

        # Create a different task
        different_group = Path("/different/group")
        task3 = AutocamTask(
            group_dir=different_group,
            input_path="/test/input.mp4",
            output_path="/test/output.mp4",
            autocam_config=mock_autocam_config,
        )

        # Different tasks should not be equal
        assert task1 != task3
        assert hash(task1) != hash(task3)

    def test_get_item_key_integration(self, mock_autocam_config, sample_group_dir):
        """Test that the task works with the processor's get_item_key method."""
        from video_grouper.task_processors.autocam_processor import AutocamProcessor

        # Create a mock config
        mock_config = MagicMock()
        storage_config = MagicMock()
        storage_config.path = "/test/storage"
        mock_config.storage = storage_config
        mock_config.autocam = mock_autocam_config

        # Create processor and task
        processor = AutocamProcessor("/test/storage", mock_config)
        task = AutocamTask(
            group_dir=sample_group_dir,
            input_path="/test/input.mp4",
            output_path="/test/output.mp4",
            autocam_config=mock_autocam_config,
        )

        # Test that get_item_key works with this task
        key = processor.get_item_key(task)
        assert key.startswith("autocam_process:")
        assert str(sample_group_dir) in key
