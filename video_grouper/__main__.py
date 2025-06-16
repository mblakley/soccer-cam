#!/usr/bin/env python
import os
import sys
import configparser
import asyncio
import logging
import time
from datetime import datetime

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d - %(message)s'
)
logger = logging.getLogger(__name__)

# Add the parent directory to sys.path to allow absolute imports
parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, parent_dir)

from video_grouper.video_grouper import VideoGrouperApp
import httpx

async def main():
    """Main entry point for the application."""
    try:
        # Look for config.ini in multiple locations
        config_paths = [
            os.path.join(os.getcwd(), "config.ini"),                  # Current directory
            os.path.join(os.path.dirname(__file__), "config.ini"),    # video_grouper directory
            os.path.join(parent_dir, "config.ini"),                   # Parent directory
            os.path.join(os.getcwd(), "video_grouper", "config.ini")  # video_grouper subdirectory
        ]
        
        config_path = None
        for path in config_paths:
            if os.path.exists(path):
                config_path = path
                break
                
        if not config_path:
            logger.error("Configuration file not found. Looked in: " + ", ".join(config_paths))
            print("Configuration file not found. Looked in: " + ", ".join(config_paths))
            return 1
        
        print(f"Using configuration file: {config_path}")
        logger.info(f"Using configuration file: {config_path}")
            
        config = configparser.ConfigParser()
        config.read(config_path)
        
        # Create and initialize the app
        app = VideoGrouperApp(config)
        await app.initialize()
        
        # Create tasks for processing
        tasks = [
            asyncio.create_task(app.process_ffmpeg_queue()),
            asyncio.create_task(app.poll_camera_and_download())
        ]
        
        # Wait for all tasks to complete (they should run forever)
        await asyncio.gather(*tasks)
        
        return 0
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt, shutting down...")
        return 0
    except Exception as e:
        logger.exception(f"Error running application: {e}")
        return 1

if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code) 