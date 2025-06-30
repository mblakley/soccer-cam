"""
Tests for the new service classes in task_processors/services.
"""

import pytest
import os
import tempfile
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime, timedelta

from video_grouper.task_processors.services import (
    TeamSnapService,
    PlayMetricsService,
    NtfyService,
    MatchInfoService,
    CleanupService,
)
from video_grouper.utils.config import TeamSnapConfig, PlayMetricsConfig, NtfyConfig


# Configure pytest for async tests
pytest_plugins = ("pytest_asyncio",)


class TestTeamSnapService:
    """Test TeamSnap service functionality."""

    @pytest.fixture
    def teamsnap_config(self):
        """Create test configuration."""
        return TeamSnapConfig(
            enabled=True, team_id="test_team_id", my_team_name="Test Team"
        )

    @pytest.fixture
    def storage_path(self):
        """Create temporary storage path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir

    def test_init_disabled(self):
        """Test service initialization when disabled."""
        config = TeamSnapConfig(
            enabled=False, team_id="test_team_id", my_team_name="Test Team"
        )
        service = TeamSnapService([config])
        assert not service.enabled
        assert service.teamsnap_apis == []

    @patch("video_grouper.task_processors.services.teamsnap_service.TeamSnapAPI")
    def test_init_enabled(self, mock_api_class, teamsnap_config):
        """Test service initialization when enabled."""
        mock_api = Mock()
        mock_api.enabled = True
        mock_api_class.return_value = mock_api

        service = TeamSnapService([teamsnap_config])
        assert service.enabled
        assert len(service.teamsnap_apis) == 1

    @patch("video_grouper.task_processors.services.teamsnap_service.TeamSnapAPI")
    def test_find_game_for_recording(self, mock_api_class, teamsnap_config):
        """Test finding game for recording."""
        mock_api = Mock()
        mock_api.enabled = True
        mock_api.my_team_name = "Test Team"
        mock_api.find_game_for_recording.return_value = {
            "team_name": "Test Team",
            "opponent_name": "Opponent Team",
            "location_name": "Test Field",
        }
        mock_api_class.return_value = mock_api

        service = TeamSnapService([teamsnap_config])

        start_time = datetime.now()
        end_time = start_time + timedelta(hours=2)

        game = service.find_game_for_recording(start_time, end_time)

        assert game is not None
        assert game["source"] == "TeamSnap"
        assert game["team_name"] == "Test Team"
        mock_api.find_game_for_recording.assert_called_once_with(start_time, end_time)

    @patch("video_grouper.task_processors.services.teamsnap_service.TeamSnapAPI")
    @patch("video_grouper.models.MatchInfo.update_team_info")
    def test_populate_match_info(self, mock_update, mock_api_class, teamsnap_config):
        """Test populating match info."""
        mock_api = Mock()
        mock_api.enabled = True
        mock_api.find_game_for_recording.return_value = {
            "team_name": "Test Team",
            "opponent_name": "Opponent Team",
            "location_name": "Test Field",
        }
        mock_api_class.return_value = mock_api

        service = TeamSnapService([teamsnap_config])

        start_time = datetime.now()
        end_time = start_time + timedelta(hours=2)

        result = service.populate_match_info("/test/dir", start_time, end_time)

        assert result is True
        mock_update.assert_called_once()


class TestPlayMetricsService:
    """Test PlayMetrics service functionality."""

    @pytest.fixture
    def playmetrics_config(self):
        """Create test configuration."""
        return PlayMetricsConfig(
            enabled=True,
            username="test_user",
            password="test_pass",
            team_name="Test Team",
        )

    @pytest.fixture
    def storage_path(self):
        """Create temporary storage path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir

    @patch("video_grouper.task_processors.services.playmetrics_service.PlayMetricsAPI")
    def test_init_enabled(self, mock_api_class, playmetrics_config):
        """Test service initialization when enabled."""
        mock_api = Mock()
        mock_api.enabled = True
        mock_api.login.return_value = True
        mock_api_class.return_value = mock_api

        service = PlayMetricsService([playmetrics_config])
        assert service.enabled
        assert len(service.playmetrics_apis) == 1

    @patch("video_grouper.task_processors.services.playmetrics_service.PlayMetricsAPI")
    def test_find_game_for_recording(self, mock_api_class, playmetrics_config):
        """Test finding game for recording."""
        mock_api = Mock()
        mock_api.enabled = True
        mock_api.login.return_value = True
        mock_api.team_name = "Test Team"
        mock_api.find_game_for_recording.return_value = {
            "title": "Test vs Opponent",
            "opponent": "Opponent Team",
            "location": "Test Field",
            "start_time": datetime.now(),
        }
        mock_api_class.return_value = mock_api

        service = PlayMetricsService([playmetrics_config])

        start_time = datetime.now()
        end_time = start_time + timedelta(hours=2)

        game = service.find_game_for_recording(start_time, end_time)

        assert game is not None
        assert game["source"] == "PlayMetrics"
        assert game["team_name"] == "Test Team"


class TestNtfyService:
    """Test NTFY service functionality."""

    @pytest.fixture
    def ntfy_config(self):
        """Create test configuration."""
        return NtfyConfig(
            enabled=True, server_url="http://localhost:8080", topic="test-topic"
        )

    @pytest.fixture
    def storage_path(self):
        """Create temporary storage path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir

    @patch("video_grouper.task_processors.services.ntfy_service.NtfyAPI")
    def test_init_enabled(self, mock_api_class, ntfy_config, storage_path):
        """Test service initialization when enabled."""
        mock_api = Mock()
        mock_api.enabled = True
        mock_api_class.return_value = mock_api

        service = NtfyService(ntfy_config, storage_path)
        assert service.enabled

    @patch("video_grouper.task_processors.services.ntfy_service.NtfyAPI")
    def test_state_persistence(self, mock_api_class, ntfy_config, storage_path):
        """Test state persistence functionality."""
        mock_api = Mock()
        mock_api.enabled = True
        mock_api_class.return_value = mock_api

        service = NtfyService(ntfy_config, storage_path)

        # Test marking as waiting for input
        service.mark_waiting_for_input("/test/dir", "team_info", {"test": "data"})
        assert service.is_waiting_for_input("/test/dir")

        # Test state file creation
        state_file = os.path.join(storage_path, "ntfy_service_state.json")
        assert os.path.exists(state_file)

        # Test loading state
        service2 = NtfyService(ntfy_config, storage_path)
        assert service2.is_waiting_for_input("/test/dir")

        # Test marking as processed
        service.mark_as_processed("/test/dir")
        assert not service.is_waiting_for_input("/test/dir")
        assert service.has_been_processed("/test/dir")

    @pytest.mark.asyncio
    @patch("video_grouper.task_processors.services.ntfy_service.NtfyAPI")
    async def test_ensure_initialized(self, mock_api_class, ntfy_config, storage_path):
        """Test async initialization."""
        mock_api = Mock()
        mock_api.enabled = True
        mock_api.initialize = AsyncMock()
        mock_api._initialized = False
        mock_api_class.return_value = mock_api

        service = NtfyService(ntfy_config, storage_path)

        # First call should initialize
        result = await service._ensure_initialized()
        assert result is True
        mock_api.initialize.assert_called_once()

        # Manually set to True for testing second call
        mock_api._initialized = True

        # Second call should not initialize again
        result = await service._ensure_initialized()
        assert result is True
        mock_api.initialize.assert_called_once()  # Still only once

    @pytest.mark.asyncio
    @patch("video_grouper.task_processors.services.ntfy_service.NtfyAPI")
    async def test_request_team_info(self, mock_api_class, ntfy_config, storage_path):
        """Test requesting team info."""
        mock_api = AsyncMock()
        mock_api.enabled = True
        mock_api.ask_team_info.return_value = {"team_name": "Test Team"}
        mock_api_class.return_value = mock_api

        service = NtfyService(ntfy_config, storage_path)

        result = await service.request_team_info("/test/dir", "video.mp4")
        assert result is True


class TestMatchInfoService:
    """Test match info service functionality."""

    @pytest.fixture
    def mock_services(self):
        """Create mock services."""
        teamsnap_service = Mock()
        teamsnap_service.enabled = True
        playmetrics_service = Mock()
        playmetrics_service.enabled = True
        ntfy_service = Mock()
        ntfy_service.enabled = True

        return (teamsnap_service, playmetrics_service, ntfy_service)

    def test_init(self, mock_services):
        """Test service initialization."""
        teamsnap, playmetrics, ntfy = mock_services
        service = MatchInfoService(teamsnap, playmetrics, ntfy)
        assert service.teamsnap_service is teamsnap
        assert service.playmetrics_service is playmetrics
        assert service.ntfy_service is ntfy

    def test_select_best_game(self, mock_services):
        """Test selecting best game."""
        teamsnap, playmetrics, ntfy = mock_services
        service = MatchInfoService(teamsnap, playmetrics, ntfy)

        game_ts = {"source": "TeamSnap", "confidence": 100}
        game_pm = {"source": "PlayMetrics", "confidence": 100}

        # Test preference for TeamSnap (as per actual implementation)
        assert service._select_best_game([game_ts, game_pm]) is game_ts
        assert service._select_best_game([game_pm, game_ts]) is game_ts

        # Test single game
        assert service._select_best_game([game_pm]) is game_pm

        # Test empty list
        assert service._select_best_game([]) is None

    @patch("video_grouper.task_processors.services.match_info_service.DirectoryState")
    def test_get_recording_timespan(self, mock_dir_state, mock_services):
        """Test getting recording timespan."""
        teamsnap, playmetrics, ntfy = mock_services
        service = MatchInfoService(teamsnap, playmetrics, ntfy)

        # Create mock files with proper attributes
        mock_file1 = Mock()
        mock_file1.start_time = datetime.now()
        mock_file1.end_time = datetime.now() + timedelta(minutes=30)

        mock_file2 = Mock()
        mock_file2.start_time = datetime.now() + timedelta(minutes=30)
        mock_file2.end_time = datetime.now() + timedelta(minutes=90)

        mock_state = Mock()
        mock_state.get_files.return_value = [mock_file1, mock_file2]
        mock_dir_state.return_value = mock_state

        start_time, end_time = service._get_recording_timespan("/test/dir")

        assert start_time == mock_file1.start_time
        assert end_time == mock_file2.end_time


class TestCleanupService:
    """Test cleanup service functionality."""

    def test_init(self, tmp_path):
        """Test service initialization."""
        service = CleanupService(str(tmp_path))
        assert service.storage_path == str(tmp_path)

    @patch("video_grouper.task_processors.services.cleanup_service.DirectoryState")
    def test_cleanup_dav_files(self, mock_dir_state, tmp_path):
        """Test cleanup of DAV files."""
        service = CleanupService(str(tmp_path))

        group_dir = tmp_path / "test_group"
        group_dir.mkdir()

        # Create some mock DAV files
        (group_dir / "file1.dav").touch()
        (group_dir / "file2.dav").touch()
        (group_dir / "video.mp4").touch()  # This should not be deleted

        dir_state_instance = Mock()
        mock_dir_state.return_value = dir_state_instance

        with patch(
            "video_grouper.task_processors.services.cleanup_service.DirectoryState.save"
        ):
            with patch("os.remove") as mock_remove:
                service.cleanup_dav_files(str(group_dir))

                assert mock_remove.call_count == 2
                mock_remove.assert_any_call(str(group_dir / "file1.dav"))
                mock_remove.assert_any_call(str(group_dir / "file2.dav"))

                dir_state_instance.update_dir_state.assert_called_once_with(
                    {"status": "autocam_complete_dav_files_deleted"}
                )

    def test_cleanup_temporary_files(self, tmp_path):
        """Test cleanup of temporary files."""
        service = CleanupService(str(tmp_path))

        group_dir = tmp_path / "test_group"
        group_dir.mkdir()

        # Create some mock temporary files
        (group_dir / "temp1.tmp").touch()
        (group_dir / "temp2.temp").touch()
        (group_dir / "video.mp4").touch()  # This should not be deleted

        with patch("os.remove") as mock_remove:
            service.cleanup_temporary_files(str(group_dir))

            assert mock_remove.call_count == 2
            mock_remove.assert_any_call(str(group_dir / "temp1.tmp"))
            mock_remove.assert_any_call(str(group_dir / "temp2.temp"))

    @patch("video_grouper.task_processors.services.cleanup_service.DirectoryState")
    def test_should_cleanup_dav_files(self, mock_dir_state, tmp_path):
        """Test logic for deciding to cleanup DAV files."""
        service = CleanupService(str(tmp_path))

        group_dir = tmp_path / "test_group"
        group_dir.mkdir()

        dir_state_instance = Mock()

        # Scenario 1: Status is 'autocam_complete'
        dir_state_instance.status = "autocam_complete"
        with patch(
            "video_grouper.task_processors.services.cleanup_service.DirectoryState",
            return_value=dir_state_instance,
        ):
            assert service.should_cleanup_dav_files(str(group_dir))

        # Scenario 2: Status is not 'autocam_complete'
        dir_state_instance.status = "processing"
        dir_state_instance.reset_mock()
        with patch(
            "video_grouper.task_processors.services.cleanup_service.DirectoryState",
            return_value=dir_state_instance,
        ):
            assert not service.should_cleanup_dav_files(str(group_dir))

        # Scenario 3: Status is already cleaned up
        dir_state_instance.status = "autocam_complete_dav_files_deleted"
        dir_state_instance.reset_mock()
        with patch(
            "video_grouper.task_processors.services.cleanup_service.DirectoryState",
            return_value=dir_state_instance,
        ):
            assert not service.should_cleanup_dav_files(str(group_dir))
