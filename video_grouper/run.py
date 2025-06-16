#!/usr/bin/env python
"""
Simple script to run the video_grouper application from within the video_grouper directory.
"""
import os
import sys
import asyncio
import configparser
import logging
import signal

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d - %(message)s'
)
logger = logging.getLogger(__name__)

# Add the parent directory to sys.path to allow absolute imports
parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, parent_dir)

# Import after setting up sys.path
from video_grouper.video_grouper import VideoGrouperApp

# Global variable to track tasks
tasks = []

async def main():
    """Main entry point for the application."""
    try:
        # Look for config.ini in the current directory
        config_path = os.path.join(os.path.dirname(__file__), "config.ini")
        
        if not os.path.exists(config_path):
            logger.error(f"Configuration file not found: {config_path}")
            print(f"Configuration file not found: {config_path}")
            return 1
        
        print(f"Using configuration file: {config_path}")
        logger.info(f"Using configuration file: {config_path}")
            
        config = configparser.ConfigParser()
        config.read(config_path)
        
        # Log the configuration values
        logger.info("Configuration values:")
        for section in config.sections():
            logger.info(f"  [{section}]")
            for key, value in config[section].items():
                logger.info(f"    {key} = {value}")
        
        # Ensure storage path exists
        storage_path = config.get('STORAGE', 'path')
        logger.info(f"Storage path: {os.path.abspath(storage_path)}")
        os.makedirs(storage_path, exist_ok=True)
        
        # Log state file locations
        state_file = os.path.join(storage_path, "processing_state.json")
        queue_state_file = os.path.join(storage_path, "ffmpeg_queue_state.json")
        camera_state_file = os.path.join(storage_path, "camera_state.json")
        latest_file = os.path.join(storage_path, "latest_video.txt")
        
        logger.info(f"State files:")
        logger.info(f"  Processing state: {state_file}")
        logger.info(f"  Queue state: {queue_state_file}")
        logger.info(f"  Camera state: {camera_state_file}")
        logger.info(f"  Latest video: {latest_file}")
        
        # Create and initialize the app
        app = VideoGrouperApp(config)
        await app.initialize()
        
        # Create tasks
        global tasks
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
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received, shutting down...")
        sys.exit(0) 