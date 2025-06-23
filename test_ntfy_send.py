#!/usr/bin/env python3
"""
Test script to send a notification to the NTFY topic.
This script will help diagnose issues with the NTFY integration.
"""
import os
import sys
import asyncio
import configparser
import logging
import pytest
from pathlib import Path
from datetime import datetime
from unittest.mock import patch, AsyncMock, MagicMock

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(name)s:%(filename)s:%(lineno)d - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Add the parent directory to the path so we can import video_grouper modules
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from video_grouper.api_integrations.ntfy import NtfyAPI

@pytest.mark.asyncio
async def test_ntfy_send():
    """Test sending a notification via NTFY."""
    # Create a mock config
    config = configparser.ConfigParser()
    config.add_section('NTFY')
    config.set('NTFY', 'enabled', 'true')
    config.set('NTFY', 'topic', 'test-topic')
    
    # Initialize the NTFY API
    ntfy_api = NtfyAPI(config)
    logger.info(f"NTFY API initialized with topic: {ntfy_api.topic}")
    
    # Mock the send_notification method
    with patch.object(ntfy_api, 'send_notification', AsyncMock(return_value=True)), \
         patch.object(ntfy_api, 'initialize', AsyncMock(return_value=None)), \
         patch.object(ntfy_api, 'close', AsyncMock(return_value=None)), \
         patch.object(ntfy_api, '_listen_for_responses', AsyncMock(return_value=None)), \
         patch('video_grouper.api_integrations.ntfy.get_video_duration', AsyncMock(return_value='01:30:00')), \
         patch('video_grouper.api_integrations.ntfy.create_screenshot', AsyncMock(return_value=True)), \
         patch('video_grouper.api_integrations.ntfy.compress_image', AsyncMock(return_value='test_screenshot.jpg')), \
         patch('video_grouper.api_integrations.ntfy.os.path.exists', return_value=True), \
         patch('video_grouper.api_integrations.ntfy.os.remove', return_value=None), \
         patch('asyncio.create_task', side_effect=lambda x: x):
        
        # Initialize the NTFY API (mocked)
        await ntfy_api.initialize()
        
        # Test sending a notification
        logger.info("Sending test notification...")
        sent = await ntfy_api.send_notification(
            message="This is a test notification from soccer-cam",
            title="Test Notification",
            tags=["test"],
            priority=4
        )
        
        # Verify the notification was sent (mocked)
        assert sent is True
        ntfy_api.send_notification.assert_called_once()
        args, kwargs = ntfy_api.send_notification.call_args
        assert kwargs['message'] == 'This is a test notification from soccer-cam'
        assert kwargs['title'] == 'Test Notification'
        assert kwargs['tags'] == ['test']
        assert kwargs['priority'] == 4
        
        logger.info("Test notification sent successfully (mocked)")
        
        # Reset mock
        ntfy_api.send_notification.reset_mock()
        
        # Test asking for team info
        logger.info("Testing team info notification...")
        team_info = await ntfy_api.ask_team_info(None, {})
        
        # Verify the notification was sent (mocked)
        ntfy_api.send_notification.assert_called_once()
        args, kwargs = ntfy_api.send_notification.call_args
        assert 'Missing match information' in kwargs['message']
        assert kwargs['title'] == 'Missing Match Information'
        
        logger.info(f"Team info notification sent successfully (mocked)")
        
        # Reset mock
        ntfy_api.send_notification.reset_mock()
        
        # Create a temporary test video file path (not actually creating the file)
        test_video_path = "tests/test_data/test.mp4"
        
        # Test asking for game start time
        logger.info("Testing game start time notification...")
        start_time = await ntfy_api.ask_game_start_time(test_video_path, os.path.dirname(test_video_path))
        
        # Verify the notification was sent (mocked)
        ntfy_api.send_notification.assert_called_once()
        args, kwargs = ntfy_api.send_notification.call_args
        assert 'Game start time' in kwargs['message']
        assert kwargs['title'] == 'Set Game Start Time'
        
        logger.info(f"Game start time notification sent successfully (mocked)")
        
        # Reset mock
        ntfy_api.send_notification.reset_mock()
        
        # Test asking for game end time
        logger.info("Testing game end time notification...")
        end_time = await ntfy_api.ask_game_end_time(test_video_path, os.path.dirname(test_video_path), "00:05:00")
        
        # Verify the notification was sent (mocked)
        ntfy_api.send_notification.assert_called_once()
        args, kwargs = ntfy_api.send_notification.call_args
        assert 'Game end time' in kwargs['message']
        assert kwargs['title'] == 'Set Game End Time'
        
        logger.info(f"Game end time notification sent successfully (mocked)")
        
        # Close the NTFY API (mocked)
        await ntfy_api.close()

if __name__ == "__main__":
    asyncio.run(test_ntfy_send()) 