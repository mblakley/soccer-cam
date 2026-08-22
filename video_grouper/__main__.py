#!/usr/bin/env python
import argparse
import asyncio
import os
import sys
from pathlib import Path

from video_grouper.utils.config import create_default_config, load_config
from video_grouper.utils.config_migrations import migrate_config_file
from video_grouper.utils.locking import FileLock
from video_grouper.utils.logger import get_logger, setup_logging
from video_grouper.utils.paths import get_shared_data_path
from video_grouper.video_grouper_app import VideoGrouperApp

# Configure basic logging first, will be updated with config later
setup_logging(level="INFO", app_name="video_grouper")
logger = get_logger(__name__)

# Add the parent directory to sys.path to allow absolute imports
parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, parent_dir)

# Global variable to track tasks
tasks: list[asyncio.Task[None]] = []


def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Video Grouper Application",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                           # Use default config from shared_data/config.ini
  %(prog)s --config /path/to/config.ini  # Use custom config file
        """,
    )
    parser.add_argument(
        "--config",
        type=str,
        help="Path to configuration file (default: shared_data/config.ini)",
    )
    return parser.parse_args()


def load_application_config(config_path: Path = None):
    """Loads configuration from the specified path or default shared data directory."""
    if config_path is None:
        config_path = get_shared_data_path() / "config.ini"

    try:
        with FileLock(config_path):
            if not config_path.exists():
                # Phase 2 done-criterion: a fresh shared_data must boot.
                # Stub a minimal config so the orchestrator can come up
                # with the web server bound; the dashboard at "/" will
                # then redirect the user to /setup/welcome.
                logger.info("No config at %s; writing onboarding stub.", config_path)
                return create_default_config(config_path, str(config_path.parent))
            # Bring the file up to the current schema before reading it.
            # Inside the lock, because it rewrites config.ini. Idempotent and a
            # no-op once the file is current, so this costs one parse per boot.
            # This is the only place migrations run: the NSIS installer never
            # touches config.ini and is Windows-only anyway, while Docker,
            # Linux and hand-copied configs all come through here.
            migrate_config_file(config_path)
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

    # Resolve the default path BEFORE load so we can pass the same Path
    # to VideoGrouperApp (used by the auth server's /config editor).
    if config_path is None:
        from video_grouper.utils.paths import get_shared_data_path

        config_path = get_shared_data_path() / "config.ini"

    config = load_application_config(config_path)
    if not config:
        logger.error("Failed to load configuration. Exiting.")
        return

    app = VideoGrouperApp(config, config_path=config_path)

    try:
        await app.run()
    except asyncio.CancelledError:
        logger.info("Application is shutting down.")
        # app.run() already calls shutdown() in its finally block,
        # so we only need to call it explicitly on CancelledError
        # which may bypass the finally block in run().
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
