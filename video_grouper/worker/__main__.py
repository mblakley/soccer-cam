"""``python -m video_grouper.worker`` entry point.

Polls the master configured in ``[NODE].master_url`` (or ``--master``)
for tasks, executes them, and reports back. Skips every orchestrator
processor — workers never write state.json or touch ``shared_data/``
beyond the task-local artifacts the master streams in.

v1 scaffold: registration + poll loop + task dispatch stub. Per-task
runners (combine, trim, ball_tracking) are wired in follow-ups; for
this commit the worker logs the offered task and acks it as
"completed" so the master-side flow is testable end-to-end.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import socket
import sys
from pathlib import Path

import httpx

from video_grouper.utils.config import load_config
from video_grouper.utils.logger import setup_logging_from_config, get_logger
from video_grouper.utils.paths import get_shared_data_path

logger = get_logger(__name__)


def _state_path(storage_path: str | Path) -> Path:
    return Path(storage_path) / "worker_state.json"


def _load_state(storage_path: str | Path) -> dict:
    p = _state_path(storage_path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(storage_path: str | Path, data: dict) -> None:
    p = _state_path(storage_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


async def _register(
    client: httpx.AsyncClient,
    master_url: str,
    node_id: str,
    capabilities: list[str],
) -> str:
    """Register with the master and return the bearer token."""
    resp = await client.post(
        f"{master_url}/api/work/register",
        json={
            "node_id": node_id,
            "capabilities": capabilities,
            "version": "0.1.0",
        },
        timeout=10.0,
    )
    resp.raise_for_status()
    data = resp.json()
    logger.info(
        "WORKER: registered with master as %s (capabilities=%s)",
        data["node_id"],
        capabilities,
    )
    return data["token"]


async def _process_task(task: dict) -> tuple[bool, dict | str]:
    """Run a task. Returns (success, outputs|error)."""
    task_type = task.get("task_type")
    logger.info("WORKER: processing task %s (type=%s)", task.get("task_id"), task_type)
    # v1: just log + ack. Per-task runners (combine, trim, ball_tracking)
    # land in follow-ups; the master-side flow is testable end-to-end
    # without them.
    return True, {"runner": "stub", "task_type": task_type}


async def _poll_loop(master_url: str, token: str, poll_interval: float = 5.0) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(headers=headers, timeout=15.0) as client:
        while True:
            try:
                resp = await client.get(f"{master_url}/api/work/next")
                if resp.status_code == 204:
                    await asyncio.sleep(poll_interval)
                    continue
                resp.raise_for_status()
                task = resp.json()
            except httpx.HTTPError as exc:
                logger.warning("WORKER: poll failed: %s", exc)
                await asyncio.sleep(poll_interval)
                continue

            success, result = await _process_task(task)
            task_id = task["task_id"]
            try:
                if success:
                    await client.post(
                        f"{master_url}/api/work/{task_id}/complete",
                        json={"outputs": result},
                    )
                else:
                    await client.post(
                        f"{master_url}/api/work/{task_id}/fail",
                        json={"error": str(result), "retry": True},
                    )
            except httpx.HTTPError as exc:
                logger.error("WORKER: failed to report task %s: %s", task_id, exc)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Soccer-cam remote worker")
    parser.add_argument(
        "--master",
        help="Master URL (e.g. http://master.local:8765); "
        "falls back to [NODE].master_url",
    )
    parser.add_argument(
        "--config", help="Config file path; defaults to shared_data/config.ini"
    )
    args = parser.parse_args()

    config_path = (
        Path(args.config) if args.config else get_shared_data_path() / "config.ini"
    )
    if not config_path.exists():
        logger.error(
            "Worker needs a config.ini at %s to know its node identity. "
            "Run the orchestrator's web wizard at /setup first, then set "
            "[NODE].role = 'worker' and [NODE].master_url.",
            config_path,
        )
        return 2

    config = load_config(config_path)
    setup_logging_from_config(config)

    if config.node.role != "worker":
        logger.error(
            "[NODE].role is %r; worker entry point only runs when "
            "role = 'worker'. Use the orchestrator entry point for the "
            "other roles.",
            config.node.role,
        )
        return 2

    master_url = (args.master or config.node.master_url or "").rstrip("/")
    if not master_url:
        logger.error(
            "No master URL configured. Set [NODE].master_url in config.ini "
            "or pass --master http://...:8765."
        )
        return 2

    storage_path = Path(config.storage.path)
    state = _load_state(storage_path)
    node_id = state.get("node_id") or socket.gethostname() or "worker"
    state["node_id"] = node_id

    capabilities = list(config.node.capabilities or ["combine", "trim"])

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            token = await _register(client, master_url, node_id, capabilities)
        except httpx.HTTPError as exc:
            logger.error("WORKER: registration failed: %s", exc)
            return 1

    state["master_url"] = master_url
    state["token"] = token
    _save_state(storage_path, state)

    logger.info(
        "WORKER: starting poll loop against %s (host=%s, capabilities=%s)",
        master_url,
        platform.node(),
        capabilities,
    )
    try:
        await _poll_loop(master_url, token)
    except asyncio.CancelledError:
        logger.info("WORKER: shutting down")
    return 0


def main_entry() -> None:
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main_entry()
