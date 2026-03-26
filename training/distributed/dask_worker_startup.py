"""Dask worker startup script for remote nodes.

Maps the network share, then starts a Dask worker connecting to the scheduler.
All output logged to worker_startup.log.

Usage on remote node:
    python dask_worker_startup.py
"""

import asyncio
import logging
import os
import subprocess
import time

SCHEDULER = os.environ.get("DASK_SCHEDULER", "tcp://192.168.86.152:8786")
SHARE_UNC = r"\\192.168.86.152\video"
SHARE_USER = os.environ.get("SHARE_USER", r"DESKTOP-5L867J8\training")
SHARE_PASS = os.environ.get("SHARE_PASS")
WORKER_NAME = os.environ.get("DASK_WORKER_NAME", "laptop-rtx4070")
WORKER_PORT = 0  # auto-assign — more resilient to restarts

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(r"C:\soccer-cam-label\worker_startup.log", mode="a"),
    ],
)
log = logging.getLogger("worker-startup")


def map_share():
    """Ensure network share is connected."""
    if not SHARE_PASS:
        log.error("SHARE_PASS env var not set — cannot map network share")
        return False
    # Disconnect only our specific share (not all mapped drives)
    subprocess.run(
        ["net", "use", SHARE_UNC, "/delete", "/y"],
        capture_output=True,
    )
    time.sleep(2)
    result = subprocess.run(
        ["net", "use", SHARE_UNC, f"/user:{SHARE_USER}", SHARE_PASS],
        capture_output=True,
        text=True,
    )
    log.info(
        "net use: rc=%d %s %s",
        result.returncode,
        result.stdout.strip(),
        result.stderr.strip(),
    )
    from pathlib import Path

    accessible = (Path(SHARE_UNC) / "Flash_2013s").exists()
    log.info("Share accessible: %s", accessible)
    return accessible


def run_worker():
    """Start the Dask worker (blocks until it exits)."""
    from distributed import Worker

    async def main():
        w = await Worker(
            SCHEDULER,
            nthreads=4,  # 1 GPU label + up to 3 CPU tiles in parallel (16GB RAM limit)
            resources={"GPU": 1},
            name=WORKER_NAME,
            host="0.0.0.0",
            port=WORKER_PORT,
            memory_limit=0,
        )
        log.info("Worker started: %s", w.address)
        await w.finished()

    asyncio.run(main())


# Auto-restart loop — if the worker crashes, wait 30s and try again
MAX_RESTARTS = 50

for attempt in range(MAX_RESTARTS):
    try:
        log.info("=== Worker attempt %d/%d ===", attempt + 1, MAX_RESTARTS)
        map_share()
        run_worker()
        log.info("Worker exited — reconnecting in 10s...")
        time.sleep(10)
    except Exception as e:
        log.error("Worker crashed: %s", e, exc_info=True)
        log.info("Restarting in 30s...")
        time.sleep(30)
else:
    log.error("Max restarts reached, giving up")
