"""Minimal label agent — FastAPI server for remote label job control.

Runs on a labeling node. Accepts commands via HTTP to:
- Report status and label counts
- Copy labels to a destination share
- View labeling log

Requires API key authentication via X-API-Key header or LABEL_AGENT_KEY env var.

Usage:
    set LABEL_AGENT_KEY=my-secret-key
    python label_agent.py --port 8650
"""

import logging
import os
import subprocess
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

WORK_DIR = Path(os.environ.get("LABEL_WORK_DIR", r"C:\soccer-cam-label"))
API_KEY = os.environ.get("LABEL_AGENT_KEY", "")

app = FastAPI(title="Label Agent")


def _check_auth(x_api_key: str = Header(default="")):
    """Validate API key if one is configured."""
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


@app.get("/status")
async def status(x_api_key: str = Header(default="")):
    """Return node status."""
    _check_auth(x_api_key)
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
async def copy_labels(
    dest_share: str,
    x_api_key: str = Header(default=""),
):
    """Copy all output labels to the destination share.

    The dest_share parameter must be provided explicitly (no default).
    """
    _check_auth(x_api_key)

    output_dir = WORK_DIR / "output"
    if not output_dir.exists():
        return {"status": "error", "message": "No output directory"}

    # Validate dest_share is a UNC path (not arbitrary local path)
    if not dest_share.startswith("\\\\"):
        return {"status": "error", "message": "dest_share must be a UNC path"}

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
            capture_output=True,
            text=True,
            timeout=600,
        )
        results[game_dir.name] = {"files": count, "exit_code": r.returncode}

    return {"status": "done", "results": results}


@app.get("/log")
async def get_log(lines: int = 20, x_api_key: str = Header(default="")):
    """Return tail of labeling log."""
    _check_auth(x_api_key)

    log_path = WORK_DIR / "labeling.log"
    if not log_path.exists():
        return {"log": "No log file"}
    with open(log_path) as f:
        all_lines = f.readlines()
    return {"log": "".join(all_lines[-min(lines, 100) :])}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8650)
    args = parser.parse_args()
    uvicorn.run(app, host="0.0.0.0", port=args.port)
