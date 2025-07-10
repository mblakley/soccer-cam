"""Tests for the AutocamProcessor."""

import tempfile
import json
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
import pytest

from video_grouper.task_processors.autocam_processor import AutocamProcessor
from video_grouper.task_processors.tasks.autocam import AutocamTask


@pytest.fixture
def temp_storage():
    """Create a temporary storage directory for testing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def mock_config():
    """Create a mock configuration."""
    config = MagicMock()

    # Mock storage config
    storage_config = MagicMock()
    storage_config.path = "/test/storage"
    config.storage = storage_config

    # Mock autocam config
    autocam_config = MagicMock()
    autocam_config.executable = "test_autocam.exe"
    autocam_config.enabled = True
    config.autocam = autocam_config

    # Mock YouTube config
    youtube_config = MagicMock()
    youtube_config.enabled = True
    config.youtube = youtube_config

    return config


@pytest.fixture
def mock_upload_processor():
    """Create a mock upload processor."""
    processor = AsyncMock()
    processor.add_work = AsyncMock()
    return processor


@pytest.fixture
def sample_group_dir(temp_storage):
    """Create a sample group directory with state.json and video files."""
    group_dir = Path(temp_storage) / "2023.01.01-10.00.00"
    group_dir.mkdir()

    # Create state.json with "trimmed" status
    state_data = {"status": "trimmed"}
    state_file = group_dir / "state.json"
    with open(state_file, "w") as f:
        json.dump(state_data, f)

    # Create video subdirectory with raw video file
    video_dir = group_dir / "videos"
    video_dir.mkdir()

    raw_video_path = video_dir / "test-raw.mp4"
    raw_video_path.touch()

    return group_dir


class TestAutocamProcessor:
    """Test the AutocamProcessor."""

    @pytest.mark.asyncio
    async def test_autocam_processor_initialization(self, temp_storage, mock_config):
        """Test AutocamProcessor initialization."""
        processor = AutocamProcessor(temp_storage, mock_config)

        assert processor.storage_path == temp_storage
        assert processor.config == mock_config
        assert processor.upload_processor is None
        assert processor._is_first_check is True

    @pytest.mark.asyncio
    async def test_autocam_processor_with_upload_processor(
        self, temp_storage, mock_config, mock_upload_processor
    ):
        """Test AutocamProcessor initialization with upload processor."""
        processor = AutocamProcessor(temp_storage, mock_config, mock_upload_processor)

        assert processor.storage_path == temp_storage
        assert processor.config == mock_config
        assert processor.upload_processor == mock_upload_processor

    def test_queue_type(self, temp_storage, mock_config):
        """Test queue type property."""
        processor = AutocamProcessor(temp_storage, mock_config)
        from video_grouper.task_processors.queue_type import QueueType

        assert processor.queue_type == QueueType.AUTOCAM

    def test_get_state_file_name(self, temp_storage, mock_config):
        """Test getting state file name."""
        processor = AutocamProcessor(temp_storage, mock_config)
        state_file_name = processor.get_state_file_name()
        assert state_file_name == "autocam_queue_state.json"

    @pytest.mark.asyncio
    async def test_autocam_task_processing_success(
        self, temp_storage, mock_config, mock_upload_processor
    ):
        """Test successful autocam task processing."""
        processor = AutocamProcessor(temp_storage, mock_config, mock_upload_processor)

        # Create a mock autocam task
        group_dir = Path("/test/group")
        task = AutocamTask(
            group_dir=group_dir,
            input_path="/test/input.mp4",
            output_path="/test/output.mp4",
            autocam_config=mock_config.autocam,
        )

        # Mock the task execution
        with patch.object(AutocamTask, "execute") as mock_execute:
            mock_execute.return_value = True

            await processor.process_item(task)

            mock_execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_autocam_task_processing_failure(
        self, temp_storage, mock_config, mock_upload_processor
    ):
        """Test failed autocam task processing."""
        processor = AutocamProcessor(temp_storage, mock_config, mock_upload_processor)

        # Create a mock autocam task
        group_dir = Path("/test/group")
        task = AutocamTask(
            group_dir=group_dir,
            input_path="/test/input.mp4",
            output_path="/test/output.mp4",
            autocam_config=mock_config.autocam,
        )

        # Mock the task execution to fail
        with patch.object(AutocamTask, "execute") as mock_execute:
            mock_execute.return_value = False

            await processor.process_item(task)

            mock_execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_autocam_task_processing_exception(
        self, temp_storage, mock_config, mock_upload_processor
    ):
        """Test autocam task processing with exception."""
        processor = AutocamProcessor(temp_storage, mock_config, mock_upload_processor)

        # Create a mock autocam task
        group_dir = Path("/test/group")
        task = AutocamTask(
            group_dir=group_dir,
            input_path="/test/input.mp4",
            output_path="/test/output.mp4",
            autocam_config=mock_config.autocam,
        )

        # Mock the task execution to raise an exception
        with patch.object(AutocamTask, "execute") as mock_execute:
            mock_execute.side_effect = Exception("Autocam error")

            await processor.process_item(task)

            # Should complete without raising the exception

    def test_get_item_key(self, temp_storage, mock_config):
        """Test getting unique key for an AutocamTask."""
        processor = AutocamProcessor(temp_storage, mock_config)

        # Create a mock autocam task
        group_dir = Path("/test/group")
        task = AutocamTask(
            group_dir=group_dir,
            input_path="/test/input.mp4",
            output_path="/test/output.mp4",
            autocam_config=mock_config.autocam,
        )

        key = processor.get_item_key(task)
        expected_path = str(group_dir)
        assert key.startswith(f"autocam_process:{expected_path}")
        assert "autocam_process" in key
        assert expected_path in key

    def test_get_autocam_input_output_paths(self, temp_storage, mock_config):
        """Test getting autocam input and output paths."""
        processor = AutocamProcessor(temp_storage, mock_config)

        # Create a test directory structure
        test_group_dir = Path(temp_storage) / "test_group"
        test_group_dir.mkdir()

        # Create a subdirectory with a raw video file
        video_dir = test_group_dir / "videos"
        video_dir.mkdir()

        raw_video_path = video_dir / "test-raw.mp4"
        raw_video_path.touch()

        input_path, output_path = processor._get_autocam_input_output_paths(
            test_group_dir
        )

        assert input_path == str(raw_video_path)
        assert output_path == str(video_dir / "test.mp4")

    def test_get_autocam_input_output_paths_no_file(self, temp_storage, mock_config):
        """Test getting autocam paths when no raw file exists."""
        processor = AutocamProcessor(temp_storage, mock_config)

        # Create a test directory structure without raw video file
        test_group_dir = Path(temp_storage) / "test_group"
        test_group_dir.mkdir()

        with pytest.raises(FileNotFoundError):
            processor._get_autocam_input_output_paths(test_group_dir)

    @pytest.mark.asyncio
    async def test_discover_work_no_trimmed_groups(self, temp_storage, mock_config):
        """Test discover_work when no trimmed groups exist."""
        processor = AutocamProcessor(temp_storage, mock_config)

        # Create a group with non-trimmed status
        group_dir = Path(temp_storage) / "test_group"
        group_dir.mkdir()
        state_file = group_dir / "state.json"
        with open(state_file, "w") as f:
            json.dump({"status": "downloaded"}, f)

        await processor.discover_work()

        # Should not add any tasks to the queue
        assert processor.get_queue_size() == 0

    @pytest.mark.asyncio
    async def test_discover_work_with_trimmed_group(
        self, temp_storage, mock_config, sample_group_dir
    ):
        """Test discover_work when a trimmed group exists."""
        processor = AutocamProcessor(temp_storage, mock_config)

        await processor.discover_work()

        # Should add one task to the queue
        assert processor.get_queue_size() == 1

    @pytest.mark.asyncio
    async def test_discover_work_duplicate_prevention(
        self, temp_storage, mock_config, sample_group_dir
    ):
        """Test that discover_work doesn't add duplicate tasks."""
        processor = AutocamProcessor(temp_storage, mock_config)

        # Call discover_work twice
        await processor.discover_work()
        await processor.discover_work()

        # Should only have one task in the queue
        assert processor.get_queue_size() == 1

    @pytest.mark.asyncio
    async def test_handle_successful_completion(
        self, temp_storage, mock_config, mock_upload_processor
    ):
        """Test handling successful completion of an autocam task."""
        processor = AutocamProcessor(temp_storage, mock_config, mock_upload_processor)

        # Create a test group directory
        group_dir = Path(temp_storage) / "test_group"
        group_dir.mkdir()
        state_file = group_dir / "state.json"
        with open(state_file, "w") as f:
            json.dump({"status": "trimmed"}, f)

        # Create a mock autocam task
        task = AutocamTask(
            group_dir=group_dir,
            input_path="/test/input.mp4",
            output_path="/test/output.mp4",
            autocam_config=mock_config.autocam,
        )

        await processor._handle_successful_completion(task)

        # Check that state was updated
        with open(state_file, "r") as f:
            state_data = json.load(f)
        assert state_data["status"] == "autocam_complete"

        # Check that YouTube upload task was added
        mock_upload_processor.add_work.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_successful_completion_youtube_disabled(
        self, temp_storage, mock_config
    ):
        """Test handling successful completion when YouTube is disabled."""
        # Disable YouTube
        mock_config.youtube.enabled = False
        processor = AutocamProcessor(temp_storage, mock_config)

        # Create a test group directory
        group_dir = Path(temp_storage) / "test_group"
        group_dir.mkdir()
        state_file = group_dir / "state.json"
        with open(state_file, "w") as f:
            json.dump({"status": "trimmed"}, f)

        # Create a mock autocam task
        task = AutocamTask(
            group_dir=group_dir,
            input_path="/test/input.mp4",
            output_path="/test/output.mp4",
            autocam_config=mock_config.autocam,
        )

        await processor._handle_successful_completion(task)

        # Check that state was updated
        with open(state_file, "r") as f:
            state_data = json.load(f)
        assert state_data["status"] == "autocam_complete"

    @pytest.mark.asyncio
    async def test_handle_successful_completion_no_upload_processor(
        self, temp_storage, mock_config
    ):
        """Test handling successful completion when no upload processor is available."""
        processor = AutocamProcessor(temp_storage, mock_config)  # No upload processor

        # Create a test group directory
        group_dir = Path(temp_storage) / "test_group"
        group_dir.mkdir()
        state_file = group_dir / "state.json"
        with open(state_file, "w") as f:
            json.dump({"status": "trimmed"}, f)

        # Create a mock autocam task
        task = AutocamTask(
            group_dir=group_dir,
            input_path="/test/input.mp4",
            output_path="/test/output.mp4",
            autocam_config=mock_config.autocam,
        )

        await processor._handle_successful_completion(task)

        # Check that state was updated
        with open(state_file, "r") as f:
            state_data = json.load(f)
        assert state_data["status"] == "autocam_complete"

    @pytest.mark.asyncio
    async def test_add_to_youtube_queue(
        self, temp_storage, mock_config, mock_upload_processor
    ):
        """Test adding a group to the YouTube upload queue."""
        processor = AutocamProcessor(temp_storage, mock_config, mock_upload_processor)

        group_dir = Path(temp_storage) / "test_group"
        group_dir.mkdir()

        await processor._add_to_youtube_queue(group_dir)

        # Check that a task was added to the upload processor
        mock_upload_processor.add_work.assert_called_once()

    @pytest.mark.asyncio
    async def test_add_to_youtube_queue_no_upload_processor(
        self, temp_storage, mock_config
    ):
        """Test adding to YouTube queue when no upload processor is available."""
        processor = AutocamProcessor(temp_storage, mock_config)  # No upload processor

        group_dir = Path(temp_storage) / "test_group"
        group_dir.mkdir()

        await processor._add_to_youtube_queue(group_dir)

        # Should not raise an exception, just log a warning
