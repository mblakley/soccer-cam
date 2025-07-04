"""
Tests for NTFY Queue Processor.
"""

import os
import pytest
import tempfile
from unittest.mock import Mock, patch, AsyncMock

from video_grouper.task_processors.ntfy_queue_processor import NtfyQueueProcessor
from video_grouper.task_processors.services.ntfy_service import NtfyService
from video_grouper.utils.config import Config
from video_grouper.task_processors.polling_processor_base import PollingProcessor


@pytest.fixture
def mock_config():
    """Create a mock configuration object."""
    config = Mock(spec=Config)
    # Mock the ntfy attribute
    config.ntfy = Mock()
    config.ntfy.enabled = True
    return config


@pytest.fixture
def mock_ntfy_service():
    """Create a mock NTFY service."""
    service = Mock(spec=NtfyService)
    service.get_pending_inputs.return_value = {}
    return service


@pytest.fixture
def storage_path():
    """Create a temporary storage path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


class TestNtfyQueueProcessor:
    """Test NTFY queue processor functionality."""

    def test_init(self, mock_config, mock_ntfy_service, storage_path):
        """Test processor initialization."""
        processor = NtfyQueueProcessor(
            storage_path=storage_path,
            config=mock_config,
            ntfy_service=mock_ntfy_service,
            poll_interval=30,
        )

        assert processor.ntfy_service == mock_ntfy_service
        assert processor.video_processor is None

    def test_set_video_processor(self, mock_config, mock_ntfy_service, storage_path):
        """Test setting video processor reference."""
        processor = NtfyQueueProcessor(
            storage_path=storage_path,
            config=mock_config,
            ntfy_service=mock_ntfy_service,
        )

        mock_video_processor = Mock()
        processor.set_video_processor(mock_video_processor)

        assert processor.video_processor == mock_video_processor

    @pytest.mark.asyncio
    async def test_startup_with_no_pending_requests(
        self, mock_config, mock_ntfy_service, storage_path
    ):
        """Test startup when there are no pending requests."""
        mock_ntfy_service.get_pending_inputs.return_value = {}

        processor = NtfyQueueProcessor(
            storage_path=storage_path,
            config=mock_config,
            ntfy_service=mock_ntfy_service,
        )

        with patch.object(
            processor,
            "_process_pending_requests_on_startup",
            wraps=processor._process_pending_requests_on_startup,
        ) as mock_startup:
            with patch.object(
                PollingProcessor, "start", new_callable=AsyncMock
            ) as mock_parent_start:
                await processor.start()
                # Should call startup processing
                mock_startup.assert_called_once()
                # Should call parent start method
                mock_parent_start.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_startup_with_pending_requests(
        self, mock_config, mock_ntfy_service, storage_path
    ):
        """Test startup when there are pending requests."""
        pending_inputs = {
            "/test/dir1": {
                "input_type": "team_info_queued",
                "metadata": {"status": "queued", "task_type": "team_info"},
            },
            "/test/dir2": {
                "input_type": "playlist_name_sent",
                "metadata": {
                    "status": "sent",
                    "task_type": "playlist_name",
                    "team_name": "Test Team",
                },
            },
        }
        mock_ntfy_service.get_pending_inputs.return_value = pending_inputs

        processor = NtfyQueueProcessor(
            storage_path=storage_path,
            config=mock_config,
            ntfy_service=mock_ntfy_service,
        )

        with patch.object(processor, "_recreate_queued_task") as mock_recreate_queued:
            with patch.object(processor, "_recreate_sent_task") as mock_recreate_sent:
                with patch.object(
                    PollingProcessor, "start", new_callable=AsyncMock
                ) as mock_parent_start:
                    await processor.start()
                    # Should recreate both types of tasks
                    mock_recreate_queued.assert_called_once_with(
                        "/test/dir1",
                        "team_info",
                        {"status": "queued", "task_type": "team_info"},
                    )
                    mock_recreate_sent.assert_called_once_with(
                        "/test/dir2",
                        "playlist_name",
                        {
                            "status": "sent",
                            "task_type": "playlist_name",
                            "team_name": "Test Team",
                        },
                    )
                    # Should call parent start method
                    mock_parent_start.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_startup_with_invalid_format_clears_inputs(
        self, mock_config, mock_ntfy_service, storage_path
    ):
        """Test startup with invalid format inputs clears them."""
        # Legacy format without proper status
        pending_inputs = {
            "/test/dir1": {"input_type": "team_info", "metadata": {}},
            "/test/dir2": {
                "input_type": "playlist_name",
                "metadata": {"team_name": "Test Team"},
            },
        }
        mock_ntfy_service.get_pending_inputs.return_value = pending_inputs

        processor = NtfyQueueProcessor(
            storage_path=storage_path,
            config=mock_config,
            ntfy_service=mock_ntfy_service,
        )

        with patch.object(
            PollingProcessor, "start", new_callable=AsyncMock
        ) as mock_parent_start:
            await processor.start()

            # Should clear both invalid inputs
            assert mock_ntfy_service.clear_pending_input.call_count == 2
            mock_ntfy_service.clear_pending_input.assert_any_call("/test/dir1")
            mock_ntfy_service.clear_pending_input.assert_any_call("/test/dir2")

            # Should call parent start method
            mock_parent_start.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_check_match_info_completion_populated(
        self, mock_config, mock_ntfy_service, storage_path
    ):
        """Test checking match info completion when populated."""
        group_dir = "/test/dir"
        match_info_path = os.path.join(storage_path, "match_info.ini")

        # Create a mock match info file
        with open(match_info_path, "w") as f:
            f.write("[MATCH_INFO]\nmy_team_name = Test Team\n")

        processor = NtfyQueueProcessor(
            storage_path=storage_path,
            config=mock_config,
            ntfy_service=mock_ntfy_service,
        )

        with patch("video_grouper.models.MatchInfo.from_file") as mock_from_file:
            mock_match_info = Mock()
            mock_match_info.is_populated.return_value = True
            mock_from_file.return_value = mock_match_info

            await processor._check_match_info_completion(group_dir)

            # Should mark as processed
            mock_ntfy_service.mark_as_processed.assert_called_once_with(group_dir)

    @pytest.mark.asyncio
    async def test_check_match_info_completion_not_populated(
        self, mock_config, mock_ntfy_service, storage_path
    ):
        """Test checking match info completion when not populated."""
        group_dir = "/test/dir"

        processor = NtfyQueueProcessor(
            storage_path=storage_path,
            config=mock_config,
            ntfy_service=mock_ntfy_service,
        )

        with patch("video_grouper.models.MatchInfo.from_file") as mock_from_file:
            mock_match_info = Mock()
            mock_match_info.is_populated.return_value = False
            mock_from_file.return_value = mock_match_info

            await processor._check_match_info_completion(group_dir)

            # Should not mark as processed
            mock_ntfy_service.mark_as_processed.assert_not_called()

    @pytest.mark.asyncio
    async def test_discover_work(self, mock_config, mock_ntfy_service, storage_path):
        """Test the main work discovery method."""
        processor = NtfyQueueProcessor(
            storage_path=storage_path,
            config=mock_config,
            ntfy_service=mock_ntfy_service,
        )

        with patch.object(processor, "_send_pending_tasks") as mock_send:
            with patch.object(processor, "_process_completed_tasks") as mock_process:
                await processor.discover_work()

                # Should call both processing methods
                mock_send.assert_called_once()
                mock_process.assert_called_once()
