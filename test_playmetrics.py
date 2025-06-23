#!/usr/bin/env python3
"""
Test script for PlayMetrics scraper.
"""

import os
import sys
import asyncio
import logging
import configparser
from datetime import datetime

# Add the parent directory to the path so we can import the video_grouper module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import the PlayMetrics scraper
from video_grouper.api_integrations.playmetrics.scraper import PlayMetricsScraper
from video_grouper.models import MatchInfo

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(name)s:%(filename)s:%(lineno)d - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)

logger = logging.getLogger(__name__)

def test_playmetrics_scraper():
    """Test the PlayMetrics scraper."""
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
    
    # Check if PlayMetrics section exists, if not, add it
    if "PLAYMETRICS" not in config:
        logger.info("Adding PlayMetrics section to config")
        config.add_section("PLAYMETRICS")
        config.set("PLAYMETRICS", "enabled", "false")
        config.set("PLAYMETRICS", "username", "")
        config.set("PLAYMETRICS", "password", "")
        config.set("PLAYMETRICS", "team_id", "")
        config.set("PLAYMETRICS", "team_name", "")
        
        # Save the updated config
        with open(config_file, 'w') as f:
            config.write(f)
        
        logger.info("Please update the config file with your PlayMetrics credentials")
        return
    
    # Get PlayMetrics config
    playmetrics_enabled = config.getboolean("PLAYMETRICS", "enabled", fallback=False)
    
    if not playmetrics_enabled:
        logger.error("PlayMetrics integration is not enabled in the config")
        return
    
    # Initialize the PlayMetrics scraper
    scraper = PlayMetricsScraper(config)
    
    # Initialize the scraper
    if not scraper.initialize():
        logger.error("Failed to initialize PlayMetrics scraper")
        return
    
    try:
        # Login to PlayMetrics
        if not scraper.login():
            logger.error("Failed to login to PlayMetrics")
            return
        
        # Get team events
        logger.info("Getting team events...")
        events = scraper.get_team_events(days_range=30)
        
        if not events:
            logger.warning("No events found")
        else:
            logger.info(f"Found {len(events)} events:")
            for event in events:
                logger.info(f"- {event['date']} {event['time']}: {event['title']} at {event['location']}")
        
        # Test finding a game for a specific date
        today = datetime.now()
        logger.info(f"Looking for games on {today.date()}...")
        game = scraper.find_game_for_recording(today)
        
        if game:
            logger.info(f"Found game: {game['title']} at {game['location']}")
            
            # Test populating match info
            match_info = MatchInfo("test_group_dir")
            if scraper.populate_match_info(match_info, today):
                logger.info(f"Successfully populated match info: {match_info.get_team_info()}")
            else:
                logger.warning("Failed to populate match info")
        else:
            logger.warning(f"No games found for {today.date()}")
    
    finally:
        # Close the scraper
        scraper.close()

if __name__ == "__main__":
    test_playmetrics_scraper() 