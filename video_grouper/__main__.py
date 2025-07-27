#!/usr/bin/env python
import os
import sys
import asyncio
import logging
import argparse
from pathlib import Path
from video_grouper.video_grouper_app import VideoGrouperApp
from video_grouper.utils.paths import get_shared_data_path
from video_grouper.utils.locking import FileLock
from video_grouper.utils.config import load_config
from video_grouper.utils.logger import setup_logging, get_logger

# Configure basic logging first, will be updated with config later
setup_logging(level="INFO", app_name="video_grouper")
logger = get_logger(__name__)

# Add the parent directory to sys.path to allow absolute imports
parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, parent_dir)

# Global variable to track tasks
tasks = []


def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Video Grouper Application",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                           # Use default config from shared_data/config.ini
  %(prog)s --config /path/to/config.ini  # Use custom config file
        """
    )
    parser.add_argument(
        "--config",
        type=str,
        help="Path to configuration file (default: shared_data/config.ini)"
    )
    return parser.parse_args()


def load_application_config(config_path: Path = None):
    """Loads configuration from the specified path or default shared data directory."""
    if config_path is None:
        config_path = get_shared_data_path() / "config.ini"

    try:
        with FileLock(config_path):
            if not config_path.exists():
                logger.error(
                    f"Configuration file not found at {config_path}. Please create it or run the UI first."
                )
                return None
            return load_config(config_path)
    except TimeoutError:
        logger.error(f"Could not acquire lock to read config file at {config_path}.")
        return None


async def main():
    """Main entry point for the application."""
    args = parse_arguments()
    
    # Determine config path
    config_path = None
    if args.config:
        config_path = Path(args.config)
        if not config_path.is_absolute():
            # Convert relative path to absolute
            config_path = Path.cwd() / config_path
        logger.info(f"Using custom config file: {config_path}")
    else:
        logger.info("Using default config file from shared_data directory")
    
    config = load_application_config(config_path)
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
