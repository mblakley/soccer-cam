"""Idle-aware training agent for distributed YOLO training.

Runs as a background process on any Windows machine with a GPU.
Monitors user activity and starts/stops training when the machine is idle.

Features:
- Detects idle via Windows GetLastInputInfo API
- Starts YOLO training when idle for N minutes
- Gracefully stops training when user returns (saves checkpoint)
- Resumes from checkpoint when idle again
- Reports status to a shared coordination directory

Usage:
    python -m training.distributed.idle_trainer --dataset F:/training_data/ball_dataset_640
    python -m training.distributed.idle_trainer --config idle_trainer.yaml
"""

import argparse
import ctypes
import json
import logging
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Defaults
DEFAULT_IDLE_THRESHOLD = 300  # seconds (5 min) before starting training
DEFAULT_POLL_INTERVAL = 30  # seconds between idle checks
DEFAULT_GRACE_PERIOD = 60  # seconds after user returns before killing training


def get_idle_seconds() -> float:
    """Get seconds since last user input (mouse/keyboard) on Windows."""

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_ulong)]

    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        millis = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
        return millis / 1000.0
    return 0.0


def find_latest_checkpoint(project_dir: Path, run_name: str) -> Path | None:
    """Find the latest checkpoint from a previous training run."""
    # Ultralytics saves to project/name/weights/last.pt
    last_pt = project_dir / run_name / "weights" / "last.pt"
    if last_pt.exists():
        return last_pt
    # Also check numbered runs (name2, name3, etc.)
    for d in sorted(project_dir.glob(f"{run_name}*"), reverse=True):
        last_pt = d / "weights" / "last.pt"
        if last_pt.exists():
            return last_pt
    return None


def write_status(status_dir: Path, hostname: str, status: dict):
    """Write agent status to coordination directory."""
    status_dir.mkdir(parents=True, exist_ok=True)
    status["hostname"] = hostname
    status["updated"] = datetime.now(timezone.utc).isoformat()
    status_file = status_dir / f"{hostname}.json"
    with open(status_file, "w") as f:
        json.dump(status, f, indent=2)


class IdleTrainer:
    """Monitors idle state and manages training lifecycle."""

    def __init__(
        self,
        data_yaml: str,
        model: str = "yolo11m.pt",
        project: str = "runs",
        run_name: str = "idle_train",
        batch: int = 16,
        epochs: int = 150,
        fraction: float = 1.0,
        idle_threshold: int = DEFAULT_IDLE_THRESHOLD,
        grace_period: int = DEFAULT_GRACE_PERIOD,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
        status_dir: Path | None = None,
    ):
        self.data_yaml = data_yaml
        self.model = model
        self.project = project
        self.run_name = run_name
        self.batch = batch
        self.epochs = epochs
        self.fraction = fraction
        self.idle_threshold = idle_threshold
        self.grace_period = grace_period
        self.poll_interval = poll_interval
        self.status_dir = status_dir
        self.hostname = socket.gethostname()

        self._process: subprocess.Popen | None = None
        self._running = False
        self._total_train_seconds = 0
        self._session_start: float | None = None
        self._stop_requested = False

    def _update_status(self, state: str, **extra):
        if self.status_dir:
            write_status(
                self.status_dir,
                self.hostname,
                {
                    "state": state,
                    "run_name": self.run_name,
                    "model": self.model,
                    "total_train_seconds": self._total_train_seconds,
                    **extra,
                },
            )

    def start_training(self):
        """Start or resume a YOLO training subprocess."""
        checkpoint = find_latest_checkpoint(Path(self.project), self.run_name)

        cmd = [
            sys.executable,
            "-m",
            "training.train",
            "--data",
            self.data_yaml,
            "--model",
            str(checkpoint) if checkpoint else self.model,
            "--name",
            self.run_name,
            "--batch",
            str(self.batch),
            "--epochs",
            str(self.epochs),
            "--fraction",
            str(self.fraction),
            "--project",
            self.project,
        ]

        if checkpoint:
            logger.info("Resuming from checkpoint: %s", checkpoint)
        else:
            logger.info("Starting fresh training with %s", self.model)

        # Start training in a subprocess so we can kill it cleanly
        self._process = subprocess.Popen(
            cmd,
            cwd=str(Path(__file__).resolve().parents[2]),  # project root
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        self._running = True
        self._session_start = time.time()
        self._update_status("training", pid=self._process.pid)
        logger.info("Training started (PID %d)", self._process.pid)

    def stop_training(self):
        """Gracefully stop training. YOLO saves checkpoints on interrupt."""
        if self._process and self._process.poll() is None:
            logger.info("Stopping training (sending CTRL_BREAK)...")
            # CTRL_BREAK_EVENT triggers graceful shutdown in Ultralytics
            try:
                self._process.send_signal(signal.CTRL_BREAK_EVENT)
            except (OSError, ValueError):
                self._process.terminate()

            # Wait for graceful shutdown (checkpoint save)
            try:
                self._process.wait(timeout=60)
                logger.info("Training stopped gracefully")
            except subprocess.TimeoutExpired:
                logger.warning("Training didn't stop in time, killing")
                self._process.kill()
                self._process.wait()

        if self._session_start:
            self._total_train_seconds += time.time() - self._session_start
            self._session_start = None

        self._running = False
        self._process = None
        self._update_status("idle")

    def run(self):
        """Main loop: monitor idle state and manage training."""
        logger.info(
            "Idle trainer started on %s (threshold=%ds, grace=%ds)",
            self.hostname,
            self.idle_threshold,
            self.grace_period,
        )
        self._update_status("waiting")

        try:
            while not self._stop_requested:
                idle_secs = get_idle_seconds()

                if self._running:
                    # Check if training process died
                    if self._process and self._process.poll() is not None:
                        exit_code = self._process.returncode
                        logger.info("Training process exited (code %d)", exit_code)
                        if self._session_start:
                            self._total_train_seconds += time.time() - self._session_start
                            self._session_start = None
                        self._running = False
                        self._process = None
                        if exit_code == 0:
                            logger.info("Training completed successfully!")
                            self._update_status("completed")
                            return
                        else:
                            self._update_status("error", exit_code=exit_code)
                            # Wait before retrying
                            time.sleep(60)

                    # User came back — stop after grace period
                    elif idle_secs < self.grace_period:
                        logger.info(
                            "User active (idle %.0fs < grace %ds), stopping training",
                            idle_secs,
                            self.grace_period,
                        )
                        self.stop_training()
                else:
                    # Not training — start if idle long enough
                    if idle_secs >= self.idle_threshold:
                        logger.info(
                            "Machine idle for %.0fs (threshold %ds), starting training",
                            idle_secs,
                            self.idle_threshold,
                        )
                        self.start_training()

                time.sleep(self.poll_interval)

        except KeyboardInterrupt:
            logger.info("Interrupted — stopping training")
        finally:
            if self._running:
                self.stop_training()
            self._update_status("stopped")

    def stop(self):
        """Signal the main loop to stop."""
        self._stop_requested = True


def main():
    parser = argparse.ArgumentParser(
        description="Idle-aware YOLO training agent"
    )
    parser.add_argument(
        "--data",
        default="training/configs/ball_dataset_640.yaml",
        help="Dataset YAML config",
    )
    parser.add_argument("--model", default="yolo11m.pt", help="Base model")
    parser.add_argument("--project", default="F:/training_data/runs", help="Runs directory")
    parser.add_argument("--name", default="idle_train", help="Run name")
    parser.add_argument("--batch", type=int, default=16, help="Batch size")
    parser.add_argument("--epochs", type=int, default=150)
    parser.add_argument("--fraction", type=float, default=1.0)
    parser.add_argument(
        "--idle-threshold",
        type=int,
        default=DEFAULT_IDLE_THRESHOLD,
        help="Seconds of idle before starting training",
    )
    parser.add_argument(
        "--grace-period",
        type=int,
        default=DEFAULT_GRACE_PERIOD,
        help="Seconds after user returns before stopping",
    )
    parser.add_argument(
        "--status-dir",
        type=Path,
        default=None,
        help="Shared directory for coordination status files",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    trainer = IdleTrainer(
        data_yaml=args.data,
        model=args.model,
        project=args.project,
        run_name=args.name,
        batch=args.batch,
        epochs=args.epochs,
        fraction=args.fraction,
        idle_threshold=args.idle_threshold,
        grace_period=args.grace_period,
        status_dir=args.status_dir,
    )
    trainer.run()


if __name__ == "__main__":
    main()
