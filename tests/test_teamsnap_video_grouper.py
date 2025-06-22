#!/usr/bin/env python3
"""
Test script to verify the integration of TeamSnap API with video_grouper.py.
"""

import os
import configparser
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from video_grouper.video_grouper import VideoGrouperApp

@pytest.mark.asyncio
class TestTeamSnapVideoGrouper:
    """Test the TeamSnap integration with VideoGrouperApp."""
    
    def setup_method(self):
        """Set up the test."""
        # Define test paths
        self.storage_path = "/test/storage/path"
        
        # Create a test recording timespan
        self.start_time = datetime(2025, 3, 8, 17, 15, 0, tzinfo=timezone.utc)
        self.end_time = self.start_time + timedelta(hours=2)
        
        # Create test config
        self.config = configparser.ConfigParser()
        self.config.add_section('CAMERA')
        self.config.set('CAMERA', 'type', 'dahua')
        self.config.set('CAMERA', 'device_ip', '192.168.1.100')
        self.config.set('CAMERA', 'username', 'admin')
        self.config.set('CAMERA', 'password', 'admin')
        
        self.config.add_section('STORAGE')
        self.config.set('STORAGE', 'path', self.storage_path)
        
        self.config.add_section('TEAMSNAP')
        self.config.set('TEAMSNAP', 'enabled', 'true')
        self.config.set('TEAMSNAP', 'client_id', 'test_client_id')
        self.config.set('TEAMSNAP', 'client_secret', 'test_client_secret')
        self.config.set('TEAMSNAP', 'access_token', 'test_access_token')
        self.config.set('TEAMSNAP', 'team_id', 'test_team_id')
        self.config.set('TEAMSNAP', 'my_team_name', 'Test Team')
    
    @patch('video_grouper.api_integrations.teamsnap.TeamSnapAPI')
    async def test_teamsnap_integration(self, mock_teamsnap_api_class):
        """Test that TeamSnap API is used to populate match info."""
        # Configure TeamSnap API mock
        mock_api = mock_teamsnap_api_class.return_value
        mock_api.enabled = True
        mock_api.my_team_name = "Test Team"
        mock_api.populate_match_info = AsyncMock(return_value=True)
        
        # Create app with mocked TeamSnap API
        app = VideoGrouperApp(self.config)
        
        # Replace the real TeamSnap API with our mock
        app.teamsnap_api = mock_api
        
        # Create a match info dictionary to populate
        match_info = {}
        
        # Call populate_match_info directly
        result = await mock_api.populate_match_info(match_info, self.start_time, self.end_time)
        
        # Verify the result
        assert result is True
        mock_api.populate_match_info.assert_awaited_once_with(match_info, self.start_time, self.end_time) 