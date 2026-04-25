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
    StorageConfig,
    RecordingConfig,
    ProcessingConfig,
    LoggingConfig,
)
from video_grouper.ball_tracking.config import BallTrackingConfig


@pytest.fixture
def mock_config():
    """Create a mock pydantic Config object."""
    return Config(
        cameras=[
            CameraConfig(
                name="default",
                type="dahua",
                device_ip="127.0.0.1",
                username="admin",
                password="password",
            )
        ],
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

        from unittest.mock import Mock

        mock_download_processor = Mock()
        mock_video_processor = Mock()
        auditor = StateAuditor(
            str(tmp_path),
            mock_config,
            mock_download_processor,
            mock_video_processor,
        )

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
        mock_download_processor = AsyncMock()
        mock_video_processor = AsyncMock()
        auditor = StateAuditor(
            str(tmp_path),
            mock_config,
            mock_download_processor,
            mock_video_processor,
        )

        # Mock processors
        auditor.video_processor = Mock()
        auditor.video_processor.add_work = AsyncMock()

        # Mock match info service
        auditor.match_info_service = AsyncMock()
        auditor.match_info_service.populate_match_info_from_apis = AsyncMock(
            return_value=False
        )
        auditor.match_info_service.ntfy_service = AsyncMock()
        auditor.match_info_service.ntfy_service.enabled = True
        auditor.match_info_service.ntfy_service.process_combined_directory = AsyncMock(
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

        # Verify match info flow was triggered (API lookup + NTFY fallback)
        auditor.match_info_service.populate_match_info_from_apis.assert_called_once()
        auditor.match_info_service.ntfy_service.process_combined_directory.assert_called_once()

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
        mock_download_processor = AsyncMock()
        mock_video_processor = AsyncMock()
        auditor = StateAuditor(
            str(tmp_path),
            mock_config,
            mock_download_processor,
            mock_video_processor,
        )

        # Mock NTFY queue processor - waiting for input
        auditor.ntfy_processor = Mock()
        auditor.ntfy_processor.ntfy_service = Mock()
        auditor.ntfy_processor.ntfy_service.is_waiting_for_input = Mock(
            return_value=True
        )

        # Mock match info service
        auditor.match_info_service = AsyncMock()
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
        mock_download_processor = AsyncMock()
        mock_video_processor = AsyncMock()
        auditor = StateAuditor(
            str(tmp_path),
            mock_config,
            mock_download_processor,
            mock_video_processor,
        )

        # Mock processors
        auditor.video_processor = Mock()
        auditor.video_processor.add_work = AsyncMock()

        # Mock match info service to not be waiting for input
        auditor.match_info_service = AsyncMock()
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

        from unittest.mock import Mock

        mock_download_processor = Mock()
        mock_video_processor = Mock()
        auditor = StateAuditor(
            storage_path=mock_config.storage.path,
            config=mock_config,
            download_processor=mock_download_processor,
            video_processor=mock_video_processor,
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

            from unittest.mock import Mock

            mock_download_processor = Mock()
            mock_video_processor = Mock()
            auditor = StateAuditor(
                str(tmp_path),
                mock_config,
                mock_download_processor,
                mock_video_processor,
            )

            # Verify that ntfy_service attribute exists and is properly initialized
            assert hasattr(auditor, "ntfy_service")
            assert auditor.ntfy_service is not None

            # Verify it's the same instance used by match_info_service
            assert auditor.match_info_service.ntfy_service == auditor.ntfy_service


class TestStateAuditorAutocamToggle:
    """Tests for autocam-disabled behavior in state auditor."""

    @patch("video_grouper.task_processors.services.teamsnap_service.TeamSnapAPI")
    @patch("video_grouper.task_processors.services.playmetrics_service.PlayMetricsAPI")
    @patch("video_grouper.task_processors.services.ntfy_service.NtfyAPI")
    @patch("video_grouper.task_processors.state_auditor.DirectoryState")
    @patch("os.path.exists")
    @pytest.mark.asyncio
    async def test_trimmed_skips_to_upload_when_autocam_disabled(
        self,
        mock_exists,
        mock_dir_state,
        mock_ntfy,
        mock_playmetrics,
        mock_teamsnap,
        tmp_path,
    ):
        """When autocam is disabled, trimmed dirs transition to autocam_complete and queue upload."""
        config = Config(
            cameras=[
                CameraConfig(
                    name="default",
                    type="dahua",
                    device_ip="127.0.0.1",
                    username="admin",
                    password="password",
                )
            ],
            storage=StorageConfig(path=str(tmp_path)),
            recording=RecordingConfig(),
            processing=ProcessingConfig(),
            logging=LoggingConfig(),
            app=AppConfig(storage_path=str(tmp_path)),
            teamsnap=TeamSnapConfig(enabled=True, team_id="1", my_team_name="Team A"),
            teamsnap_teams=[],
            playmetrics=PlayMetricsConfig(
                enabled=True, username="user", password="password", team_name="Team A"
            ),
            playmetrics_teams=[],
            ntfy=NtfyConfig(
                enabled=True, server_url="http://ntfy.sh", topic="soccercam"
            ),
            youtube=YouTubeConfig(enabled=True),
            cloud_sync=CloudSyncConfig(enabled=True),
            ball_tracking=BallTrackingConfig(enabled=False),
        )

        mock_teamsnap.return_value.enabled = True
        mock_playmetrics.return_value.enabled = True
        mock_playmetrics.return_value.login.return_value = True
        mock_ntfy.return_value.enabled = True

        # Mock directory state with trimmed status
        mock_state = Mock()
        mock_state.status = "trimmed"
        mock_state.files = Mock()
        mock_state.files.values.return_value = []
        mock_state.is_ready_for_combining.return_value = False
        mock_state.update_group_status = AsyncMock()
        mock_dir_state.return_value = mock_state

        test_dir = tmp_path / "test_group"
        test_dir.mkdir()
        state_file = test_dir / "state.json"
        state_file.write_text('{"status": "trimmed", "files": {}}')

        mock_exists.return_value = True

        mock_download_processor = AsyncMock()
        mock_video_processor = Mock()
        mock_upload_processor = Mock()
        mock_upload_processor.add_work = AsyncMock()
        mock_video_processor.upload_processor = mock_upload_processor

        auditor = StateAuditor(
            str(tmp_path), config, mock_download_processor, mock_video_processor
        )

        await auditor._audit_directory(str(test_dir))

        mock_state.update_group_status.assert_called_once_with("ball_tracking_complete")
        mock_upload_processor.add_work.assert_called_once()

    @patch("video_grouper.task_processors.services.teamsnap_service.TeamSnapAPI")
    @patch("video_grouper.task_processors.services.playmetrics_service.PlayMetricsAPI")
    @patch("video_grouper.task_processors.services.ntfy_service.NtfyAPI")
    @patch("video_grouper.task_processors.state_auditor.DirectoryState")
    @patch("os.path.exists")
    @pytest.mark.asyncio
    async def test_trimmed_not_skipped_when_autocam_enabled(
        self,
        mock_exists,
        mock_dir_state,
        mock_ntfy,
        mock_playmetrics,
        mock_teamsnap,
        tmp_path,
    ):
        """When autocam is enabled, trimmed dirs are left for autocam discovery."""
        config = Config(
            cameras=[
                CameraConfig(
                    name="default",
                    type="dahua",
                    device_ip="127.0.0.1",
                    username="admin",
                    password="password",
                )
            ],
            storage=StorageConfig(path=str(tmp_path)),
            recording=RecordingConfig(),
            processing=ProcessingConfig(),
            logging=LoggingConfig(),
            app=AppConfig(storage_path=str(tmp_path)),
            teamsnap=TeamSnapConfig(enabled=True, team_id="1", my_team_name="Team A"),
            teamsnap_teams=[],
            playmetrics=PlayMetricsConfig(
                enabled=True, username="user", password="password", team_name="Team A"
            ),
            playmetrics_teams=[],
            ntfy=NtfyConfig(
                enabled=True, server_url="http://ntfy.sh", topic="soccercam"
            ),
            youtube=YouTubeConfig(enabled=True),
            cloud_sync=CloudSyncConfig(enabled=True),
        )

        mock_teamsnap.return_value.enabled = True
        mock_playmetrics.return_value.enabled = True
        mock_playmetrics.return_value.login.return_value = True
        mock_ntfy.return_value.enabled = True

        mock_state = Mock()
        mock_state.status = "trimmed"
        mock_state.files = Mock()
        mock_state.files.values.return_value = []
        mock_state.is_ready_for_combining.return_value = False
        mock_state.update_group_status = AsyncMock()
        mock_dir_state.return_value = mock_state

        test_dir = tmp_path / "test_group"
        test_dir.mkdir()
        state_file = test_dir / "state.json"
        state_file.write_text('{"status": "trimmed", "files": {}}')

        mock_exists.return_value = True

        mock_download_processor = AsyncMock()
        mock_video_processor = Mock()
        mock_upload_processor = Mock()
        mock_upload_processor.add_work = AsyncMock()
        mock_video_processor.upload_processor = mock_upload_processor

        auditor = StateAuditor(
            str(tmp_path), config, mock_download_processor, mock_video_processor
        )

        await auditor._audit_directory(str(test_dir))

        # Should NOT transition or queue upload
        mock_state.update_group_status.assert_not_called()
        mock_upload_processor.add_work.assert_not_called()


class TestStateAuditorStartupOnly:
    """Tests that StateAuditor is startup-only (not continuous polling)."""

    @patch("video_grouper.task_processors.services.teamsnap_service.TeamSnapAPI")
    @patch("video_grouper.task_processors.services.playmetrics_service.PlayMetricsAPI")
    @patch("video_grouper.task_processors.services.ntfy_service.NtfyAPI")
    @pytest.mark.asyncio
    async def test_start_runs_discover_work_once(
        self, mock_ntfy, mock_playmetrics, mock_teamsnap, mock_config, tmp_path
    ):
        """start() should call discover_work() once and return."""
        mock_teamsnap.return_value.enabled = True
        mock_playmetrics.return_value.enabled = True
        mock_playmetrics.return_value.login.return_value = True
        mock_ntfy.return_value.enabled = True

        auditor = StateAuditor(str(tmp_path), mock_config, Mock(), Mock())

        with patch.object(auditor, "discover_work", new_callable=AsyncMock) as mock_dw:
            await auditor.start()
            mock_dw.assert_called_once()

    @patch("video_grouper.task_processors.services.teamsnap_service.TeamSnapAPI")
    @patch("video_grouper.task_processors.services.playmetrics_service.PlayMetricsAPI")
    @patch("video_grouper.task_processors.services.ntfy_service.NtfyAPI")
    @pytest.mark.asyncio
    async def test_start_does_not_create_polling_loop(
        self, mock_ntfy, mock_playmetrics, mock_teamsnap, mock_config, tmp_path
    ):
        """start() should NOT create a persistent _processor_task (no polling loop)."""
        mock_teamsnap.return_value.enabled = True
        mock_playmetrics.return_value.enabled = True
        mock_playmetrics.return_value.login.return_value = True
        mock_ntfy.return_value.enabled = True

        auditor = StateAuditor(str(tmp_path), mock_config, Mock(), Mock())

        with patch.object(auditor, "discover_work", new_callable=AsyncMock):
            await auditor.start()

        # _processor_task should remain None (no background loop created)
        assert auditor._processor_task is None

    @patch("video_grouper.task_processors.services.teamsnap_service.TeamSnapAPI")
    @patch("video_grouper.task_processors.services.playmetrics_service.PlayMetricsAPI")
    @patch("video_grouper.task_processors.services.ntfy_service.NtfyAPI")
    @pytest.mark.asyncio
    async def test_stop_cleans_up_services(
        self, mock_ntfy, mock_playmetrics, mock_teamsnap, mock_config, tmp_path
    ):
        """stop() should shutdown services without errors."""
        mock_teamsnap.return_value.enabled = True
        mock_playmetrics.return_value.enabled = True
        mock_playmetrics.return_value.login.return_value = True
        mock_ntfy.return_value.enabled = True
        mock_ntfy.return_value.shutdown = AsyncMock()

        auditor = StateAuditor(str(tmp_path), mock_config, Mock(), Mock())
        auditor.match_info_service = Mock()
        auditor.match_info_service.shutdown = AsyncMock()

        await auditor.stop()
        auditor.match_info_service.shutdown.assert_called_once()


if __name__ == "__main__":
    pytest.main([__file__])
