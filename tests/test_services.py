"""
Tests for the new service classes in task_processors/services.
"""

import pytest
import asyncio
import os
import json
import tempfile
import configparser
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from datetime import datetime, timedelta

from video_grouper.task_processors.services import (
    TeamSnapService, PlayMetricsService, NtfyService, 
    MatchInfoService, CleanupService, CloudSyncService
)
from video_grouper.models import MatchInfo


# Configure pytest for async tests
pytest_plugins = ('pytest_asyncio',)


class TestTeamSnapService:
    """Test TeamSnap service functionality."""
    
    @pytest.fixture
    def config(self):
        """Create test configuration."""
        config = configparser.ConfigParser()
        config.add_section('TEAMSNAP')
        config['TEAMSNAP']['enabled'] = 'true'
        config['TEAMSNAP']['team_id'] = 'test_team_id'
        config['TEAMSNAP']['team_name'] = 'Test Team'
        return config
    
    @pytest.fixture
    def storage_path(self):
        """Create temporary storage path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir
    
    def test_init_disabled(self, storage_path):
        """Test service initialization when disabled."""
        config = configparser.ConfigParser()
        service = TeamSnapService(config, storage_path)
        assert not service.enabled
        assert service.teamsnap_apis == []
    
    @patch('video_grouper.task_processors.services.teamsnap_service.TeamSnapAPI')
    def test_init_enabled(self, mock_api_class, config, storage_path):
        """Test service initialization when enabled."""
        mock_api = Mock()
        mock_api.enabled = True
        mock_api_class.return_value = mock_api
        
        service = TeamSnapService(config, storage_path)
        assert service.enabled
        assert len(service.teamsnap_apis) == 1
    
    @patch('video_grouper.task_processors.services.teamsnap_service.TeamSnapAPI')
    def test_find_game_for_recording(self, mock_api_class, config, storage_path):
        """Test finding game for recording."""
        mock_api = Mock()
        mock_api.enabled = True
        mock_api.my_team_name = 'Test Team'
        mock_api.find_game_for_recording.return_value = {
            'team_name': 'Test Team',
            'opponent_name': 'Opponent Team',
            'location_name': 'Test Field'
        }
        mock_api_class.return_value = mock_api
        
        service = TeamSnapService(config, storage_path)
        
        start_time = datetime.now()
        end_time = start_time + timedelta(hours=2)
        
        game = service.find_game_for_recording(start_time, end_time)
        
        assert game is not None
        assert game['source'] == 'TeamSnap'
        assert game['team_name'] == 'Test Team'
        mock_api.find_game_for_recording.assert_called_once_with(start_time, end_time)
    
    @patch('video_grouper.task_processors.services.teamsnap_service.TeamSnapAPI')
    @patch('video_grouper.models.MatchInfo.update_team_info')
    def test_populate_match_info(self, mock_update, mock_api_class, config, storage_path):
        """Test populating match info."""
        mock_api = Mock()
        mock_api.enabled = True
        mock_api.find_game_for_recording.return_value = {
            'team_name': 'Test Team',
            'opponent_name': 'Opponent Team',
            'location_name': 'Test Field'
        }
        mock_api_class.return_value = mock_api
        
        service = TeamSnapService(config, storage_path)
        
        start_time = datetime.now()
        end_time = start_time + timedelta(hours=2)
        
        result = service.populate_match_info('/test/dir', start_time, end_time)
        
        assert result is True
        mock_update.assert_called_once()


class TestPlayMetricsService:
    """Test PlayMetrics service functionality."""
    
    @pytest.fixture
    def config(self):
        """Create test configuration."""
        config = configparser.ConfigParser()
        config.add_section('PLAYMETRICS')
        config['PLAYMETRICS']['enabled'] = 'true'
        config['PLAYMETRICS']['username'] = 'test_user'
        config['PLAYMETRICS']['password'] = 'test_pass'
        return config
    
    @pytest.fixture
    def storage_path(self):
        """Create temporary storage path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir
    
    @patch('video_grouper.task_processors.services.playmetrics_service.PlayMetricsAPI')
    def test_init_enabled(self, mock_api_class, config, storage_path):
        """Test service initialization when enabled."""
        mock_api = Mock()
        mock_api.enabled = True
        mock_api.login.return_value = True
        mock_api_class.return_value = mock_api
        
        service = PlayMetricsService(config, storage_path)
        assert service.enabled
        assert len(service.playmetrics_apis) == 1
    
    @patch('video_grouper.task_processors.services.playmetrics_service.PlayMetricsAPI')
    def test_find_game_for_recording(self, mock_api_class, config, storage_path):
        """Test finding game for recording."""
        mock_api = Mock()
        mock_api.enabled = True
        mock_api.login.return_value = True
        mock_api.team_name = 'Test Team'
        mock_api.find_game_for_recording.return_value = {
            'title': 'Test vs Opponent',
            'opponent': 'Opponent Team',
            'location': 'Test Field',
            'start_time': datetime.now()
        }
        mock_api_class.return_value = mock_api
        
        service = PlayMetricsService(config, storage_path)
        
        start_time = datetime.now()
        end_time = start_time + timedelta(hours=2)
        
        game = service.find_game_for_recording(start_time, end_time)
        
        assert game is not None
        assert game['source'] == 'PlayMetrics'
        assert game['team_name'] == 'Test Team'


class TestNtfyService:
    """Test NTFY service functionality."""
    
    @pytest.fixture
    def config(self):
        """Create test configuration."""
        config = configparser.ConfigParser()
        config.add_section('NTFY')
        config['NTFY']['enabled'] = 'true'
        config['NTFY']['topic'] = 'test_topic'
        return config
    
    @pytest.fixture
    def storage_path(self):
        """Create temporary storage path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir
    
    @patch('video_grouper.task_processors.services.ntfy_service.NtfyAPI')
    def test_init_enabled(self, mock_api_class, config, storage_path):
        """Test service initialization when enabled."""
        mock_api = Mock()
        mock_api.enabled = True
        mock_api_class.return_value = mock_api
        
        service = NtfyService(config, storage_path)
        assert service.enabled
        assert service.ntfy_api is not None
    
    @patch('video_grouper.task_processors.services.ntfy_service.NtfyAPI')
    def test_state_persistence(self, mock_api_class, config, storage_path):
        """Test state persistence functionality."""
        mock_api = Mock()
        mock_api.enabled = True
        mock_api_class.return_value = mock_api
        
        service = NtfyService(config, storage_path)
        
        # Test marking as waiting for input
        service.mark_waiting_for_input('/test/dir', 'team_info', {'test': 'data'})
        assert service.is_waiting_for_input('/test/dir')
        
        # Test state file creation
        state_file = os.path.join(storage_path, "ntfy_service_state.json")
        assert os.path.exists(state_file)
        
        # Test loading state
        service2 = NtfyService(config, storage_path)
        assert service2.is_waiting_for_input('/test/dir')
        
        # Test marking as processed
        service.mark_as_processed('/test/dir')
        assert not service.is_waiting_for_input('/test/dir')
        assert service.has_been_processed('/test/dir')
    
    @patch('video_grouper.task_processors.services.ntfy_service.NtfyAPI')
    @pytest.mark.asyncio
    async def test_ensure_initialized(self, mock_api_class, config, storage_path):
        """Test async initialization."""
        mock_api = Mock()
        mock_api.enabled = True
        mock_api.initialize = AsyncMock()
        # Ensure _initialized is not set initially
        if hasattr(mock_api, '_initialized'):
            delattr(mock_api, '_initialized')
        mock_api_class.return_value = mock_api
        
        service = NtfyService(config, storage_path)
        # Ensure the service is enabled (it gets this from the API)
        assert service.enabled is True
        assert service.ntfy_api is mock_api
        
        # First call should initialize
        result = await service._ensure_initialized()
        assert result is True
        mock_api.initialize.assert_called_once()
        
        # Verify _initialized was set
        assert hasattr(mock_api, '_initialized')
        assert mock_api._initialized is True
        
        # Second call should not initialize again (because _initialized is now set)
        result = await service._ensure_initialized()
        assert result is True
        mock_api.initialize.assert_called_once()  # Still only once
    
    @patch('video_grouper.task_processors.services.ntfy_service.NtfyAPI')
    @pytest.mark.asyncio
    async def test_request_team_info(self, mock_api_class, config, storage_path):
        """Test requesting team info."""
        mock_api = Mock()
        mock_api.enabled = True
        mock_api.initialize = AsyncMock()
        mock_api.ask_team_info = AsyncMock()
        mock_api_class.return_value = mock_api
        
        service = NtfyService(config, storage_path)
        
        result = await service.request_team_info('/test/dir', '/test/video.mp4')
        
        assert result is True
        mock_api.ask_team_info.assert_called_once()
        assert service.is_waiting_for_input('/test/dir')


class TestMatchInfoService:
    """Test match info service functionality."""
    
    @pytest.fixture
    def mock_services(self):
        """Create mock services."""
        teamsnap = Mock()
        teamsnap.enabled = True
        
        playmetrics = Mock()
        playmetrics.enabled = True
        
        ntfy = Mock()
        ntfy.enabled = True
        
        return teamsnap, playmetrics, ntfy
    
    def test_init(self, mock_services):
        """Test service initialization."""
        teamsnap, playmetrics, ntfy = mock_services
        service = MatchInfoService(teamsnap, playmetrics, ntfy)
        
        assert service.teamsnap_service == teamsnap
        assert service.playmetrics_service == playmetrics
        assert service.ntfy_service == ntfy
    
    def test_select_best_game(self, mock_services):
        """Test game selection logic."""
        teamsnap, playmetrics, ntfy = mock_services
        service = MatchInfoService(teamsnap, playmetrics, ntfy)
        
        # Test empty list
        result = service._select_best_game([])
        assert result is None
        
        # Test single game
        games = [{'source': 'PlayMetrics', 'title': 'Test Game'}]
        result = service._select_best_game(games)
        assert result == games[0]
        
        # Test TeamSnap preference
        games = [
            {'source': 'PlayMetrics', 'title': 'PM Game'},
            {'source': 'TeamSnap', 'title': 'TS Game'}
        ]
        result = service._select_best_game(games)
        assert result['source'] == 'TeamSnap'
    
    @patch('video_grouper.task_processors.services.match_info_service.DirectoryState')
    def test_get_recording_timespan(self, mock_dir_state, mock_services):
        """Test getting recording timespan."""
        teamsnap, playmetrics, ntfy = mock_services
        service = MatchInfoService(teamsnap, playmetrics, ntfy)
        
        # Mock directory state
        mock_file = Mock()
        mock_file.start_time = datetime.now()
        mock_file.end_time = datetime.now() + timedelta(hours=2)
        
        mock_state = Mock()
        mock_state.get_files.return_value = [mock_file]
        mock_dir_state.return_value = mock_state
        
        result = service._get_recording_timespan('/test/dir')
        
        assert result is not None
        assert len(result) == 2
        assert result[0] == mock_file.start_time
        assert result[1] == mock_file.end_time


class TestCleanupService:
    """Test cleanup service functionality."""
    
    def test_init(self, tmp_path):
        """Test service initialization."""
        service = CleanupService(str(tmp_path))
        assert service.storage_path == str(tmp_path)
    
    @patch('video_grouper.task_processors.services.cleanup_service.DirectoryState')
    def test_cleanup_dav_files(self, mock_dir_state, tmp_path):
        """Test DAV file cleanup."""
        # Create test directory with files
        test_dir = tmp_path / 'test_group'
        test_dir.mkdir()
        
        # Create test files
        dav_file = test_dir / 'test.dav'
        dav_file.write_text('test dav content')
        
        video_file = test_dir / 'combined.mp4'
        video_file.write_text('test video content')
        
        service = CleanupService(str(tmp_path))
        
        # Mock directory state
        mock_state = Mock()
        mock_dir_state.return_value = mock_state
        
        # Test cleanup
        result = service.cleanup_dav_files(str(test_dir))
        
        assert result is True
        assert not dav_file.exists()
        assert video_file.exists()  # Should not be deleted
    
    def test_cleanup_temporary_files(self, tmp_path):
        """Test temporary file cleanup."""
        # Create test directory with files
        test_dir = tmp_path / 'test_group'
        test_dir.mkdir()
        
        # Create test files
        tmp_file = test_dir / 'test.tmp'
        tmp_file.write_text('test temp content')
        
        video_file = test_dir / 'combined.mp4'
        video_file.write_text('test video content')
        
        service = CleanupService(str(tmp_path))
        
        result = service.cleanup_temporary_files(str(test_dir))
        
        assert result is True
        assert not tmp_file.exists()
        assert video_file.exists()  # Should not be deleted
    
    @patch('video_grouper.task_processors.services.cleanup_service.DirectoryState')
    def test_should_cleanup_dav_files(self, mock_dir_state, tmp_path):
        """Test DAV cleanup readiness check."""
        # Create test directory with files
        test_dir = tmp_path / 'test_group'
        test_dir.mkdir()
        
        # Create test files
        dav_file = test_dir / 'test.dav'
        dav_file.write_text('test dav content')
        
        video_file = test_dir / 'combined.mp4'
        video_file.write_text('test video content')
        
        service = CleanupService(str(tmp_path))
        
        # Mock directory state with appropriate status
        mock_state = Mock()
        mock_state.status = 'combined'
        mock_dir_state.return_value = mock_state
        
        result = service.should_cleanup_dav_files(str(test_dir))
        assert result is True
        
        # Test with inappropriate status
        mock_state.status = 'downloading'
        result = service.should_cleanup_dav_files(str(test_dir))
        assert result is False  # False because status is not safe for cleanup


class TestCloudSyncService:
    """Test cloud sync service functionality."""
    
    @patch('video_grouper.task_processors.services.cloud_sync_service.CloudSync')
    def test_init_enabled(self, mock_cloud_sync_class, tmp_path):
        """Test service initialization when enabled."""
        config = configparser.ConfigParser()
        config.add_section('CLOUD_SYNC')
        config['CLOUD_SYNC']['enabled'] = 'true'
        
        mock_cloud_sync = Mock()
        mock_cloud_sync.enabled = True
        mock_cloud_sync_class.return_value = mock_cloud_sync
        
        service = CloudSyncService(config, str(tmp_path))
        assert service.enabled
        assert service.cloud_sync is not None
    
    def test_get_final_video_path(self, tmp_path, mock_file_system):
        """Test getting final video path."""
        config = configparser.ConfigParser()
        
        # Create test directory
        test_dir = tmp_path / 'test_group'
        test_dir.mkdir()
        
        # Create service
        service = CloudSyncService(config, str(tmp_path))
        
        # Test 1: No video files - mock os.path.exists to return False for video files
        trimmed_path = str(test_dir / 'trimmed.mp4')
        combined_path = str(test_dir / 'combined.mp4')
        
        def mock_exists_side_effect(path):
            if path in [trimmed_path, combined_path]:
                return False
            return True
        
        mock_file_system['exists'].side_effect = mock_exists_side_effect
        
        result = service._get_final_video_path(str(test_dir))
        assert result is None
        
        # Test 2: Only combined.mp4 exists
        def mock_exists_side_effect_combined(path):
            if path == trimmed_path:
                return False
            elif path == combined_path:
                return True
            return True
        
        mock_file_system['exists'].side_effect = mock_exists_side_effect_combined
        
        result = service._get_final_video_path(str(test_dir))
        assert result == combined_path
        
        # Test 3: Both files exist, should prefer trimmed.mp4
        def mock_exists_side_effect_both(path):
            if path in [trimmed_path, combined_path]:
                return True
            return True
        
        mock_file_system['exists'].side_effect = mock_exists_side_effect_both
        
        result = service._get_final_video_path(str(test_dir))
        assert result == trimmed_path
        
        # Test 4: Only trimmed exists
        def mock_exists_side_effect_trimmed(path):
            if path == trimmed_path:
                return True
            elif path == combined_path:
                return False
            return True
        
        mock_file_system['exists'].side_effect = mock_exists_side_effect_trimmed
        
        result = service._get_final_video_path(str(test_dir))
        assert result == trimmed_path
    
    @patch('video_grouper.task_processors.services.cloud_sync_service.CloudSync')
    @patch('video_grouper.task_processors.services.cloud_sync_service.DirectoryState')
    def test_should_sync_directory(self, mock_dir_state, mock_cloud_sync_class, tmp_path, mock_file_system):
        """Test sync readiness check."""
        config = configparser.ConfigParser()
        config.add_section('CLOUD_SYNC')
        config['CLOUD_SYNC']['enabled'] = 'true'
        
        # Create test directory with files
        test_dir = tmp_path / 'test_group'
        test_dir.mkdir()
        
        # Mock CloudSync to be enabled
        mock_cloud_sync = Mock()
        mock_cloud_sync.enabled = True
        mock_cloud_sync_class.return_value = mock_cloud_sync
        
        # Mock directory state
        mock_state = Mock()
        mock_state.status = 'trimmed'
        mock_dir_state.return_value = mock_state
        
        service = CloudSyncService(config, str(tmp_path))
        # Manually set enabled to True since we mocked the CloudSync
        service.enabled = True
        
        # Mock file exists for video file
        trimmed_path = str(test_dir / 'trimmed.mp4')
        sync_marker_path = str(test_dir / '.cloud_synced')
        
        def mock_exists_side_effect(path):
            if path == trimmed_path:
                return True
            elif path == sync_marker_path:
                return False  # Not synced yet
            return True
        
        mock_file_system['exists'].side_effect = mock_exists_side_effect
        
        result = service.should_sync_directory(str(test_dir))
        assert result is True
        
        # Test with inappropriate status
        mock_state.status = 'downloading'
        result = service.should_sync_directory(str(test_dir))
        assert result is False
    
    @patch('video_grouper.task_processors.services.cloud_sync_service.CloudSync')
    @patch('video_grouper.task_processors.services.cloud_sync_service.DirectoryState')
    def test_sync_directory(self, mock_dir_state, mock_cloud_sync_class, tmp_path, mock_file_system):
        """Test directory sync."""
        config = configparser.ConfigParser()
        config.add_section('CLOUD_SYNC')
        config['CLOUD_SYNC']['enabled'] = 'true'
        
        # Create test directory with files
        test_dir = tmp_path / 'test_group'
        test_dir.mkdir()
        
        # Mock CloudSync to be enabled
        mock_cloud_sync = Mock()
        mock_cloud_sync.enabled = True
        mock_cloud_sync.sync_files_from_directory.return_value = True
        mock_cloud_sync_class.return_value = mock_cloud_sync
        
        # Mock directory state
        mock_state = Mock()
        mock_state.status = 'trimmed'
        mock_dir_state.return_value = mock_state
        
        service = CloudSyncService(config, str(tmp_path))
        # Manually set enabled to True since we mocked the CloudSync
        service.enabled = True
        
        # Mock file exists for video file
        trimmed_path = str(test_dir / 'trimmed.mp4')
        sync_marker_path = str(test_dir / '.cloud_synced')
        
        def mock_exists_side_effect(path):
            if path == trimmed_path:
                return True
            elif path == sync_marker_path:
                return False  # Not synced yet
            return True
        
        mock_file_system['exists'].side_effect = mock_exists_side_effect
        
        result = service.sync_directory(str(test_dir))
        
        assert result is True
        mock_cloud_sync.sync_files_from_directory.assert_called_once_with(str(test_dir))
        
        # Check sync marker was created (we'll mock the file creation)
        sync_marker = test_dir / '.cloud_synced'
        # Since we're mocking file operations, we can't actually check if the file exists
        # but we can verify the method was called correctly
    
    def test_get_sync_status(self, tmp_path):
        """Test sync status reporting."""
        config = configparser.ConfigParser()
        
        # Create test directory with files
        test_dir = tmp_path / 'test_group'
        test_dir.mkdir()
        
        # Create video file
        video_file = test_dir / 'trimmed.mp4'
        video_file.write_text('test video content')
        
        service = CloudSyncService(config, str(tmp_path))
        
        status = service.get_sync_status(str(test_dir))
        
        assert 'enabled' in status
        assert 'should_sync' in status
        assert 'already_synced' in status
        assert 'final_video_exists' in status


if __name__ == '__main__':
    pytest.main([__file__]) 