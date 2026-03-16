"""Tests for the UploadProcessor."""

import os
import tempfile
from unittest.mock import Mock, AsyncMock, patch
import pytest

from video_grouper.task_processors.upload_processor import UploadProcessor
from video_grouper.task_processors.tasks.upload import YoutubeUploadTask


@pytest.fixture
def temp_storage():
    """Create a temporary storage directory for testing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def mock_config():
    """Create a mock configuration object."""
    config = Mock()
    config.youtube = Mock()
    config.youtube.enabled = True
    config.youtube.privacy_status = "unlisted"
    config.youtube.use_mock = False
    config.ntfy = Mock()
    config.ntfy.enabled = False
    return config


def create_mock_youtube_upload_task(group_dir: str) -> YoutubeUploadTask:
    """Create a mock YoutubeUploadTask with required dependencies."""
    return YoutubeUploadTask(group_dir=group_dir)


class TestUploadProcessor:
    """Test the UploadProcessor."""

    @pytest.mark.asyncio
    async def test_upload_processor_initialization(self, temp_storage, mock_config):
        """Test UploadProcessor initialization."""
        processor = UploadProcessor(temp_storage, mock_config)

        assert processor.storage_path == temp_storage
        assert processor.config == mock_config
        assert processor.ntfy_service is None

    @pytest.mark.asyncio
    async def test_upload_processor_initialization_with_ntfy_service(
        self, temp_storage, mock_config
    ):
        """Test UploadProcessor initialization with ntfy_service."""
        mock_ntfy = Mock()
        processor = UploadProcessor(temp_storage, mock_config, ntfy_service=mock_ntfy)

        assert processor.ntfy_service is mock_ntfy

    @pytest.mark.asyncio
    async def test_upload_task_auth_failure_no_token(self, temp_storage, mock_config):
        """Test upload task when token file doesn't exist (auth check fails)."""
        group_dir = os.path.join(temp_storage, "2023.01.01-10.00.00")

        processor = UploadProcessor(temp_storage, mock_config)

        upload_task = create_mock_youtube_upload_task(group_dir)
        # ensure_valid_token will fail because no credentials/token files exist
        # process_item catches the RuntimeError internally
        await processor.process_item(upload_task)

        # Should complete without raising (error is caught and logged)

    @pytest.mark.asyncio
    async def test_upload_task_processing_success(self, temp_storage, mock_config):
        """Test successful upload task processing."""
        group_dir = "/test/group"

        processor = UploadProcessor(temp_storage, mock_config)

        with (
            patch(
                "video_grouper.utils.youtube_upload.ensure_valid_token",
                return_value=(True, "Token is valid"),
            ),
            patch.object(
                YoutubeUploadTask, "execute", new_callable=AsyncMock
            ) as mock_execute,
        ):
            mock_execute.return_value = True

            upload_task = create_mock_youtube_upload_task(group_dir)
            await processor.process_item(upload_task)

            mock_execute.assert_called_once_with(
                youtube_config=mock_config.youtube,
                ntfy_service=None,
                storage_path=temp_storage,
            )

    @pytest.mark.asyncio
    async def test_upload_task_processing_failure(self, temp_storage, mock_config):
        """Test failed upload task processing."""
        group_dir = "/test/group"

        processor = UploadProcessor(temp_storage, mock_config)

        with (
            patch(
                "video_grouper.utils.youtube_upload.ensure_valid_token",
                return_value=(True, "Token is valid"),
            ),
            patch.object(
                YoutubeUploadTask, "execute", new_callable=AsyncMock
            ) as mock_execute,
        ):
            mock_execute.return_value = False

            upload_task = create_mock_youtube_upload_task(group_dir)
            await processor.process_item(upload_task)

            mock_execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_upload_task_processing_exception(self, temp_storage, mock_config):
        """Test upload task processing with exception."""
        group_dir = "/test/group"

        processor = UploadProcessor(temp_storage, mock_config)

        with (
            patch(
                "video_grouper.utils.youtube_upload.ensure_valid_token",
                return_value=(True, "Token is valid"),
            ),
            patch.object(
                YoutubeUploadTask, "execute", new_callable=AsyncMock
            ) as mock_execute,
        ):
            mock_execute.side_effect = Exception("Upload error")

            upload_task = create_mock_youtube_upload_task(group_dir)
            await processor.process_item(upload_task)

            # Should complete without raising the exception

    @pytest.mark.asyncio
    async def test_upload_task_no_playlist_config(self, temp_storage):
        """Test upload task when no playlist configuration is available."""
        config = Mock()
        config.youtube = Mock()
        config.youtube.enabled = True
        config.youtube.use_mock = False
        config.ntfy = Mock()
        config.ntfy.enabled = False

        group_dir = "/test/group"

        processor = UploadProcessor(temp_storage, config)

        with (
            patch(
                "video_grouper.utils.youtube_upload.ensure_valid_token",
                return_value=(True, "Token is valid"),
            ),
            patch.object(
                YoutubeUploadTask, "execute", new_callable=AsyncMock
            ) as mock_execute,
        ):
            mock_execute.return_value = True

            upload_task = create_mock_youtube_upload_task(group_dir)
            await processor.process_item(upload_task)

            mock_execute.assert_called_once()

    def test_get_item_key(self, temp_storage, mock_config):
        """Test getting unique key for BaseUploadTask."""
        processor = UploadProcessor(temp_storage, mock_config)

        # Test YouTube upload task
        youtube_task = create_mock_youtube_upload_task("/test/path/group")
        key = processor.get_item_key(youtube_task)
        assert key == "youtube_upload:/test/path/group"

    def test_get_state_file_name(self, temp_storage, mock_config):
        """Test getting state file name."""
        processor = UploadProcessor(temp_storage, mock_config)

        state_file_name = processor.get_state_file_name()

        assert state_file_name == "upload_queue_state.json"


class TestUploadProcessorAuthCheck:
    """Tests for YouTube token validation in UploadProcessor."""

    @pytest.mark.asyncio
    async def test_auth_failure_sends_ntfy_notification(
        self, temp_storage, mock_config
    ):
        """When token validation fails and ntfy_service is set, send notification."""
        mock_ntfy = Mock()
        mock_ntfy.send_notification = AsyncMock(return_value=True)

        processor = UploadProcessor(temp_storage, mock_config, ntfy_service=mock_ntfy)

        with patch(
            "video_grouper.utils.youtube_upload.ensure_valid_token",
            return_value=(False, "Token expired. Please re-authenticate via tray."),
        ):
            upload_task = create_mock_youtube_upload_task("/test/group")
            await processor.process_item(upload_task)

            # NTFY notification should have been sent
            mock_ntfy.send_notification.assert_called_once_with(
                title="YouTube Authentication Required",
                message="Token expired. Please re-authenticate via tray.",
            )

    @pytest.mark.asyncio
    async def test_auth_failure_no_ntfy_service(self, temp_storage, mock_config):
        """When token validation fails and no ntfy_service, just log error."""
        processor = UploadProcessor(temp_storage, mock_config)
        assert processor.ntfy_service is None

        with patch(
            "video_grouper.utils.youtube_upload.ensure_valid_token",
            return_value=(False, "No token found."),
        ):
            upload_task = create_mock_youtube_upload_task("/test/group")
            # Should not raise even without ntfy_service
            await processor.process_item(upload_task)

    @pytest.mark.asyncio
    async def test_auth_failure_ntfy_send_fails(self, temp_storage, mock_config):
        """When NTFY notification itself fails, process_item still handles it."""
        mock_ntfy = Mock()
        mock_ntfy.send_notification = AsyncMock(
            side_effect=Exception("NTFY send failed")
        )

        processor = UploadProcessor(temp_storage, mock_config, ntfy_service=mock_ntfy)

        with patch(
            "video_grouper.utils.youtube_upload.ensure_valid_token",
            return_value=(False, "Token expired."),
        ):
            upload_task = create_mock_youtube_upload_task("/test/group")
            # Should not raise even when NTFY fails
            await processor.process_item(upload_task)

    @pytest.mark.asyncio
    async def test_auth_success_proceeds_to_execute(self, temp_storage, mock_config):
        """When token is valid, task execute is called with correct args."""
        mock_ntfy = Mock()
        processor = UploadProcessor(temp_storage, mock_config, ntfy_service=mock_ntfy)

        with (
            patch(
                "video_grouper.utils.youtube_upload.ensure_valid_token",
                return_value=(True, "Token is valid"),
            ),
            patch.object(
                YoutubeUploadTask, "execute", new_callable=AsyncMock
            ) as mock_execute,
        ):
            mock_execute.return_value = True

            upload_task = create_mock_youtube_upload_task("/test/group")
            await processor.process_item(upload_task)

            mock_execute.assert_called_once_with(
                youtube_config=mock_config.youtube,
                ntfy_service=mock_ntfy,
                storage_path=temp_storage,
            )

    @pytest.mark.asyncio
    async def test_auth_failure_skips_execute(self, temp_storage, mock_config):
        """When token validation fails, execute is NOT called."""
        processor = UploadProcessor(temp_storage, mock_config)

        with (
            patch(
                "video_grouper.utils.youtube_upload.ensure_valid_token",
                return_value=(False, "Token expired."),
            ),
            patch.object(
                YoutubeUploadTask, "execute", new_callable=AsyncMock
            ) as mock_execute,
        ):
            upload_task = create_mock_youtube_upload_task("/test/group")
            await processor.process_item(upload_task)

            mock_execute.assert_not_called()
