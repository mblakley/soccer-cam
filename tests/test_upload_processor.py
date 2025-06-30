"""Tests for the UploadProcessor."""

import os
import tempfile
import configparser
from unittest.mock import patch
import pytest

from video_grouper.task_processors.upload_processor import UploadProcessor
from video_grouper.task_processors.tasks import YoutubeUploadTask


@pytest.fixture
def temp_storage():
    """Create a temporary storage directory for testing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def mock_config():
    """Create a mock configuration object."""
    config = configparser.ConfigParser()
    config.add_section("APP")
    config.set("APP", "check_interval_seconds", "10")
    config.add_section("YOUTUBE")
    config.set("YOUTUBE", "enabled", "true")
    config.add_section("youtube.playlist.processed")
    config.set("youtube.playlist.processed", "name_format", "{my_team_name} 2023s")
    config.set("youtube.playlist.processed", "description", "Processed videos")
    config.set("youtube.playlist.processed", "privacy_status", "unlisted")
    config.add_section("youtube.playlist.raw")
    config.set(
        "youtube.playlist.raw", "name_format", "{my_team_name} 2023s - Full Field"
    )
    config.set("youtube.playlist.raw", "description", "Raw videos")
    config.set("youtube.playlist.raw", "privacy_status", "unlisted")
    return config


class TestUploadProcessor:
    """Test the UploadProcessor."""

    @pytest.mark.asyncio
    async def test_upload_processor_initialization(self, temp_storage, mock_config):
        """Test UploadProcessor initialization."""
        processor = UploadProcessor(temp_storage, mock_config)

        assert processor.storage_path == temp_storage
        assert processor.config == mock_config

    @pytest.mark.asyncio
    async def test_upload_task_processing_no_credentials(
        self, temp_storage, mock_config
    ):
        """Test upload task when credentials file doesn't exist."""
        group_dir = os.path.join(temp_storage, "2023.01.01-10.00.00")

        processor = UploadProcessor(temp_storage, mock_config)

        upload_task = YoutubeUploadTask(group_dir)
        await processor.process_item(upload_task)

        # Should complete without error (credentials check is logged but doesn't fail)

    @pytest.mark.asyncio
    async def test_upload_task_processing_success(self, temp_storage, mock_config):
        """Test successful upload task processing."""
        group_dir = "/test/group"

        processor = UploadProcessor(temp_storage, mock_config)

        # Mock the upload functionality at the task level
        with patch.object(YoutubeUploadTask, "execute") as mock_execute:
            mock_execute.return_value = True

            upload_task = YoutubeUploadTask(group_dir)
            await processor.process_item(upload_task)

            mock_execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_upload_task_processing_failure(self, temp_storage, mock_config):
        """Test failed upload task processing."""
        group_dir = "/test/group"

        processor = UploadProcessor(temp_storage, mock_config)

        # Mock the upload functionality at the task level
        with patch.object(YoutubeUploadTask, "execute") as mock_execute:
            mock_execute.return_value = False

            upload_task = YoutubeUploadTask(group_dir)
            await processor.process_item(upload_task)

            mock_execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_upload_task_processing_exception(self, temp_storage, mock_config):
        """Test upload task processing with exception."""
        group_dir = "/test/group"

        processor = UploadProcessor(temp_storage, mock_config)

        # Mock the upload functionality at the task level
        with patch.object(YoutubeUploadTask, "execute") as mock_execute:
            mock_execute.side_effect = Exception("Upload error")

            upload_task = YoutubeUploadTask(group_dir)
            await processor.process_item(upload_task)

            # Should complete without raising the exception

    @pytest.mark.asyncio
    async def test_upload_task_no_playlist_config(self, temp_storage):
        """Test upload task when no playlist configuration is available."""
        # Create config without playlist sections
        config = configparser.ConfigParser()
        config.add_section("APP")
        config.set("APP", "check_interval_seconds", "10")

        group_dir = "/test/group"

        processor = UploadProcessor(temp_storage, config)

        # Mock the upload functionality at the task level
        with patch.object(YoutubeUploadTask, "execute") as mock_execute:
            mock_execute.return_value = True

            upload_task = YoutubeUploadTask(group_dir)
            await processor.process_item(upload_task)

            mock_execute.assert_called_once()

    def test_get_item_key(self, temp_storage, mock_config):
        """Test getting unique key for BaseUploadTask."""
        processor = UploadProcessor(temp_storage, mock_config)

        # Test YouTube upload task
        youtube_task = YoutubeUploadTask(group_dir="/test/path/group")
        key = processor.get_item_key(youtube_task)
        assert key.startswith("youtube_upload:/test/path/group:")

    def test_get_state_file_name(self, temp_storage, mock_config):
        """Test getting state file name."""
        processor = UploadProcessor(temp_storage, mock_config)

        state_file_name = processor.get_state_file_name()

        assert state_file_name == "upload_queue_state.json"
