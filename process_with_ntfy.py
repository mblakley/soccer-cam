#!/usr/bin/env python3
"""
Script to manually trigger NTFY processing for a specific directory.
"""

import asyncio
import argparse
import configparser
import os
import sys
import logging
from video_grouper.video_grouper_app import VideoGrouperApp

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d - %(message)s",
    handlers=[logging.StreamHandler()],
)

logger = logging.getLogger(__name__)


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
    args = parser.parse_args()

    dir_name = args.directory
    force = args.force

    # Load config
    config = configparser.ConfigParser()
    config_path = os.path.join("shared_data", "config.ini")
    if not os.path.exists(config_path):
        logger.error(f"Config file not found: {config_path}")
        return 1

    config.read(config_path)

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
