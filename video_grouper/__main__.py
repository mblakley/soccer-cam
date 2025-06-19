#!/usr/bin/env python
import os
import sys
import configparser
import asyncio
import logging
import argparse
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

from video_grouper.video_grouper import VideoGrouperApp

# Global variable to track tasks
tasks = []

async def main():
    """Main entry point for the application."""
    parser = argparse.ArgumentParser(description="Video Grouper Application")
    
    # Default config path is in the same directory as this script
    default_config_path = os.path.join(os.path.dirname(__file__), "config.ini")
    
    parser.add_argument(
        '--config', 
        type=str, 
        default=default_config_path,
        help=f"Path to the configuration file (default: {default_config_path})"
    )
    args = parser.parse_args()

    try:        
        config_path = args.config
                
        if not os.path.exists(config_path):
            logger.error(f"Configuration file not found. Looked in: {config_path}")
            return 1
        
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
        queue_state_file = os.path.join(storage_path, "ffmpeg_queue_state.json")
        camera_state_file = os.path.join(storage_path, "camera_state.json")
        latest_file = os.path.join(storage_path, "latest_video.txt")
        
        logger.info(f"State files:")
        logger.info(f"  Queue state: {queue_state_file}")
        logger.info(f"  Camera state: {camera_state_file}")
        logger.info(f"  Latest video: {latest_file}")
        
        # Create and initialize the app
        app = VideoGrouperApp(config)
        
        try:
            await app.run()
        except asyncio.CancelledError:
            logger.info("Main task cancelled, shutting down.")
        finally:
            logger.info("Saving queue states before exit...")
            await app.shutdown()
        
        return 0
    except Exception as e:
        logger.exception(f"Unhandled error in main application loop: {e}")
        return 1

def main_entry():
    """Entry point for console script."""
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received, shutting down...")
        sys.exit(0)

if __name__ == "__main__":
    main_entry() 