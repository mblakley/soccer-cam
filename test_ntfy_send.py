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
from pathlib import Path
from datetime import datetime

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

async def test_ntfy_send():
    """Test sending a notification via NTFY."""
    # Find the config file
    config_file = None
    for path in ["config.ini", "shared_data/config.ini"]:
        if os.path.exists(path):
            config_file = path
            break
    
    if not config_file:
        logger.error("Config file not found")
        return
    
    logger.info(f"Using config file: {config_file}")
    
    # Read the config file
    config = configparser.ConfigParser()
    config.read(config_file)
    
    # Get the NTFY config
    ntfy_enabled = config.getboolean("NTFY", "enabled", fallback=False)
    ntfy_topic = config.get("NTFY", "topic", fallback="")
    
    logger.info(f"NTFY config: enabled={ntfy_enabled}, topic={ntfy_topic}")
    
    if not ntfy_enabled or not ntfy_topic:
        logger.error("NTFY not enabled or topic not set")
        return
    
    # Initialize the NTFY API
    ntfy_api = NtfyAPI(config)
    logger.info(f"NTFY API initialized with topic: {ntfy_api.topic}")
    
    # Start the NTFY response listener
    await ntfy_api.initialize()
    
    # Send a test notification
    logger.info("Sending test notification...")
    sent = await ntfy_api.send_notification(
        message="This is a test notification from soccer-cam",
        title="Test Notification",
        tags=["test"],
        priority=4
    )
    
    if sent:
        logger.info("Test notification sent successfully")
    else:
        logger.error("Failed to send test notification")
    
    # Wait a bit to make sure the notification is sent
    await asyncio.sleep(2)
    
    # Test asking for team info
    logger.info("Sending test prompt for missing team info...")
    team_info = await ntfy_api.ask_team_info(None, {})
    logger.info(f"Test prompt response: {team_info}")
    
    # Create a temporary test video file
    test_video_path = None
    if os.path.exists("tests/test_data/test.mp4"):
        test_video_path = "tests/test_data/test.mp4"
    else:
        logger.warning("Test video file not found, skipping video-related tests")
    
    if test_video_path:
        # Test asking for game start time
        logger.info("Sending test notification for game start time...")
        start_time = await ntfy_api.ask_game_start_time(test_video_path, os.path.dirname(test_video_path))
        logger.info(f"Game start time notification sent: {start_time}")
        
        # Test asking for game end time
        logger.info("Sending test notification for game end time...")
        end_time = await ntfy_api.ask_game_end_time(test_video_path, os.path.dirname(test_video_path), "00:05:00")
        logger.info(f"Game end time notification sent: {end_time}")
    
    # Close the NTFY API
    await ntfy_api.close()

if __name__ == "__main__":
    asyncio.run(test_ntfy_send()) 