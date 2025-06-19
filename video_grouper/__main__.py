#!/usr/bin/env python
import os
import sys
import configparser
import asyncio
import logging
import argparse
import signal
from video_grouper.video_grouper import VideoGrouperApp
from video_grouper.paths import get_shared_data_path
from video_grouper.locking import FileLock

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d - %(message)s'
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Add the parent directory to sys.path to allow absolute imports
parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, parent_dir)

# Global variable to track tasks
tasks = []

def load_config():
    """Loads configuration from the shared data directory."""
    config = configparser.ConfigParser()
    config_path = get_shared_data_path() / 'config.ini'
    
    try:
        with FileLock(config_path):
            if not config_path.exists():
                logger.error(f"Configuration file not found at {config_path}. Please create it or run the UI first.")
                return None
            config.read(config_path)
    except TimeoutError:
        logger.error(f"Could not acquire lock to read config file at {config_path}.")
        return None
        
    return config

async def main():
    """Main entry point for the application."""
    config = load_config()
    if not config:
        logger.error("Failed to load configuration. Exiting.")
        return
        
    app = VideoGrouperApp(config)
    
    try:
        await app.run()
    except asyncio.CancelledError:
        logger.info("Application is shutting down.")
    finally:
        await app.shutdown()

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