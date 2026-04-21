"""Relay training — multiple machines share one checkpoint.

Server trains continuously. Faster GPUs preempt it when available.
Checkpoint (last.pt) lives on the share so any machine can resume.

How it works:
- Server runs with --role server (always training, yields to faster GPUs)
- Kids' PCs run with --role helper (train when idle, preempt server)
- When a helper starts, it writes a preempt file
- Server checks after each epoch — if preempted, saves and waits
- When helper finishes (kid needs PC), server resumes from last.pt
- All machines write to the same checkpoint dir on the share

Usage:
    # Server (always running, GTX 1060):
    uv run python -m training.distributed.train_relay --role server

    # Laptop (RTX 4070, pauses for kids):
    python train_relay.py --role helper --idle-threshold 300

    # Fortnite-OP (RTX 3060 Ti):
    python train_relay.py --role helper --idle-threshold 300
"""

import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(f"relay-{socket.gethostname()}")

# Shared paths
SHARE_UNC = r"\\192.168.86.152\video"
RELAY_DIR = Path("//192.168.86.152/video/training_data/train_relay")
PREEMPT_FILE = RELAY_DIR / "preempt.json"
ACTIVE_FILE = RELAY_DIR / "active.json"

TARGET_EPOCHS = 100
MODEL_NAME = "yolo11n.pt"


def ensure_share():
    """Map share via WNet API if needed."""
    if RELAY_DIR.exists():
        return True
    try:
        script_dir = Path(__file__).resolve().parent
        sys.path.insert(0, str(script_dir))
        from map_share import map_share

        return map_share(
            SHARE_UNC,
            os.environ.get("SHARE_USER", r"DESKTOP-5L867J8\training"),
            os.environ.get("SHARE_PASS", "amy4ever"),
        )
    except Exception as e:
        logger.error("Share mapping failed: %s", e)
        return False


def get_idle_seconds() -> float:
    """Get user idle time in seconds."""
    try:
        result = subprocess.run(
            ["powershell", "-Command", "(quser 2>$null | Select-String '\\d+[+:]')"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in result.stdout.strip().splitlines():
            parts = line.strip().split()
            for part in parts:
                if "+" in part:
                    return 999999
                if ":" in part and part.replace(":", "").isdigit():
                    h, m = part.split(":")
                    return int(h) * 3600 + int(m) * 60
                if part.isdigit():
                    return int(part) * 60
    except Exception:
        pass
    return 999999


def set_active(role: str):
    """Mark this machine as actively training."""
    try:
        ACTIVE_FILE.write_text(
            json.dumps(
                {
                    "hostname": socket.gethostname(),
                    "role": role,
                    "heartbeat": time.time(),
                    "pid": os.getpid(),
                }
            )
        )
    except OSError:
        pass


def clear_active():
    """Clear active marker."""
    try:
        if ACTIVE_FILE.exists():
            data = json.loads(ACTIVE_FILE.read_text())
            if data.get("hostname") == socket.gethostname():
                ACTIVE_FILE.unlink()
    except OSError:
        pass


def is_preempted() -> bool:
    """Check if a helper wants to take over."""
    try:
        if PREEMPT_FILE.exists():
            data = json.loads(PREEMPT_FILE.read_text())
            age = time.time() - data.get("heartbeat", 0)
            if age < 120:  # preempt is fresh
                return True
            # Stale preempt — helper died
            PREEMPT_FILE.unlink()
    except OSError:
        pass
    return False


def request_preempt():
    """Signal the server to yield after current epoch."""
    try:
        PREEMPT_FILE.write_text(
            json.dumps(
                {
                    "hostname": socket.gethostname(),
                    "heartbeat": time.time(),
                }
            )
        )
    except OSError:
        pass


def clear_preempt():
    """Remove preempt signal."""
    try:
        if PREEMPT_FILE.exists():
            PREEMPT_FILE.unlink()
    except OSError:
        pass


def is_helper_active() -> bool:
    """Check if a helper is currently training."""
    try:
        if ACTIVE_FILE.exists():
            data = json.loads(ACTIVE_FILE.read_text())
            if data.get("role") == "helper":
                age = time.time() - data.get("heartbeat", 0)
                return age < 120
    except OSError:
        pass
    return False


def get_resume_path() -> Path | None:
    """Find last.pt checkpoint."""
    last_pt = RELAY_DIR / "weights" / "last.pt"
    return last_pt if last_pt.exists() else None


def get_current_epoch() -> int:
    """Read current epoch from results.csv."""
    results = RELAY_DIR / "results.csv"
    if not results.exists():
        return 0
    try:
        lines = results.read_text().strip().splitlines()
        if len(lines) > 1:
            return int(lines[-1].split(",")[0].strip())
    except (ValueError, IndexError):
        pass
    return 0


def get_batch_size() -> int:
    """Pick batch size based on GPU."""
    try:
        import torch

        name = torch.cuda.get_device_name(0) if torch.cuda.is_available() else ""
        if "4070" in name:
            return 16
        if "3060" in name:
            return 16
    except Exception:
        pass
    return 8  # GTX 1060 default


def make_preempt_callback():
    """Create a YOLO callback that stops training when preempted."""
    should_stop = [False]

    def on_train_epoch_end(trainer):
        # Refresh heartbeat
        set_active("server")
        # Check preempt
        if is_preempted():
            logger.info("Preempted by helper! Stopping after this epoch.")
            trainer.stop = True
            should_stop[0] = True

    return on_train_epoch_end, should_stop


def do_train(role: str):
    """Run training, returns True if more epochs needed."""
    from ultralytics import YOLO

    epoch = get_current_epoch()
    if epoch >= TARGET_EPOCHS:
        return False

    resume_path = get_resume_path()
    batch = get_batch_size()

    import torch

    gpu = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu"
    logger.info(
        "%s training on %s: epoch %d/%d batch=%d",
        role,
        gpu,
        epoch,
        TARGET_EPOCHS,
        batch,
    )

    set_active(role)

    if resume_path:
        logger.info("Resuming from %s", resume_path)
        model = YOLO(str(resume_path))
        # Add preempt callback for server
        if role == "server":
            cb, _ = make_preempt_callback()
            model.add_callback("on_train_epoch_end", cb)
        model.train(resume=True)
    else:
        logger.info("Starting fresh training")
        model = YOLO(MODEL_NAME)
        data_yaml = Path(__file__).resolve().parent / "dataset.yaml"
        if role == "server":
            cb, _ = make_preempt_callback()
            model.add_callback("on_train_epoch_end", cb)
        model.train(
            data=str(data_yaml),
            epochs=TARGET_EPOCHS,
            imgsz=640,
            batch=batch,
            device=0,
            project=str(RELAY_DIR.parent),
            name=RELAY_DIR.name,
            exist_ok=True,
            patience=30,
            workers=0,
            deterministic=True,
        )

    clear_active()
    return get_current_epoch() < TARGET_EPOCHS


def run_server():
    """Server loop — always training unless preempted."""
    while True:
        # If a helper is active, wait
        if is_helper_active():
            logger.info("Helper is training, waiting...")
            time.sleep(30)
            continue

        if get_current_epoch() >= TARGET_EPOCHS:
            logger.info("Training complete! %d epochs", TARGET_EPOCHS)
            break

        # Train (will stop if preempted)
        try:
            more = do_train("server")
            if not more:
                break
        except Exception as e:
            logger.error("Training error: %s", e)
            clear_active()
            time.sleep(60)
            continue

        # If preempted, wait for helper to finish
        if is_preempted():
            logger.info("Yielding to helper, waiting...")
            while is_helper_active() or is_preempted():
                time.sleep(30)
            logger.info("Helper done, resuming...")


def run_helper(idle_threshold: int):
    """Helper loop — train when idle, yield when kids need PC."""
    while True:
        # Check idle
        if idle_threshold > 0:
            idle = get_idle_seconds()
            if idle < idle_threshold:
                clear_preempt()
                clear_active()
                time.sleep(30)
                continue

        if get_current_epoch() >= TARGET_EPOCHS:
            logger.info("Training complete!")
            break

        # Signal server to yield
        request_preempt()
        logger.info("Requested preempt, waiting for server to yield...")

        # Wait for server to stop (up to 5 min for epoch to finish)
        for _ in range(60):
            if not is_helper_active():
                active = ACTIVE_FILE.exists()
                if not active:
                    break
                try:
                    data = json.loads(ACTIVE_FILE.read_text())
                    if data.get("role") != "server":
                        break
                except (json.JSONDecodeError, OSError):
                    break
            time.sleep(5)

        clear_preempt()

        # Train
        try:
            do_train("helper")
        except KeyboardInterrupt:
            logger.info("Interrupted")
            clear_active()
            clear_preempt()
            break
        except Exception as e:
            logger.error("Training error: %s", e)
            clear_active()
            clear_preempt()
            time.sleep(60)


def main():
    parser = argparse.ArgumentParser(description="Relay training")
    parser.add_argument("--role", choices=["server", "helper"], required=True)
    parser.add_argument("--idle-threshold", type=int, default=0)
    parser.add_argument("--model", default=MODEL_NAME)
    args = parser.parse_args()

    global MODEL_NAME
    MODEL_NAME = args.model

    logger.info("Starting as %s on %s", args.role, socket.gethostname())

    if not ensure_share():
        logger.error("Cannot access share")
        return

    RELAY_DIR.mkdir(parents=True, exist_ok=True)

    if args.role == "server":
        run_server()
    else:
        run_helper(args.idle_threshold)


if __name__ == "__main__":
    main()
