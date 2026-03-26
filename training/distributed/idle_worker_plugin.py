"""Dask WorkerPlugin that pauses the worker when a user is active.

Detects user activity across ALL sessions (console, RDP) by parsing
the output of `quser` (Query User), which reports idle time per session.
Works even when the worker runs in a non-interactive service session.

When all interactive users have been idle for longer than the threshold,
the worker accepts tasks. When any user is active, the worker pauses
(finishes its current task but won't accept new ones).

Usage:
    from dask.distributed import Client
    from idle_worker_plugin import IdleOnlyWorkerPlugin

    client = Client("tcp://scheduler:8786")
    client.register_plugin(IdleOnlyWorkerPlugin(idle_threshold=120))
"""

import logging
import subprocess
import sys

from distributed.diagnostics.plugin import WorkerPlugin

logger = logging.getLogger(__name__)


def get_min_user_idle_seconds() -> float | None:
    """Return the idle time (seconds) of the LEAST idle interactive user.

    Parses `quser` output which shows idle time for all logged-in sessions.
    Returns None if no interactive users are logged in (worker should run).

    quser output format:
        USERNAME       SESSIONNAME    ID  STATE   IDLE TIME  LOGON TIME
        >jared          console         2  Active      5:32   3/7/2026 10:44 AM
         kid            rdp-tcp#1       3  Active      none   3/26/2026 8:00 AM
    """
    try:
        r = subprocess.run(["quser"], capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return None
    except Exception:
        return None

    min_idle = None
    for line in r.stdout.strip().splitlines()[1:]:  # skip header
        # Parse idle time column — formats: "none", ".", "5", "1:23", "3+05:23"
        parts = line.split()
        if len(parts) < 5:
            continue

        # Find STATE column (Active/Disc), idle is right after it
        state_idx = None
        for i, p in enumerate(parts):
            if p in ("Active", "Disc"):
                state_idx = i
                break
        if state_idx is None or state_idx + 1 >= len(parts):
            continue

        # Skip disconnected sessions
        if parts[state_idx] == "Disc":
            continue

        idle_str = parts[state_idx + 1]

        # Parse idle time to seconds
        if idle_str in ("none", "."):
            idle_secs = 0  # actively typing right now
        elif "+" in idle_str:
            # "3+05:23" = 3 days, 5 hours, 23 minutes
            days, hm = idle_str.split("+")
            h, m = hm.split(":")
            idle_secs = int(days) * 86400 + int(h) * 3600 + int(m) * 60
        elif ":" in idle_str:
            # "5:23" = 5 hours, 23 minutes
            h, m = idle_str.split(":")
            idle_secs = int(h) * 3600 + int(m) * 60
        else:
            # Plain number = minutes
            try:
                idle_secs = int(idle_str) * 60
            except ValueError:
                continue

        if min_idle is None or idle_secs < min_idle:
            min_idle = idle_secs

    return min_idle


class IdleOnlyWorkerPlugin(WorkerPlugin):
    """Pauses the worker when any interactive user is active.

    Parameters
    ----------
    idle_threshold : float
        Seconds of user inactivity before the worker is allowed to run.
        Default 120 (2 minutes).
    poll_interval : int
        Milliseconds between idle checks. Default 10000 (10 seconds).
    """

    name = "idle-only"

    def __init__(self, idle_threshold: float = 120, poll_interval: int = 10_000):
        self.idle_threshold = idle_threshold
        self.poll_interval = poll_interval
        self.worker = None
        self._pc = None

    def setup(self, worker):
        from distributed.core import Status

        self.worker = worker
        self._Status = Status

        if sys.platform != "win32":
            logger.warning("IdleOnlyWorkerPlugin only works on Windows; skipping.")
            return

        from tornado.ioloop import PeriodicCallback

        self._pc = PeriodicCallback(self._check_idle, self.poll_interval)
        self._pc.start()
        logger.info(
            "IdleOnlyWorkerPlugin active: threshold=%ss, poll=%sms",
            self.idle_threshold,
            self.poll_interval,
        )

    def teardown(self, worker):
        if self._pc is not None:
            self._pc.stop()
            self._pc = None

    def _check_idle(self):
        Status = self._Status
        min_idle = get_min_user_idle_seconds()

        # No interactive users = always run
        if min_idle is None:
            if self.worker.status == Status.paused:
                logger.info("No interactive users — resuming worker")
                self.worker.status = Status.running
            return

        current = self.worker.status

        if min_idle < self.idle_threshold and current == Status.running:
            logger.info(
                "User active (idle %ds < %ds) — pausing worker",
                min_idle,
                self.idle_threshold,
            )
            self.worker.status = Status.paused

        elif min_idle >= self.idle_threshold and current == Status.paused:
            logger.info(
                "User idle (%ds >= %ds) — resuming worker",
                min_idle,
                self.idle_threshold,
            )
            self.worker.status = Status.running
