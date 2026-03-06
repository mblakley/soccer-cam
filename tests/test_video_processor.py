"""Tests for the VideoProcessor."""

import asyncio
import tempfile
from unittest.mock import Mock, AsyncMock, patch
import pytest

from video_grouper.task_processors.video_processor import VideoProcessor
from video_grouper.task_processors.tasks import CombineTask, TrimTask


@pytest.fixture
def temp_storage():
    """Create a temporary storage directory for testing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def mock_config():
    """Create a mock config for testing."""
    config = Mock()
    return config


class TestVideoProcessor:
    """Test the VideoProcessor."""

    @pytest.mark.asyncio
    async def test_video_processor_initialization(self, temp_storage, mock_config):
        """Test VideoProcessor initialization."""
        mock_upload_processor = Mock()
        processor = VideoProcessor(temp_storage, mock_config, mock_upload_processor)

        assert processor.storage_path == temp_storage
        assert processor.config == mock_config

    @pytest.mark.asyncio
    async def test_combine_task_processing(self, temp_storage, mock_config):
        """Test processing a combine task."""
        group_dir = "/test/group"

        mock_upload_processor = Mock()
        processor = VideoProcessor(temp_storage, mock_config, mock_upload_processor)

        # Create combine task and mock its execute method
        combine_task = CombineTask(group_dir=group_dir)

        # Mock the task's execute method
        with patch.object(
            combine_task, "execute", new_callable=AsyncMock
        ) as mock_execute:
            mock_execute.return_value = True

            await processor.process_item(combine_task)

            mock_execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_combine_task_failed(self, temp_storage, mock_config):
        """Test processing a combine task that fails."""
        group_dir = "/test/group"

        mock_upload_processor = Mock()
        processor = VideoProcessor(temp_storage, mock_config, mock_upload_processor)

        combine_task = CombineTask(group_dir=group_dir)

        # Mock the task's execute method to return False (failed)
        with patch.object(
            combine_task, "execute", new_callable=AsyncMock
        ) as mock_execute:
            mock_execute.return_value = False

            await processor.process_item(combine_task)

            mock_execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_trim_task_processing(self, temp_storage, mock_config):
        """Test processing a trim task."""
        group_dir = "/test/group"

        mock_upload_processor = Mock()
        processor = VideoProcessor(temp_storage, mock_config, mock_upload_processor)

        # Create trim task using the new interface
        trim_task = TrimTask(
            group_dir=group_dir, start_time="00:05:00", end_time="01:35:00"
        )

        # Mock the execute method to return success
        with patch.object(trim_task, "execute", return_value=True) as mock_execute:
            await processor.process_item(trim_task)
            mock_execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_trim_task_missing_combined_file(self, temp_storage, mock_config):
        """Test trim task when combined file is missing."""
        group_dir = "/test/group"

        mock_upload_processor = Mock()
        processor = VideoProcessor(temp_storage, mock_config, mock_upload_processor)

        try:
            # Create trim task using the new interface
            trim_task = TrimTask(
                group_dir=group_dir, start_time="00:05:00", end_time="01:35:00"
            )

            # Mock the execute method to return failure
            with patch.object(trim_task, "execute", return_value=False) as mock_execute:
                await processor.process_item(trim_task)
                mock_execute.assert_called_once()
        finally:
            # Ensure processor is properly stopped
            await processor.stop()

    @pytest.mark.asyncio
    async def test_unknown_task_type(self, temp_storage, mock_config):
        """Test processing an unknown task type."""
        mock_upload_processor = Mock()
        processor = VideoProcessor(temp_storage, mock_config, mock_upload_processor)

        # Create a mock task with unknown type
        unknown_task = Mock()
        unknown_task.execute = AsyncMock(return_value=False)

        await processor.process_item(unknown_task)

        unknown_task.execute.assert_called_once()

    def test_get_item_key(self, temp_storage, mock_config):
        """Test getting unique key for FFmpegTask."""
        mock_upload_processor = Mock()
        processor = VideoProcessor(temp_storage, mock_config, mock_upload_processor)

        # Test combine task
        combine_task = CombineTask(group_dir="/test/group")
        key = processor.get_item_key(combine_task)
        assert key == "combine:/test/group"

        # Test trim task with new constructor
        trim_task = TrimTask(
            group_dir="/test/group", start_time="00:05:00", end_time="01:35:00"
        )
        key = processor.get_item_key(trim_task)
        assert key == "trim:/test/group"

    @pytest.mark.asyncio
    async def test_init_with_optional_services(self, temp_storage, mock_config):
        """Test VideoProcessor init with optional match_info_service and ntfy_processor."""
        mock_upload = Mock()
        mock_mis = Mock()
        mock_ntfy = Mock()
        processor = VideoProcessor(
            temp_storage,
            mock_config,
            mock_upload,
            match_info_service=mock_mis,
            ntfy_processor=mock_ntfy,
        )
        assert processor.match_info_service is mock_mis
        assert processor.ntfy_processor is mock_ntfy

    @pytest.mark.asyncio
    async def test_init_optional_services_default_none(self, temp_storage, mock_config):
        """Test VideoProcessor defaults optional services to None."""
        mock_upload = Mock()
        processor = VideoProcessor(temp_storage, mock_config, mock_upload)
        assert processor.match_info_service is None
        assert processor.ntfy_processor is None


class TestVideoProcessorTransitions:
    """Tests for event-driven transitions after task completion."""

    @pytest.mark.asyncio
    async def test_combine_success_triggers_match_info_service(
        self, temp_storage, mock_config
    ):
        """After CombineTask succeeds, populate_match_info_from_apis is called."""
        mock_mis = Mock()
        mock_mis.populate_match_info_from_apis = AsyncMock(return_value=True)
        mock_ntfy = Mock()
        mock_ntfy.request_match_info_for_directory = AsyncMock(return_value=True)

        processor = VideoProcessor(
            temp_storage,
            mock_config,
            Mock(),
            match_info_service=mock_mis,
            ntfy_processor=mock_ntfy,
        )

        combine_task = CombineTask(group_dir="/test/group")
        with patch.object(
            combine_task, "execute", new_callable=AsyncMock, return_value=True
        ):
            await processor.process_item(combine_task)

        # Allow the fire-and-forget task to run
        await asyncio.sleep(0.05)

        mock_mis.populate_match_info_from_apis.assert_called_once_with("/test/group")

    @pytest.mark.asyncio
    async def test_combine_success_triggers_ntfy_request(
        self, temp_storage, mock_config
    ):
        """After CombineTask succeeds, request_match_info_for_directory is called."""
        mock_mis = Mock()
        mock_mis.populate_match_info_from_apis = AsyncMock(return_value=False)
        mock_ntfy = Mock()
        mock_ntfy.request_match_info_for_directory = AsyncMock(return_value=True)

        processor = VideoProcessor(
            temp_storage,
            mock_config,
            Mock(),
            match_info_service=mock_mis,
            ntfy_processor=mock_ntfy,
        )

        combine_task = CombineTask(group_dir="/test/group")
        with patch.object(
            combine_task, "execute", new_callable=AsyncMock, return_value=True
        ):
            await processor.process_item(combine_task)

        await asyncio.sleep(0.05)

        mock_ntfy.request_match_info_for_directory.assert_called_once()
        call_args = mock_ntfy.request_match_info_for_directory.call_args
        assert call_args[0][0] == "/test/group"

    @pytest.mark.asyncio
    async def test_combine_failure_does_not_trigger_transitions(
        self, temp_storage, mock_config
    ):
        """When CombineTask fails, no transitions are triggered."""
        mock_mis = Mock()
        mock_mis.populate_match_info_from_apis = AsyncMock()
        mock_ntfy = Mock()
        mock_ntfy.request_match_info_for_directory = AsyncMock()

        processor = VideoProcessor(
            temp_storage,
            mock_config,
            Mock(),
            match_info_service=mock_mis,
            ntfy_processor=mock_ntfy,
        )

        combine_task = CombineTask(group_dir="/test/group")
        with patch.object(
            combine_task, "execute", new_callable=AsyncMock, return_value=False
        ):
            await processor.process_item(combine_task)

        await asyncio.sleep(0.05)

        mock_mis.populate_match_info_from_apis.assert_not_called()
        mock_ntfy.request_match_info_for_directory.assert_not_called()

    @pytest.mark.asyncio
    async def test_trim_success_does_not_trigger_combine_transitions(
        self, temp_storage, mock_config
    ):
        """TrimTask success should NOT trigger match info transitions."""
        mock_mis = Mock()
        mock_mis.populate_match_info_from_apis = AsyncMock()
        mock_ntfy = Mock()
        mock_ntfy.request_match_info_for_directory = AsyncMock()
        mock_config.autocam.enabled = True

        processor = VideoProcessor(
            temp_storage,
            mock_config,
            Mock(),
            match_info_service=mock_mis,
            ntfy_processor=mock_ntfy,
        )

        trim_task = TrimTask(group_dir="/test/group", start_time="00:05:00")
        with patch.object(
            trim_task, "execute", new_callable=AsyncMock, return_value=True
        ):
            await processor.process_item(trim_task)

        await asyncio.sleep(0.05)

        mock_mis.populate_match_info_from_apis.assert_not_called()
        mock_ntfy.request_match_info_for_directory.assert_not_called()

    @pytest.mark.asyncio
    async def test_trim_success_skips_autocam_when_disabled(
        self, temp_storage, mock_config
    ):
        """When autocam is disabled, trim success should queue upload directly."""
        mock_config.autocam.enabled = False
        mock_config.youtube.enabled = True
        mock_upload = AsyncMock()
        mock_upload.add_work = AsyncMock()

        processor = VideoProcessor(temp_storage, mock_config, mock_upload)

        trim_task = TrimTask(group_dir=temp_storage, start_time="00:05:00")
        with (
            patch.object(
                trim_task, "execute", new_callable=AsyncMock, return_value=True
            ),
            patch("video_grouper.models.DirectoryState") as mock_ds,
        ):
            mock_ds_instance = AsyncMock()
            mock_ds.return_value = mock_ds_instance

            await processor.process_item(trim_task)
            await asyncio.sleep(0.05)

            mock_ds_instance.update_group_status.assert_called_once_with(
                "autocam_complete"
            )
            mock_upload.add_work.assert_called_once()

    @pytest.mark.asyncio
    async def test_trim_success_no_upload_when_autocam_disabled_youtube_disabled(
        self, temp_storage, mock_config
    ):
        """When autocam and youtube are both disabled, no upload is queued."""
        mock_config.autocam.enabled = False
        mock_config.youtube.enabled = False
        mock_upload = AsyncMock()
        mock_upload.add_work = AsyncMock()

        processor = VideoProcessor(temp_storage, mock_config, mock_upload)

        trim_task = TrimTask(group_dir=temp_storage, start_time="00:05:00")
        with (
            patch.object(
                trim_task, "execute", new_callable=AsyncMock, return_value=True
            ),
            patch("video_grouper.models.DirectoryState") as mock_ds,
        ):
            mock_ds_instance = AsyncMock()
            mock_ds.return_value = mock_ds_instance

            await processor.process_item(trim_task)
            await asyncio.sleep(0.05)

            mock_ds_instance.update_group_status.assert_called_once_with(
                "autocam_complete"
            )
            mock_upload.add_work.assert_not_called()

    @pytest.mark.asyncio
    async def test_trim_success_does_not_skip_when_autocam_enabled(
        self, temp_storage, mock_config
    ):
        """When autocam is enabled, trim success should NOT queue upload."""
        mock_config.autocam.enabled = True
        mock_upload = AsyncMock()
        mock_upload.add_work = AsyncMock()

        processor = VideoProcessor(temp_storage, mock_config, mock_upload)

        trim_task = TrimTask(group_dir="/test/group", start_time="00:05:00")
        with patch.object(
            trim_task, "execute", new_callable=AsyncMock, return_value=True
        ):
            await processor.process_item(trim_task)
            await asyncio.sleep(0.05)

            mock_upload.add_work.assert_not_called()

    @pytest.mark.asyncio
    async def test_combine_transition_without_services(self, temp_storage, mock_config):
        """CombineTask success should not error when services are None."""
        processor = VideoProcessor(temp_storage, mock_config, Mock())

        combine_task = CombineTask(group_dir="/test/group")
        with patch.object(
            combine_task, "execute", new_callable=AsyncMock, return_value=True
        ):
            # Should not raise even with no services
            await processor.process_item(combine_task)

        await asyncio.sleep(0.05)
        # No assertions needed - just verify no exception
