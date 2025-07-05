"""
Tests for the enhanced StateAuditor with service integrations.
"""

import pytest
import tempfile
from unittest.mock import Mock, AsyncMock, patch

from video_grouper.task_processors.state_auditor import StateAuditor
from video_grouper.utils.config import (
    Config,
    TeamSnapConfig,
    PlayMetricsConfig,
    NtfyConfig,
    CloudSyncConfig,
    YouTubeConfig,
    AppConfig,
    CameraConfig,
    AutocamConfig,
    StorageConfig,
    RecordingConfig,
    ProcessingConfig,
    LoggingConfig,
)


@pytest.fixture
def mock_config():
    """Create a mock pydantic Config object."""
    return Config(
        camera=CameraConfig(
            type="dahua", device_ip="127.0.0.1", username="admin", password="password"
        ),
        storage=StorageConfig(path=tempfile.mkdtemp()),
        recording=RecordingConfig(),
        processing=ProcessingConfig(),
        logging=LoggingConfig(),
        app=AppConfig(storage_path=tempfile.mkdtemp()),
        teamsnap=TeamSnapConfig(enabled=True, team_id="1", my_team_name="Team A"),
        teamsnap_teams=[],
        playmetrics=PlayMetricsConfig(
            enabled=True, username="user", password="password", team_name="Team A"
        ),
        playmetrics_teams=[],
        ntfy=NtfyConfig(enabled=True, server_url="http://ntfy.sh", topic="soccercam"),
        youtube=YouTubeConfig(enabled=True),
        autocam=AutocamConfig(enabled=True),
        cloud_sync=CloudSyncConfig(enabled=True),
    )


class TestStateAuditorEnhanced:
    """Test enhanced StateAuditor functionality with services."""

    @pytest.fixture
    def test_dir(self, tmp_path):
        """Create test directory with state file."""
        test_dir = tmp_path / "test_group"
        test_dir.mkdir()

        # Create state.json file
        state_file = test_dir / "state.json"
        state_file.write_text('{"status": "combined", "files": {}}')

        # Create combined.mp4
        combined_file = test_dir / "combined.mp4"
        combined_file.write_text("test video content")

        yield test_dir

    @patch("video_grouper.task_processors.services.teamsnap_service.TeamSnapAPI")
    @patch("video_grouper.task_processors.services.playmetrics_service.PlayMetricsAPI")
    @patch("video_grouper.task_processors.services.ntfy_service.NtfyAPI")
    def test_init_with_services(
        self, mock_ntfy, mock_playmetrics, mock_teamsnap, mock_config, tmp_path
    ):
        """Test StateAuditor initialization with all services."""
        # Mock all the APIs
        mock_teamsnap.return_value.enabled = True
        mock_playmetrics.return_value.enabled = True
        mock_playmetrics.return_value.login.return_value = True
        mock_ntfy.return_value.enabled = True

        auditor = StateAuditor(str(tmp_path), mock_config)

        # Check that all services are initialized
        assert auditor.teamsnap_service is not None
        assert auditor.playmetrics_service is not None
        assert auditor.match_info_service is not None
        assert auditor.cleanup_service is not None

    @patch("video_grouper.task_processors.services.teamsnap_service.TeamSnapAPI")
    @patch("video_grouper.task_processors.services.playmetrics_service.PlayMetricsAPI")
    @patch("video_grouper.task_processors.services.ntfy_service.NtfyAPI")
    @patch("video_grouper.task_processors.state_auditor.DirectoryState")
    @patch("os.path.exists")
    @pytest.mark.asyncio
    async def test_audit_combined_directory_with_match_info(
        self,
        mock_exists,
        mock_dir_state,
        mock_ntfy,
        mock_playmetrics,
        mock_teamsnap,
        test_dir,
        mock_config,
        tmp_path,
    ):
        """Test auditing a combined directory with match info processing."""
        # Mock all the APIs
        mock_teamsnap.return_value.enabled = True
        mock_playmetrics.return_value.enabled = True
        mock_playmetrics.return_value.login.return_value = True
        mock_ntfy.return_value.enabled = True
        mock_ntfy.return_value.initialize = AsyncMock()

        # Mock directory state
        mock_state = Mock()
        mock_state.status = "combined"
        # Create a mock that behaves like a dict with empty values
        mock_files = Mock()
        mock_files.values.return_value = []
        mock_state.files = mock_files
        mock_state.is_ready_for_combining.return_value = False
        mock_dir_state.return_value = mock_state

        # Create combined.mp4 file (required for processing)
        combined_file = test_dir / "combined.mp4"
        combined_file.write_text("test video content")

        # Create state.json file (required for processing)
        state_file = test_dir / "state.json"
        state_file.write_text('{"status": "combined"}')

        # Create auditor
        auditor = StateAuditor(str(tmp_path), mock_config)

        # Mock processors
        auditor.video_processor = Mock()
        auditor.video_processor.add_work = AsyncMock()

        # Mock match info service
        auditor.match_info_service.is_waiting_for_user_input = Mock(return_value=False)
        auditor.match_info_service.process_combined_directory = AsyncMock(
            return_value=True
        )

        # Configure the file system properly
        state_path = str(test_dir / "state.json")
        combined_path = str(test_dir / "combined.mp4")
        match_info_path = str(test_dir / "match_info.ini")

        def mock_exists_side_effect(path):
            # Convert to string for comparison
            path_str = str(path)
            if path_str == state_path:
                return True
            elif path_str == combined_path:
                return True
            elif path_str == match_info_path:
                return False  # No match info file exists yet
            elif "ntfy_service_state.json" in path_str:
                return False  # Any NTFY state file doesn't exist
            else:
                return True  # Default to True for other paths (like directory checks)

        mock_exists.side_effect = mock_exists_side_effect

        # Run audit
        await auditor._audit_directory(str(test_dir))

        # Verify match info processing was called
        auditor.match_info_service.process_combined_directory.assert_called_once()

    @patch("video_grouper.task_processors.services.teamsnap_service.TeamSnapAPI")
    @patch("video_grouper.task_processors.services.playmetrics_service.PlayMetricsAPI")
    @patch("video_grouper.task_processors.services.ntfy_service.NtfyAPI")
    @patch("video_grouper.task_processors.state_auditor.DirectoryState")
    @patch("os.path.exists")
    @pytest.mark.asyncio
    async def test_audit_with_user_input_waiting(
        self,
        mock_exists,
        mock_dir_state,
        mock_ntfy,
        mock_playmetrics,
        mock_teamsnap,
        test_dir,
        mock_config,
        tmp_path,
    ):
        """Test auditing when waiting for user input."""
        # Mock all the APIs
        mock_teamsnap.return_value.enabled = True
        mock_playmetrics.return_value.enabled = True
        mock_playmetrics.return_value.login.return_value = True
        mock_ntfy.return_value.enabled = True
        mock_ntfy.return_value.initialize = AsyncMock()

        # Mock directory state
        mock_state = Mock()
        mock_state.status = "combined"
        # Create a mock that behaves like a dict with empty values
        mock_files = Mock()
        mock_files.values.return_value = []
        mock_state.files = mock_files
        mock_state.is_ready_for_combining.return_value = False
        mock_dir_state.return_value = mock_state

        # Create auditor
        auditor = StateAuditor(str(tmp_path), mock_config)

        # Mock NTFY queue processor - waiting for input
        auditor.ntfy_queue_processor = Mock()
        auditor.ntfy_queue_processor.ntfy_service = Mock()
        auditor.ntfy_queue_processor.ntfy_service.is_waiting_for_input = Mock(
            return_value=True
        )

        # Mock match info service
        auditor.match_info_service.process_combined_directory = AsyncMock()

        # Mock file system
        state_path = str(test_dir / "state.json")
        combined_path = str(test_dir / "combined.mp4")

        def mock_exists_side_effect(path):
            path_str = str(path)
            if path_str == state_path:
                return True
            elif path_str == combined_path:
                return True
            elif "ntfy_service_state.json" in path_str:
                return False
            else:
                return True

        mock_exists.side_effect = mock_exists_side_effect

        # Run audit
        await auditor._audit_directory(str(test_dir))

        # Verify match info processing was NOT called
        auditor.match_info_service.process_combined_directory.assert_not_called()

    @patch("video_grouper.task_processors.services.teamsnap_service.TeamSnapAPI")
    @patch("video_grouper.task_processors.services.playmetrics_service.PlayMetricsAPI")
    @patch("video_grouper.task_processors.services.ntfy_service.NtfyAPI")
    @patch("video_grouper.task_processors.state_auditor.DirectoryState")
    @patch("os.path.exists")
    @pytest.mark.asyncio
    async def test_cleanup_and_sync_handling(
        self,
        mock_exists,
        mock_dir_state,
        mock_ntfy,
        mock_playmetrics,
        mock_teamsnap,
        test_dir,
        mock_config,
        tmp_path,
    ):
        """Test cleanup and sync handling."""
        # Mock all the APIs
        mock_teamsnap.return_value.enabled = True
        mock_playmetrics.return_value.enabled = True
        mock_playmetrics.return_value.login.return_value = True
        mock_ntfy.return_value.enabled = True
        mock_ntfy.return_value.initialize = AsyncMock()

        # Mock directory state
        mock_state = Mock()
        mock_state.status = "autocam_complete"
        # Create a mock that behaves like a dict with empty values
        mock_files = Mock()
        mock_files.values.return_value = []
        mock_state.files = mock_files
        mock_state.is_ready_for_combining.return_value = False
        mock_dir_state.return_value = mock_state

        # Create auditor
        auditor = StateAuditor(str(tmp_path), mock_config)

        # Mock services
        auditor.cleanup_service.should_cleanup_dav_files = Mock(return_value=True)
        auditor.cleanup_service.cleanup_dav_files = Mock()

        def mock_exists_side_effect(path):
            return True

        mock_exists.side_effect = mock_exists_side_effect

        # Run audit
        await auditor._audit_directory(str(test_dir))

        # Verify cleanup was called
        auditor.cleanup_service.should_cleanup_dav_files.assert_called_once_with(
            str(test_dir)
        )
        auditor.cleanup_service.cleanup_dav_files.assert_called_once_with(str(test_dir))

    @patch("video_grouper.task_processors.services.teamsnap_service.TeamSnapAPI")
    @patch("video_grouper.task_processors.services.playmetrics_service.PlayMetricsAPI")
    @patch("video_grouper.task_processors.services.ntfy_service.NtfyAPI")
    @patch("video_grouper.task_processors.state_auditor.DirectoryState")
    @patch("os.path.exists")
    @pytest.mark.asyncio
    async def test_youtube_upload_queuing(
        self,
        mock_exists,
        mock_dir_state,
        mock_ntfy,
        mock_playmetrics,
        mock_teamsnap,
        test_dir,
        mock_config,
        tmp_path,
    ):
        """Test YouTube upload queuing."""
        # Mock all the APIs
        mock_teamsnap.return_value.enabled = True
        mock_playmetrics.return_value.enabled = True
        mock_playmetrics.return_value.login.return_value = True
        mock_ntfy.return_value.enabled = True
        mock_ntfy.return_value.initialize = AsyncMock()

        # Mock directory state - use 'autocam_complete' status to trigger upload
        mock_state = Mock()
        mock_state.status = "autocam_complete"
        # Create a mock that behaves like a dict with empty values
        mock_files = Mock()
        mock_files.values.return_value = []
        mock_state.files = mock_files
        mock_dir_state.return_value = mock_state

        # Create auditor
        auditor = StateAuditor(str(tmp_path), mock_config)

        # Mock processors
        auditor.upload_processor = Mock()
        auditor.upload_processor.add_work = AsyncMock()

        # Mock file system
        state_path = str(test_dir / "state.json")

        def mock_exists_side_effect(path):
            path_str = str(path)
            if path_str == state_path:
                return True
            return False

        mock_exists.side_effect = mock_exists_side_effect

        # Run audit
        await auditor._audit_directory(str(test_dir))

        # Verify YouTube upload was queued
        auditor.upload_processor.add_work.assert_called_once()

    @patch("video_grouper.task_processors.services.teamsnap_service.TeamSnapAPI")
    @patch("video_grouper.task_processors.services.playmetrics_service.PlayMetricsAPI")
    @patch("video_grouper.task_processors.services.ntfy_service.NtfyAPI")
    @patch("video_grouper.task_processors.state_auditor.TrimTask")
    @patch("video_grouper.task_processors.state_auditor.MatchInfo")
    @patch("video_grouper.task_processors.state_auditor.DirectoryState")
    @patch("os.path.exists")
    @pytest.mark.asyncio
    async def test_populated_match_info_triggers_trim(
        self,
        mock_exists,
        mock_dir_state,
        mock_match_info,
        mock_trim_task,
        mock_ntfy,
        mock_playmetrics,
        mock_teamsnap,
        test_dir,
        mock_config,
        tmp_path,
    ):
        """Test that a populated match info file triggers trimming."""
        # Mock all the APIs
        mock_teamsnap.return_value.enabled = True
        mock_playmetrics.return_value.enabled = True
        mock_playmetrics.return_value.login.return_value = True
        mock_ntfy.return_value.enabled = True
        mock_ntfy.return_value.initialize = AsyncMock()

        # Mock directory state
        mock_state = Mock()
        mock_state.status = "combined"
        # Create a mock that behaves like a dict with empty values
        mock_files = Mock()
        mock_files.values.return_value = []
        mock_state.files = mock_files
        # Add get_files method for the MatchInfoService
        mock_state.get_files.return_value = []
        mock_dir_state.return_value = mock_state

        # Mock MatchInfo - patch it in the state_auditor module
        mock_match_instance = Mock()
        mock_match_instance.is_populated.return_value = True
        mock_match_info.from_file.return_value = mock_match_instance

        # Mock TrimTask.from_match_info
        mock_trim_task_instance = Mock()
        mock_trim_task.from_match_info.return_value = mock_trim_task_instance

        # Create auditor
        auditor = StateAuditor(str(tmp_path), mock_config)

        # Mock processors
        auditor.video_processor = Mock()
        auditor.video_processor.add_work = AsyncMock()

        # Mock match info service to not be waiting for input
        auditor.match_info_service.is_waiting_for_user_input = Mock(return_value=False)

        # Mock file system
        state_path = str(test_dir / "state.json")
        combined_path = str(test_dir / "combined.mp4")
        match_info_path = str(test_dir / "match_info.ini")

        def mock_exists_side_effect(path):
            path_str = str(path)
            if path_str == state_path:
                return True
            elif path_str == combined_path:
                return True
            elif path_str == match_info_path:
                return True
            return False

        mock_exists.side_effect = mock_exists_side_effect

        # Run audit
        await auditor._audit_directory(str(test_dir))

        # Verify trim task was added
        auditor.video_processor.add_work.assert_called_once_with(
            mock_trim_task_instance
        )

    @pytest.mark.asyncio
    @patch("video_grouper.task_processors.services.ntfy_service.NtfyAPI")
    @patch("video_grouper.task_processors.services.teamsnap_service.TeamSnapAPI")
    @patch("video_grouper.task_processors.services.playmetrics_service.PlayMetricsAPI")
    async def test_service_shutdown(
        self, mock_playmetrics_api, mock_teamsnap_api, mock_ntfy_api, mock_config
    ):
        """Test service shutdown."""
        # Mock all the APIs
        mock_ntfy_api.return_value.enabled = True
        mock_ntfy_api.return_value.initialize = AsyncMock()
        mock_ntfy_api.return_value.shutdown = AsyncMock()

        mock_teamsnap_api.return_value.enabled = True
        mock_playmetrics_api.return_value.enabled = True
        mock_playmetrics_api.return_value.login.return_value = True

        auditor = StateAuditor(
            storage_path=mock_config.storage.path, config=mock_config
        )

        # Test that shutdown doesn't raise an exception
        await auditor.stop()

    def test_ntfy_service_attribute_exists(self, mock_config, tmp_path):
        """Test that StateAuditor has ntfy_service attribute."""
        with (
            patch(
                "video_grouper.task_processors.services.teamsnap_service.TeamSnapAPI"
            ),
            patch(
                "video_grouper.task_processors.services.playmetrics_service.PlayMetricsAPI"
            ),
            patch(
                "video_grouper.task_processors.services.ntfy_service.NtfyAPI"
            ) as mock_ntfy,
        ):
            mock_ntfy.return_value.enabled = True

            auditor = StateAuditor(str(tmp_path), mock_config)

            # Verify that ntfy_service attribute exists and is properly initialized
            assert hasattr(auditor, "ntfy_service")
            assert auditor.ntfy_service is not None

            # Verify it's the same instance used by match_info_service
            assert auditor.match_info_service.ntfy_service == auditor.ntfy_service

    @patch("video_grouper.task_processors.services.teamsnap_service.TeamSnapAPI")
    @patch("video_grouper.task_processors.services.playmetrics_service.PlayMetricsAPI")
    @patch("video_grouper.task_processors.services.ntfy_service.NtfyAPI")
    @patch("video_grouper.task_processors.state_auditor.DirectoryState")
    @patch("video_grouper.task_processors.state_auditor.YoutubeUploadTask")
    @patch("os.path.exists")
    @pytest.mark.asyncio
    async def test_youtube_upload_task_creation_with_dependencies(
        self,
        mock_exists,
        mock_youtube_task,
        mock_dir_state,
        mock_ntfy,
        mock_playmetrics,
        mock_teamsnap,
        test_dir,
        mock_config,
        tmp_path,
    ):
        """Test that YoutubeUploadTask is created with proper dependencies."""
        # Mock all the APIs
        mock_teamsnap.return_value.enabled = True
        mock_playmetrics.return_value.enabled = True
        mock_playmetrics.return_value.login.return_value = True
        mock_ntfy.return_value.enabled = True
        mock_ntfy.return_value.initialize = AsyncMock()

        # Mock directory state - use 'autocam_complete' status to trigger upload
        mock_state = Mock()
        mock_state.status = "autocam_complete"
        mock_files = Mock()
        mock_files.values.return_value = []
        mock_state.files = mock_files
        mock_dir_state.return_value = mock_state

        # Mock YoutubeUploadTask creation
        mock_task_instance = Mock()
        mock_youtube_task.return_value = mock_task_instance

        # Create auditor
        auditor = StateAuditor(str(tmp_path), mock_config)

        # Mock processors
        auditor.upload_processor = Mock()
        auditor.upload_processor.add_work = AsyncMock()

        # Mock file system
        state_path = str(test_dir / "state.json")

        def mock_exists_side_effect(path):
            path_str = str(path)
            if path_str == state_path:
                return True
            return False

        mock_exists.side_effect = mock_exists_side_effect

        # Run audit
        await auditor._audit_directory(str(test_dir))

        # Verify YoutubeUploadTask was created with the correct parameters
        mock_youtube_task.assert_called_once_with(
            str(test_dir), auditor.config.youtube, auditor.ntfy_service
        )

        # Verify the task was added to the upload processor
        auditor.upload_processor.add_work.assert_called_once_with(mock_task_instance)


if __name__ == "__main__":
    pytest.main([__file__])
