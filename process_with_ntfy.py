#!/usr/bin/env python3
"""
Script to manually trigger NTFY processing for a specific directory.
"""

import asyncio
import argparse
import os
import sys
import logging
from pathlib import Path
from video_grouper.video_grouper_app import VideoGrouperApp
from video_grouper.utils.paths import get_shared_data_path
from video_grouper.utils.locking import FileLock
from video_grouper.utils.config import load_config
from video_grouper.utils.logger import setup_logging, get_logger

# Configure logging
setup_logging(level="INFO", app_name="video_grouper_ntfy")
logger = get_logger(__name__)


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
    # Parse command line arguments
    parser = argparse.ArgumentParser(
        description="Process a directory with NTFY integration"
    )
    parser.add_argument(
        "directory", help="Directory name to process (e.g., 2025.06.14-10.37.25)"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force processing even if match_info is already populated",
    )
    parser.add_argument(
        "--config",
        type=str,
        help="Path to configuration file (default: shared_data/config.ini)"
    )
    args = parser.parse_args()

    dir_name = args.directory
    force = args.force

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

    # Load config
    config = load_application_config(config_path)
    if not config:
        logger.error("Failed to load configuration. Exiting.")
        return 1

    # Initialize VideoGrouperApp
    app = VideoGrouperApp(config=config)

    # Initialize NTFY integration
    await app.initialize()

    # Process the directory with NTFY
    success = await app.process_combined_directory_with_ntfy(dir_name, force=force)

    if success:
        logger.info(f"Successfully started NTFY processing for {dir_name}")
    else:
        logger.error(f"Failed to start NTFY processing for {dir_name}")
        return 1

    # Wait for NTFY operations to complete
    logger.info("Waiting for NTFY operations to complete...")
    await asyncio.sleep(5)

    # Shutdown the app
    await app.shutdown()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
