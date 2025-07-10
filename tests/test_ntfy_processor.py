"""
Tests for NTFY Queue Processor.
"""

import pytest
import tempfile
from unittest.mock import Mock, patch, AsyncMock

from video_grouper.task_processors.ntfy_processor import NtfyProcessor
from video_grouper.task_processors.services.ntfy_service import NtfyService
from video_grouper.utils.config import Config
from video_grouper.task_processors.base_queue_processor import QueueProcessor


@pytest.fixture
def mock_config():
    """Create a mock configuration object."""
    config = Mock(spec=Config)
    # Mock the ntfy attribute
    config.ntfy = Mock()
    config.ntfy.enabled = True
    return config


@pytest.fixture
def mock_match_info_service():
    """Create a mock match info service."""
    service = Mock()
    service.is_match_info_complete = AsyncMock(return_value=False)
    return service


@pytest.fixture
def mock_ntfy_service():
    """Create a mock NTFY service."""
    service = Mock(spec=NtfyService)
    service.get_pending_tasks.return_value = {}
    # Mock the ntfy_api attribute
    service.ntfy_api = Mock()
    service.ntfy_api.topic = "test-topic"
    service.ntfy_api.base_url = "http://localhost:8080"
    service.ntfy_api.enabled = True
    return service


@pytest.fixture
def storage_path():
    """Create a temporary storage path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


class TestNtfyProcessor:
    """Test NTFY queue processor functionality."""

    def test_init(
        self, mock_config, mock_ntfy_service, mock_match_info_service, storage_path
    ):
        """Test processor initialization."""
        processor = NtfyProcessor(
            storage_path=storage_path,
            config=mock_config,
            ntfy_service=mock_ntfy_service,
            match_info_service=mock_match_info_service,
            poll_interval=30,
        )

        assert processor.ntfy_service == mock_ntfy_service
        assert processor.video_processor is None

    @pytest.mark.asyncio
    async def test_startup_with_no_pending_requests(
        self, mock_config, mock_ntfy_service, mock_match_info_service, storage_path
    ):
        """Test startup when there are no pending requests."""
        mock_ntfy_service.get_pending_tasks.return_value = {}

        processor = NtfyProcessor(
            storage_path=storage_path,
            config=mock_config,
            ntfy_service=mock_ntfy_service,
            match_info_service=mock_match_info_service,
        )

        with patch.object(
            processor,
            "_process_pending_requests_on_startup",
            wraps=processor._process_pending_requests_on_startup,
        ) as mock_startup:
            with patch.object(
                QueueProcessor, "start", new_callable=AsyncMock
            ) as mock_parent_start:
                await processor.start()
                # Should call startup processing
                mock_startup.assert_called_once()
                # Should call parent start method
                mock_parent_start.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_startup_with_pending_requests(
        self, mock_config, mock_ntfy_service, mock_match_info_service, storage_path
    ):
        """Test startup when there are pending requests."""
        pending_tasks = {
            "/test/dir1": {
                "task_type": "team_info",
                "status": "queued",
                "task_metadata": {"task_type": "team_info"},
            },
            "/test/dir2": {
                "task_type": "playlist_name",
                "status": "in_progress",
                "task_metadata": {
                    "task_type": "playlist_name",
                    "team_name": "Test Team",
                },
            },
        }
        mock_ntfy_service.get_pending_tasks.return_value = pending_tasks

        processor = NtfyProcessor(
            storage_path=storage_path,
            config=mock_config,
            ntfy_service=mock_ntfy_service,
            match_info_service=mock_match_info_service,
        )

        with patch.object(processor, "_recreate_queued_task") as mock_recreate_queued:
            with patch.object(
                QueueProcessor, "start", new_callable=AsyncMock
            ) as mock_parent_start:
                await processor.start()
                # Should recreate queued task only
                mock_recreate_queued.assert_called_once_with(
                    "/test/dir1",
                    "team_info",
                    {"task_type": "team_info"},
                )
                # Should call parent start method
                mock_parent_start.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_startup_with_invalid_format_clears_inputs(
        self, mock_config, mock_ntfy_service, mock_match_info_service, storage_path
    ):
        """Test startup with invalid format inputs clears them."""
        # Legacy format without proper status
        pending_tasks = {
            "/test/dir1": {"task_type": "team_info", "task_metadata": {}},
            "/test/dir2": {
                "task_type": "playlist_name",
                "task_metadata": {"team_name": "Test Team"},
            },
        }
        mock_ntfy_service.get_pending_tasks.return_value = pending_tasks

        processor = NtfyProcessor(
            storage_path=storage_path,
            config=mock_config,
            ntfy_service=mock_ntfy_service,
            match_info_service=mock_match_info_service,
        )

        with patch.object(
            QueueProcessor, "start", new_callable=AsyncMock
        ) as mock_parent_start:
            await processor.start()

            # Should clear both invalid inputs
            assert mock_ntfy_service.clear_pending_task.call_count == 2
            mock_ntfy_service.clear_pending_task.assert_any_call("/test/dir1")
            mock_ntfy_service.clear_pending_task.assert_any_call("/test/dir2")

            # Should call parent start method
            mock_parent_start.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_check_match_info_completion_populated(
        self, mock_config, mock_ntfy_service, mock_match_info_service, storage_path
    ):
        """Test checking match info completion when populated."""
        group_dir = "/test/dir"

        # Mock the match info service to return True for populated match info
        mock_match_info_service.is_match_info_complete.return_value = True

        processor = NtfyProcessor(
            storage_path=storage_path,
            config=mock_config,
            ntfy_service=mock_ntfy_service,
            match_info_service=mock_match_info_service,
        )

        with patch(
            "video_grouper.models.MatchInfo.get_or_create"
        ) as mock_get_or_create:
            mock_match_info = Mock()
            mock_match_info.is_populated.return_value = True
            mock_get_or_create.return_value = (mock_match_info, Mock())

            await processor._check_match_info_completion(group_dir)

            # Should mark as processed
            mock_ntfy_service.mark_as_processed.assert_called_once_with(group_dir)

    @pytest.mark.asyncio
    async def test_check_match_info_completion_not_populated(
        self, mock_config, mock_ntfy_service, mock_match_info_service, storage_path
    ):
        """Test checking match info completion when not populated."""
        group_dir = "/test/dir"

        processor = NtfyProcessor(
            storage_path=storage_path,
            config=mock_config,
            ntfy_service=mock_ntfy_service,
            match_info_service=mock_match_info_service,
        )

        with patch(
            "video_grouper.models.MatchInfo.get_or_create"
        ) as mock_get_or_create:
            mock_match_info = Mock()
            mock_match_info.is_populated.return_value = False
            mock_get_or_create.return_value = (mock_match_info, Mock())

            await processor._check_match_info_completion(group_dir)

            # Should not mark as processed
            mock_ntfy_service.mark_as_processed.assert_not_called()
