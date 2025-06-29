"""
Tests for the enhanced StateAuditor with service integrations.
"""

import pytest
import asyncio
import os
import tempfile
import configparser
from unittest.mock import Mock, AsyncMock, patch, MagicMock

from video_grouper.task_processors.state_auditor import StateAuditor
from video_grouper.task_processors.state_auditor import DirectoryState


class TestStateAuditorEnhanced:
    """Test enhanced StateAuditor functionality with services."""
    
    @pytest.fixture
    def config(self):
        """Create test configuration."""
        config = configparser.ConfigParser()
        
        # Add required sections
        config.add_section('TEAMSNAP')
        config['TEAMSNAP']['enabled'] = 'true'
        
        config.add_section('PLAYMETRICS')
        config['PLAYMETRICS']['enabled'] = 'true'
        
        config.add_section('NTFY')
        config['NTFY']['enabled'] = 'true'
        
        config.add_section('CLOUD_SYNC')
        config['CLOUD_SYNC']['enabled'] = 'true'
        
        config.add_section('YOUTUBE')
        config['YOUTUBE']['enabled'] = 'true'
        
        return config
    
    @pytest.fixture
    def test_dir(self, tmp_path):
        """Create test directory with state file."""
        test_dir = tmp_path / 'test_group'
        test_dir.mkdir()
        
        # Create state.json file
        state_file = test_dir / 'state.json'
        state_file.write_text('{"status": "combined", "files": {}}')
        
        # Create combined.mp4
        combined_file = test_dir / 'combined.mp4'
        combined_file.write_text('test video content')
        
        yield test_dir
    
    @patch('video_grouper.task_processors.services.teamsnap_service.TeamSnapAPI')
    @patch('video_grouper.task_processors.services.playmetrics_service.PlayMetricsAPI')
    @patch('video_grouper.task_processors.services.ntfy_service.NtfyAPI')
    @patch('video_grouper.task_processors.services.cloud_sync_service.CloudSync')
    def test_init_with_services(self, mock_cloud_sync, mock_ntfy, mock_playmetrics, mock_teamsnap, config, tmp_path):
        """Test StateAuditor initialization with all services."""
        # Mock all the APIs
        mock_teamsnap.return_value.enabled = True
        mock_playmetrics.return_value.enabled = True
        mock_playmetrics.return_value.login.return_value = True
        mock_ntfy.return_value.enabled = True
        mock_cloud_sync.return_value.enabled = True
        
        auditor = StateAuditor(str(tmp_path), config)
        
        # Check that all services are initialized
        assert auditor.teamsnap_service is not None
        assert auditor.playmetrics_service is not None
        assert auditor.ntfy_service is not None
        assert auditor.match_info_service is not None
        assert auditor.cleanup_service is not None
        assert auditor.cloud_sync_service is not None
    
    @patch('video_grouper.task_processors.services.teamsnap_service.TeamSnapAPI')
    @patch('video_grouper.task_processors.services.playmetrics_service.PlayMetricsAPI')
    @patch('video_grouper.task_processors.services.ntfy_service.NtfyAPI')
    @patch('video_grouper.task_processors.services.cloud_sync_service.CloudSync')
    @patch('video_grouper.task_processors.state_auditor.DirectoryState')
    @patch('os.path.exists')
    @pytest.mark.asyncio
    async def test_audit_combined_directory_with_match_info(self, mock_exists, mock_dir_state, mock_cloud_sync, 
                                                           mock_ntfy, mock_playmetrics, mock_teamsnap, 
                                                           test_dir, config, tmp_path):
        """Test auditing a combined directory with match info processing."""
        # Mock all the APIs
        mock_teamsnap.return_value.enabled = True
        mock_playmetrics.return_value.enabled = True
        mock_playmetrics.return_value.login.return_value = True
        mock_ntfy.return_value.enabled = True
        mock_ntfy.return_value.initialize = AsyncMock()
        mock_cloud_sync.return_value.enabled = True
        
        # Mock directory state
        mock_state = Mock()
        mock_state.status = 'combined'
        # Create a mock that behaves like a dict with empty values
        mock_files = Mock()
        mock_files.values.return_value = []
        mock_state.files = mock_files
        mock_state.is_ready_for_combining.return_value = False
        mock_dir_state.return_value = mock_state
        
        # Create combined.mp4 file (required for processing)
        combined_file = test_dir / 'combined.mp4'
        combined_file.write_text('test video content')
        
        # Create state.json file (required for processing)
        state_file = test_dir / 'state.json'
        state_file.write_text('{"status": "combined"}')
        
        # Create auditor
        auditor = StateAuditor(str(tmp_path), config)
        
        # Mock processors
        auditor.video_processor = Mock()
        auditor.video_processor.add_work = AsyncMock()
        
        # Mock match info service
        auditor.match_info_service.is_waiting_for_user_input = Mock(return_value=False)
        auditor.match_info_service.process_combined_directory = AsyncMock(return_value=True)
        
        # Configure the file system properly
        state_path = str(test_dir / 'state.json')
        combined_path = str(test_dir / 'combined.mp4')
        match_info_path = str(test_dir / 'match_info.ini')
        
        def mock_exists_side_effect(path):
            # Convert to string for comparison
            path_str = str(path)
            if path_str == state_path:
                return True
            elif path_str == combined_path:
                return True
            elif path_str == match_info_path:
                return False  # No match info file exists yet
            elif 'ntfy_service_state.json' in path_str:
                return False  # Any NTFY state file doesn't exist
            else:
                return True  # Default to True for other paths (like directory checks)
        
        mock_exists.side_effect = mock_exists_side_effect
        
        # Run audit
        await auditor._audit_directory(str(test_dir))
        
        # Verify match info processing was called
        auditor.match_info_service.process_combined_directory.assert_called_once()
    
    @patch('video_grouper.task_processors.services.teamsnap_service.TeamSnapAPI')
    @patch('video_grouper.task_processors.services.playmetrics_service.PlayMetricsAPI')
    @patch('video_grouper.task_processors.services.ntfy_service.NtfyAPI')
    @patch('video_grouper.task_processors.services.cloud_sync_service.CloudSync')
    @patch('video_grouper.task_processors.state_auditor.DirectoryState')
    @patch('os.path.exists')
    @pytest.mark.asyncio
    async def test_audit_with_user_input_waiting(self, mock_exists, mock_dir_state, mock_cloud_sync, 
                                                mock_ntfy, mock_playmetrics, mock_teamsnap, 
                                                test_dir, config, tmp_path):
        """Test auditing when waiting for user input."""
        # Mock all the APIs
        mock_teamsnap.return_value.enabled = True
        mock_playmetrics.return_value.enabled = True
        mock_playmetrics.return_value.login.return_value = True
        mock_ntfy.return_value.enabled = True
        mock_ntfy.return_value.initialize = AsyncMock()
        mock_cloud_sync.return_value.enabled = True
        
        # Mock directory state
        mock_state = Mock()
        mock_state.status = 'combined'
        # Create a mock that behaves like a dict with empty values
        mock_files = Mock()
        mock_files.values.return_value = []
        mock_state.files = mock_files
        mock_state.is_ready_for_combining.return_value = False
        mock_dir_state.return_value = mock_state
        
        # Create auditor
        auditor = StateAuditor(str(tmp_path), config)
        
        # Mock match info service - waiting for input
        auditor.match_info_service.is_waiting_for_user_input = Mock(return_value=True)
        auditor.match_info_service.process_combined_directory = AsyncMock()
        
        # Mock file system
        state_path = str(test_dir / 'state.json')
        combined_path = str(test_dir / 'combined.mp4')
        
        def mock_exists_side_effect(path):
            path_str = str(path)
            if path_str == state_path:
                return True
            elif path_str == combined_path:
                return True
            elif 'ntfy_service_state.json' in path_str:
                return False
            else:
                return True
        
        mock_exists.side_effect = mock_exists_side_effect
        
        # Run audit
        await auditor._audit_directory(str(test_dir))
        
        # Verify match info processing was NOT called
        auditor.match_info_service.process_combined_directory.assert_not_called()
    
    @patch('video_grouper.task_processors.services.teamsnap_service.TeamSnapAPI')
    @patch('video_grouper.task_processors.services.playmetrics_service.PlayMetricsAPI')
    @patch('video_grouper.task_processors.services.ntfy_service.NtfyAPI')
    @patch('video_grouper.task_processors.services.cloud_sync_service.CloudSync')
    @patch('video_grouper.task_processors.state_auditor.DirectoryState')
    @patch('os.path.exists')
    @pytest.mark.asyncio
    async def test_cleanup_and_sync_handling(self, mock_exists, mock_dir_state, mock_cloud_sync, 
                                            mock_ntfy, mock_playmetrics, mock_teamsnap, 
                                            test_dir, config, tmp_path):
        """Test cleanup and sync handling."""
        # Mock all the APIs
        mock_teamsnap.return_value.enabled = True
        mock_playmetrics.return_value.enabled = True
        mock_playmetrics.return_value.login.return_value = True
        mock_ntfy.return_value.enabled = True
        mock_ntfy.return_value.initialize = AsyncMock()
        mock_cloud_sync_instance = Mock()
        mock_cloud_sync_instance.enabled = True
        mock_cloud_sync.return_value = mock_cloud_sync_instance
        
        # Mock directory state
        mock_state = Mock()
        mock_state.status = 'autocam_complete'
        # Create a mock that behaves like a dict with empty values
        mock_files = Mock()
        mock_files.values.return_value = []
        mock_state.files = mock_files
        mock_state.is_ready_for_combining.return_value = False
        mock_dir_state.return_value = mock_state
        
        # Create auditor
        auditor = StateAuditor(str(tmp_path), config)
        
        # Mock services
        auditor.cloud_sync_service.should_sync_directory = Mock(return_value=True)
        auditor.cloud_sync_service.sync_directory = Mock(return_value=True)
        auditor.cleanup_service.should_cleanup_dav_files = Mock(return_value=True)
        auditor.cleanup_service.cleanup_dav_files = Mock(return_value=True)
        auditor.cleanup_service.cleanup_temporary_files = Mock(return_value=True)
        
        # Mock file system
        state_path = str(test_dir / 'state.json')
        
        def mock_exists_side_effect(path):
            path_str = str(path)
            if path_str == state_path:
                return True
            elif 'ntfy_service_state.json' in path_str:
                return False
            else:
                return True
        
        mock_exists.side_effect = mock_exists_side_effect
        
        # Run cleanup and sync
        await auditor._handle_cleanup_and_sync(str(test_dir), mock_state)
        
        # Verify all cleanup and sync operations were called
        auditor.cloud_sync_service.should_sync_directory.assert_called_once_with(str(test_dir))
        auditor.cloud_sync_service.sync_directory.assert_called_once_with(str(test_dir))
        auditor.cleanup_service.should_cleanup_dav_files.assert_called_once_with(str(test_dir))
        auditor.cleanup_service.cleanup_dav_files.assert_called_once_with(str(test_dir))
        auditor.cleanup_service.cleanup_temporary_files.assert_called_once_with(str(test_dir))
    
    @patch('video_grouper.task_processors.services.teamsnap_service.TeamSnapAPI')
    @patch('video_grouper.task_processors.services.playmetrics_service.PlayMetricsAPI')
    @patch('video_grouper.task_processors.services.ntfy_service.NtfyAPI')
    @patch('video_grouper.task_processors.services.cloud_sync_service.CloudSync')
    @patch('video_grouper.task_processors.state_auditor.DirectoryState')
    @patch('os.path.exists')
    @pytest.mark.asyncio
    async def test_youtube_upload_queuing(self, mock_exists, mock_dir_state, mock_cloud_sync, 
                                         mock_ntfy, mock_playmetrics, mock_teamsnap, 
                                         test_dir, config, tmp_path):
        """Test YouTube upload task queuing."""
        # Mock all the APIs
        mock_teamsnap.return_value.enabled = True
        mock_playmetrics.return_value.enabled = True
        mock_playmetrics.return_value.login.return_value = True
        mock_ntfy.return_value.enabled = True
        mock_ntfy.return_value.initialize = AsyncMock()
        mock_cloud_sync.return_value.enabled = True
        
        # Mock directory state
        mock_state = Mock()
        mock_state.status = 'autocam_complete'
        # Create a mock that behaves like a dict with empty values
        mock_files = Mock()
        mock_files.values.return_value = []
        mock_state.files = mock_files
        mock_state.is_ready_for_combining.return_value = False
        mock_dir_state.return_value = mock_state
        
        # Create state.json file (required for processing)
        state_file = test_dir / 'state.json'
        state_file.write_text('{"status": "autocam_complete"}')
        
        # Create auditor
        auditor = StateAuditor(str(tmp_path), config)
        
        # Mock upload processor
        auditor.upload_processor = Mock()
        auditor.upload_processor.add_work = AsyncMock()
        
        # Mock services
        auditor.cloud_sync_service.should_sync_directory = Mock(return_value=False)
        auditor.cleanup_service.should_cleanup_dav_files = Mock(return_value=False)
        auditor.cleanup_service.cleanup_temporary_files = Mock(return_value=True)
        
        # Mock file system to ensure state.json exists
        state_path = str(test_dir / 'state.json')
        
        def mock_exists_side_effect(path):
            path_str = str(path)
            if path_str == state_path:
                return True
            elif 'ntfy_service_state.json' in path_str:
                return False
            else:
                return True  # Default to True for other paths
        
        mock_exists.side_effect = mock_exists_side_effect
        
        # Run audit
        await auditor._audit_directory(str(test_dir))
        
        # Verify YouTube upload was queued
        auditor.upload_processor.add_work.assert_called_once()
        
        # Check that the task is a VideoUploadTask
        call_args = auditor.upload_processor.add_work.call_args[0]
        task = call_args[0]
        assert hasattr(task, 'item_path')
        assert task.item_path == str(test_dir)
    
    @patch('video_grouper.task_processors.services.teamsnap_service.TeamSnapAPI')
    @patch('video_grouper.task_processors.services.playmetrics_service.PlayMetricsAPI')
    @patch('video_grouper.task_processors.services.ntfy_service.NtfyAPI')
    @patch('video_grouper.task_processors.services.cloud_sync_service.CloudSync')
    @patch('video_grouper.task_processors.state_auditor.MatchInfo')
    @patch('video_grouper.task_processors.state_auditor.DirectoryState')
    @patch('os.path.exists')
    @pytest.mark.asyncio
    async def test_populated_match_info_triggers_trim(self, mock_exists, mock_dir_state, mock_match_info, 
                                                     mock_cloud_sync, mock_ntfy, mock_playmetrics, 
                                                     mock_teamsnap, test_dir, config, tmp_path):
        """Test that populated match info triggers trim task."""
        # Mock all the APIs
        mock_teamsnap.return_value.enabled = True
        mock_playmetrics.return_value.enabled = True
        mock_playmetrics.return_value.login.return_value = True
        mock_ntfy.return_value.enabled = True
        mock_ntfy.return_value.initialize = AsyncMock()
        mock_cloud_sync.return_value.enabled = True
        
        # Mock directory state
        mock_state = Mock()
        mock_state.status = 'combined'
        # Create a mock that behaves like a dict with empty values
        mock_files = Mock()
        mock_files.values.return_value = []
        mock_state.files = mock_files
        mock_state.is_ready_for_combining.return_value = False
        mock_dir_state.return_value = mock_state
        
        # Mock match info - populated
        mock_match_info_instance = Mock()
        mock_match_info_instance.is_populated.return_value = True
        mock_match_info_instance.get_start_offset.return_value = "00:05:00"  # 5 minutes
        mock_match_info_instance.get_total_duration_seconds.return_value = 3600  # 1 hour
        mock_match_info.from_file.return_value = mock_match_info_instance
        
        # Create match_info.ini file
        match_info_file = test_dir / 'match_info.ini'
        match_info_file.write_text('[TEAM_INFO]\nmy_team_name=Test Team\n')
        
        # Create combined.mp4 file (required for processing)
        combined_file = test_dir / 'combined.mp4'
        combined_file.write_text('test video content')
        
        # Create state.json file (required for processing)
        state_file = test_dir / 'state.json'
        state_file.write_text('{"status": "combined"}')
        
        # Create auditor
        auditor = StateAuditor(str(tmp_path), config)
        
        # Mock video processor
        auditor.video_processor = Mock()
        auditor.video_processor.add_work = AsyncMock()
        
        # Mock match info service
        auditor.match_info_service.is_waiting_for_user_input = Mock(return_value=False)
        
        # Mock file system to ensure required files exist
        state_path = str(test_dir / 'state.json')
        combined_path = str(test_dir / 'combined.mp4')
        match_info_path = str(test_dir / 'match_info.ini')
        
        def mock_exists_side_effect(path):
            path_str = str(path)
            if path_str == state_path:
                return True
            elif path_str == combined_path:
                return True
            elif path_str == match_info_path:
                return True  # Match info file exists
            elif 'ntfy_service_state.json' in path_str:
                return False
            else:
                return True  # Default to True for other paths
        
        mock_exists.side_effect = mock_exists_side_effect
        
        # Run audit
        await auditor._audit_directory(str(test_dir))
        
        # Verify trim task was queued
        auditor.video_processor.add_work.assert_called_once()
    
    @patch('video_grouper.task_processors.services.teamsnap_service.TeamSnapAPI')
    @patch('video_grouper.task_processors.services.playmetrics_service.PlayMetricsAPI')
    @patch('video_grouper.task_processors.services.ntfy_service.NtfyAPI')
    @patch('video_grouper.task_processors.services.cloud_sync_service.CloudSync')
    @patch('video_grouper.task_processors.state_auditor.DirectoryState')
    @patch('os.path.exists')
    @pytest.mark.asyncio
    async def test_service_shutdown(self, mock_exists, mock_dir_state, mock_cloud_sync, 
                                    mock_ntfy, mock_playmetrics, mock_teamsnap, 
                                    test_dir, config, tmp_path):
        """Test proper service shutdown."""
        # Mock all the APIs
        mock_teamsnap.return_value.enabled = True
        mock_playmetrics.return_value.enabled = True
        mock_playmetrics.return_value.login.return_value = True
        mock_ntfy_instance = Mock()
        mock_ntfy_instance.enabled = True
        mock_ntfy_instance.initialize = AsyncMock()
        mock_ntfy_instance.shutdown = AsyncMock()
        mock_ntfy.return_value = mock_ntfy_instance
        mock_cloud_sync.return_value.enabled = True
        
        # Create auditor
        auditor = StateAuditor(str(tmp_path), config)
        
        # Mock match info service shutdown
        auditor.match_info_service.shutdown = AsyncMock()
        
        # Stop the auditor
        await auditor.stop()
        
        # Verify shutdown was called
        auditor.match_info_service.shutdown.assert_called_once()


if __name__ == '__main__':
    pytest.main([__file__]) 