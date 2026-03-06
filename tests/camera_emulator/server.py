"""Camera emulator HTTP server.

Emulates either a Dahua or ReoLink camera based on the CAMERA_TYPE env var.
Serves real test clips from the /clips directory (mounted as a Docker volume).

Usage:
    CAMERA_TYPE=dahua USERNAME=admin PASSWORD=admin python server.py
    CAMERA_TYPE=reolink USERNAME=admin PASSWORD=admin python server.py
"""

import logging
import os
import sys

from aiohttp import web

from file_generator import generate_test_files

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def create_app(
    camera_type: str = None,
    username: str = None,
    password: str = None,
    clips_dir: str = None,
) -> web.Application:
    """Create the aiohttp application for the camera emulator."""
    camera_type = camera_type or os.environ.get("CAMERA_TYPE", "dahua")
    username = username or os.environ.get("USERNAME", "admin")
    password = password or os.environ.get("PASSWORD", "admin")
    clips_dir = clips_dir or os.environ.get("CLIPS_DIR", "/clips")

    app = web.Application()
    test_files = generate_test_files(clips_dir)

    if camera_type == "dahua":
        from dahua_handler import setup_routes

        setup_routes(app, test_files, username, password)
        logger.info("Camera emulator started in Dahua mode")
    elif camera_type == "reolink":
        from reolink_handler import setup_routes

        setup_routes(app, test_files, username, password)
        logger.info("Camera emulator started in ReoLink mode")
    else:
        raise ValueError(f"Unknown camera type: {camera_type}")

    # Store config on app for introspection
    app["camera_type"] = camera_type
    app["test_files"] = test_files

    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "80"))
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=port)
