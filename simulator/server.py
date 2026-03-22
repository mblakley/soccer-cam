"""Camera simulator entry point.

Starts the camera-specific HTTP API, shared web dashboard,
and (for Reolink) Baichuan TCP server -- all in one asyncio event loop.

Configuration via environment variables:
    CAMERA_TYPE: "reolink" or "dahua" (default: reolink)
    USERNAME: Camera login username (default: admin)
    PASSWORD: Camera login password (default: admin)
    DEVICE_NAME: Device name in API responses (default: SimCamera)
    CLIPS_DIR: Path to auto-seed clips for E2E testing (optional)
"""

import asyncio
import collections
import logging
import os
import sys

from aiohttp import web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def create_apps(
    camera_type: str = None,
    username: str = None,
    password: str = None,
    device_name: str = None,
    clips_dir: str = None,
) -> tuple:
    """Create HTTP API app, web UI app, storage, and optional Baichuan server.

    Returns (http_app, web_app, storage, baichuan_server_or_None).
    """
    camera_type = camera_type or os.environ.get("CAMERA_TYPE", "reolink")
    username = username or os.environ.get("USERNAME", "admin")
    password = password or os.environ.get("PASSWORD", "admin")
    device_name = device_name or os.environ.get("DEVICE_NAME", "SimCamera")
    clips_dir = clips_dir or os.environ.get("CLIPS_DIR", "")

    # Shared activity log (bounded deque)
    activity_log = collections.deque(maxlen=200)

    # Create shared storage manager
    from storage import StorageManager

    storage = StorageManager(camera_type=camera_type)

    # Auto-seed from clips directory (E2E test mode)
    if clips_dir and os.path.isdir(clips_dir):
        count = storage.seed_from_clips(clips_dir)
        if count:
            logger.info(f"Auto-seeded {count} recordings from {clips_dir}")

    # Create camera-specific HTTP API app
    http_app = web.Application()
    http_app["activity_log"] = activity_log

    baichuan_server = None

    if camera_type == "reolink":
        from reolink.http_api import setup_routes

        setup_routes(http_app, storage, username, password, device_name)

        from reolink.baichuan_server import BaichuanServer

        baichuan_server = BaichuanServer(storage, username, password, activity_log)
        logger.info("Reolink simulator configured (HTTP + Baichuan)")

    elif camera_type == "dahua":
        from dahua.http_api import setup_routes

        setup_routes(http_app, storage, username, password, device_name)
        logger.info("Dahua simulator configured (HTTP + Digest Auth)")

    else:
        raise ValueError(f"Unknown camera type: {camera_type}")

    # Create web UI app
    web_app = web.Application()
    web_app["activity_log"] = activity_log

    from web_ui import setup_web_ui

    setup_web_ui(web_app, storage, camera_type, username, password, baichuan_server)

    return http_app, web_app, storage, baichuan_server


async def run_servers():
    """Start all servers and run until interrupted."""
    http_app, web_app, storage, baichuan_server = create_apps()

    # Start Baichuan TCP server (Reolink only)
    if baichuan_server:
        await baichuan_server.start(host="0.0.0.0", port=9000)

    # Start HTTP API server on port 80
    http_runner = web.AppRunner(http_app)
    await http_runner.setup()
    http_site = web.TCPSite(http_runner, "0.0.0.0", 80)
    await http_site.start()
    logger.info("HTTP API server started on port 80")

    # Start Web UI server on port 8080
    web_runner = web.AppRunner(web_app)
    await web_runner.setup()
    web_site = web.TCPSite(web_runner, "0.0.0.0", 8080)
    await web_site.start()
    logger.info("Web UI server started on port 8080")

    logger.info("Camera simulator ready. Press Ctrl+C to stop.")

    # Run forever
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        if baichuan_server:
            await baichuan_server.stop()
        await http_runner.cleanup()
        await web_runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(run_servers())
    except KeyboardInterrupt:
        logger.info("Shutting down...")
