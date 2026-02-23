"""
Tests for NTFY Queue Processor.
"""

import asyncio
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
        """Test match info completion check when not populated."""
        mock_match_info_service.is_match_info_complete.return_value = False

        processor = NtfyProcessor(
            storage_path=storage_path,
            config=mock_config,
            ntfy_service=mock_ntfy_service,
            match_info_service=mock_match_info_service,
        )

        group_dir = storage_path  # Use the temp directory instead of "/test/path"
        await processor._check_match_info_completion(group_dir)

        # Should not mark as processed since match info is not complete
        mock_ntfy_service.mark_as_processed.assert_not_called()

    @pytest.mark.asyncio
    async def test_remove_completed_task_from_queue(
        self, mock_config, mock_ntfy_service, mock_match_info_service, storage_path
    ):
        """Test that completed tasks are properly removed from the queue."""
        processor = NtfyProcessor(
            storage_path=storage_path,
            config=mock_config,
            ntfy_service=mock_ntfy_service,
            match_info_service=mock_match_info_service,
        )

        # Initialize the queue
        processor._queue = Mock()
        processor._queued_items = {
            "game_start_time:/test/path:123",
            "team_info:/test/path:456",
        }

        # Mock save_state to avoid file operations
        with patch.object(processor, "save_state", new_callable=AsyncMock) as mock_save:
            await processor.remove_completed_task_from_queue(
                "/test/path", "game_start_time"
            )

            # Should remove the task from _queued_items
            assert "game_start_time:/test/path:123" not in processor._queued_items
            assert (
                "team_info:/test/path:456" in processor._queued_items
            )  # Other task should remain

            # Should save state
            mock_save.assert_called_once()


class TestNtfyProcessorBlocking:
    """Tests for process_item blocking until user responds."""

    @pytest.mark.asyncio
    async def test_process_item_blocks_until_response_event(
        self, mock_config, mock_ntfy_service, mock_match_info_service, storage_path
    ):
        """process_item should block after sending notification until the response event is set."""
        processor = NtfyProcessor(
            storage_path=storage_path,
            config=mock_config,
            ntfy_service=mock_ntfy_service,
            match_info_service=mock_match_info_service,
        )

        # Create a mock NTFY task whose execute succeeds
        mock_task = Mock()
        mock_task.execute = AsyncMock(return_value=True)
        mock_task.group_dir = "/test/dir"
        mock_task.metadata = {}
        mock_task.get_task_type.return_value = "game_start_time"

        completed = False

        async def run_process_item():
            nonlocal completed
            await processor.process_item(mock_task)
            completed = True

        task = asyncio.create_task(run_process_item())

        # Give process_item a chance to run and block
        await asyncio.sleep(0.05)
        assert not completed, "process_item should be blocked waiting for response"
        assert "/test/dir" in processor._response_events

        # Simulate response by setting the event
        processor._response_events["/test/dir"].set()

        await asyncio.wait_for(task, timeout=2.0)
        assert completed, "process_item should have completed after event was set"

    @pytest.mark.asyncio
    async def test_process_item_unblocks_via_check_match_info_completion(
        self, mock_config, mock_ntfy_service, mock_match_info_service, storage_path
    ):
        """process_item should unblock when _check_match_info_completion is called with task_completed=False."""
        mock_match_info_service.is_match_info_complete.return_value = False

        processor = NtfyProcessor(
            storage_path=storage_path,
            config=mock_config,
            ntfy_service=mock_ntfy_service,
            match_info_service=mock_match_info_service,
        )

        mock_task = Mock()
        mock_task.execute = AsyncMock(return_value=True)
        mock_task.group_dir = storage_path  # Use real path for MatchInfo access
        mock_task.metadata = {}
        mock_task.get_task_type.return_value = "team_info"

        completed = False

        async def run_process_item():
            nonlocal completed
            await processor.process_item(mock_task)
            completed = True

        task = asyncio.create_task(run_process_item())

        # Give process_item a chance to block
        await asyncio.sleep(0.05)
        assert not completed

        # Simulate the completion callback (task_completed=False triggers event)
        await processor._check_match_info_completion(
            storage_path, task_type=None, task_completed=False
        )

        await asyncio.wait_for(task, timeout=2.0)
        assert completed

    @pytest.mark.asyncio
    async def test_process_item_does_not_block_on_failure(
        self, mock_config, mock_ntfy_service, mock_match_info_service, storage_path
    ):
        """process_item should raise immediately when execute fails, without blocking."""
        processor = NtfyProcessor(
            storage_path=storage_path,
            config=mock_config,
            ntfy_service=mock_ntfy_service,
            match_info_service=mock_match_info_service,
        )

        mock_task = Mock()
        mock_task.execute = AsyncMock(return_value=False)
        mock_task.group_dir = "/test/dir"
        mock_task.metadata = {}
        mock_task.get_task_type.return_value = "game_start_time"

        with pytest.raises(RuntimeError, match="Failed to send NTFY notification"):
            await processor.process_item(mock_task)

        # No event should be registered
        assert "/test/dir" not in processor._response_events

    @pytest.mark.asyncio
    async def test_process_item_unblocks_on_shutdown(
        self, mock_config, mock_ntfy_service, mock_match_info_service, storage_path
    ):
        """process_item should return when _stopping is set during wait."""
        processor = NtfyProcessor(
            storage_path=storage_path,
            config=mock_config,
            ntfy_service=mock_ntfy_service,
            match_info_service=mock_match_info_service,
        )

        mock_task = Mock()
        mock_task.execute = AsyncMock(return_value=True)
        mock_task.group_dir = "/test/dir"
        mock_task.metadata = {}
        mock_task.get_task_type.return_value = "game_start_time"

        completed = False

        async def run_process_item():
            nonlocal completed
            await processor.process_item(mock_task)
            completed = True

        task = asyncio.create_task(run_process_item())

        # Give process_item a chance to block
        await asyncio.sleep(0.05)
        assert not completed

        # Signal shutdown
        processor._stopping = True

        await asyncio.wait_for(
            task, timeout=35.0
        )  # Must wait for one 30s timeout cycle
        assert completed

    @pytest.mark.asyncio
    async def test_response_event_cleaned_up_after_completion(
        self, mock_config, mock_ntfy_service, mock_match_info_service, storage_path
    ):
        """_response_events entry should be cleaned up after process_item returns."""
        processor = NtfyProcessor(
            storage_path=storage_path,
            config=mock_config,
            ntfy_service=mock_ntfy_service,
            match_info_service=mock_match_info_service,
        )

        mock_task = Mock()
        mock_task.execute = AsyncMock(return_value=True)
        mock_task.group_dir = "/test/dir"
        mock_task.metadata = {}
        mock_task.get_task_type.return_value = "team_info"

        async def set_event_later():
            await asyncio.sleep(0.05)
            processor._signal_response_event("/test/dir")

        asyncio.create_task(set_event_later())
        await processor.process_item(mock_task)

        # Event should be cleaned up
        assert "/test/dir" not in processor._response_events

    @pytest.mark.asyncio
    async def test_signal_response_event_no_registered_event(
        self, mock_config, mock_ntfy_service, mock_match_info_service, storage_path
    ):
        """_signal_response_event should be a no-op when no event is registered."""
        processor = NtfyProcessor(
            storage_path=storage_path,
            config=mock_config,
            ntfy_service=mock_ntfy_service,
            match_info_service=mock_match_info_service,
        )

        # Should not raise
        processor._signal_response_event("/nonexistent/dir")
        assert len(processor._response_events) == 0
