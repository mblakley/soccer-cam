"""Minimal label agent — FastAPI server for remote label job control.

Runs on a labeling node. Accepts commands via HTTP to:
- Copy files to/from network shares
- Start/stop labeling jobs
- Report status
- Serve label files for download

Usage:
    python label_agent.py --port 8650
"""

import logging
import os
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse, FileResponse
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

WORK_DIR = Path(r"C:\soccer-cam-label")
app = FastAPI(title="Label Agent")


@app.get("/status")
async def status():
    """Return node status."""
    import socket
    output_games = {}
    output_dir = WORK_DIR / "output"
    if output_dir.exists():
        for d in output_dir.iterdir():
            if d.is_dir():
                output_games[d.name] = len(list(d.glob("*.txt")))

    return {
        "hostname": socket.gethostname(),
        "work_dir": str(WORK_DIR),
        "output_games": output_games,
        "total_labels": sum(output_games.values()),
    }


@app.post("/copy-labels")
async def copy_labels(dest_share: str = r"\\192.168.86.152\video\training_data\labels_640_ext"):
    """Copy all output labels to the destination share."""
    output_dir = WORK_DIR / "output"
    if not output_dir.exists():
        return {"status": "error", "message": "No output directory"}

    results = {}
    for game_dir in sorted(output_dir.iterdir()):
        if not game_dir.is_dir():
            continue
        count = len(list(game_dir.glob("*.txt")))
        if count == 0:
            continue
        dest = os.path.join(dest_share, game_dir.name)
        logger.info("Copying %s (%d files) to %s", game_dir.name, count, dest)
        r = subprocess.run(
            ["robocopy", str(game_dir), dest, "/MIR", "/MT:8"],
            capture_output=True, text=True, timeout=600,
        )
        results[game_dir.name] = {"files": count, "exit_code": r.returncode}

    return {"status": "done", "results": results}


@app.post("/run")
async def run_command(cmd: str):
    """Run an arbitrary command and return output."""
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
    return {"stdout": r.stdout, "stderr": r.stderr, "returncode": r.returncode}


@app.get("/log")
async def get_log(lines: int = 20):
    """Return tail of labeling log."""
    log_path = WORK_DIR / "labeling.log"
    if not log_path.exists():
        return {"log": "No log file"}
    with open(log_path) as f:
        all_lines = f.readlines()
    return {"log": "".join(all_lines[-lines:])}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8650)
    args = parser.parse_args()
    uvicorn.run(app, host="0.0.0.0", port=args.port)
