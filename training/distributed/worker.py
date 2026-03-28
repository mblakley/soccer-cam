"""Independent GPU worker — no coordinator needed.

Each machine runs this script independently. Workers coordinate via
lock files on the shared filesystem. No single point of failure.

Supports: labeling (GPU), tiling (CPU), or both.

Usage:
    uv run python -m training.distributed.worker
    uv run python -m training.distributed.worker --label-only
    uv run python -m training.distributed.worker --tile-only
    uv run python -m training.distributed.worker --idle-threshold 300
"""

import argparse
import glob as glob_mod
import logging
import os
import socket
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(f"worker-{socket.gethostname()}")

# ---- Shared paths (UNC for cross-machine access) ----
SHARE = os.environ.get("SHARE_PATH", "//192.168.86.152/video")
MODEL_PATH = f"{SHARE}/test/***REDACTED***/model.onnx"
LABELS_DIR = f"{SHARE}/training_data/labels_640_ext"
TILES_DIR = f"{SHARE}/training_data/tiles_640"
LOCKS_DIR = f"{SHARE}/training_data/worker_locks"

GAMES = {
    "flash__06.01.2024_vs_IYSA_home": f"{SHARE}/Flash_2013s/06.01.2024 - vs IYSA (home)",
    "flash__09.27.2024_vs_RNYFC_Black_home": f"{SHARE}/Flash_2013s/09.27.2024 - vs RNYFC Black (home)",
    "flash__09.30.2024_vs_Chili_home": f"{SHARE}/Flash_2013s/09.30.2024 - vs Chili (home)",
    "flash__2025.06.02": f"{SHARE}/Flash_2013s/2025.06.02-18.16.03",
    "heat__05.31.2024_vs_Fairport_home": f"{SHARE}/Heat_2012s/05.31.2024 - vs Fairport (home)",
    "heat__06.20.2024_vs_Chili_away": f"{SHARE}/Heat_2012s/06.20.2024 - vs Chili (away)",
    "heat__07.17.2024_vs_Fairport_away": f"{SHARE}/Heat_2012s/07.17.2024 - vs Fairport (away)",
    "heat__Clarence_Tournament": f"{SHARE}/Heat_2012s/07.20.2024-07.21.2024 - Clarence Tournament",
    "heat__Heat_Tournament": f"{SHARE}/Heat_2012s/06.07.2024-06.09.2024 - Heat Tournament",
}


# ---- Share mapping (for remote workers) ----

SHARE_UNC = r"\\192.168.86.152\video"


def ensure_share():
    """Map network share if not already accessible (remote workers only)."""
    import subprocess

    # Quick check — try listing the share root
    result = subprocess.run(
        ["cmd", "/c", f"dir {SHARE_UNC} >nul 2>&1"],
        capture_output=True,
        timeout=5,
    )
    if result.returncode == 0:
        return True

    share_user = os.environ.get("SHARE_USER")
    share_pass = os.environ.get("SHARE_PASS")
    if not share_user or not share_pass:
        logger.warning("Share not accessible and SHARE_USER/SHARE_PASS not set")
        return False

    subprocess.run(["net", "use", SHARE_UNC, "/delete", "/y"], capture_output=True)
    time.sleep(1)
    result = subprocess.run(
        ["net", "use", SHARE_UNC, f"/user:{share_user}", share_pass],
        capture_output=True,
        text=True,
    )
    logger.info(
        "Share map: rc=%d %s",
        result.returncode,
        result.stdout.strip() or result.stderr.strip(),
    )
    return result.returncode == 0


# ---- Lock file coordination ----


def try_lock(lock_path: Path) -> bool:
    """Atomically create a lock file. Returns True if we got the lock."""
    try:
        # os.open with O_CREAT | O_EXCL is atomic — fails if file exists
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{socket.gethostname()} {time.time():.0f}\n".encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except OSError:
        return False


def is_lock_stale(lock_path: Path, max_age_hours: float = 4.0) -> bool:
    """Check if a lock file is stale (worker crashed without cleanup)."""
    try:
        age = time.time() - lock_path.stat().st_mtime
        return age > max_age_hours * 3600
    except OSError:
        return False


def release_lock(lock_path: Path):
    """Remove a lock file."""
    try:
        lock_path.unlink()
    except OSError:
        pass


# ---- Idle detection ----


def get_idle_seconds() -> float:
    """Get user idle time in seconds (Windows only)."""
    try:
        import subprocess

        result = subprocess.run(
            ["powershell", "-Command", "(quser 2>$null | Select-String '\\d+[+:]')"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in result.stdout.strip().splitlines():
            # Parse idle time from quser output
            parts = line.strip().split()
            for part in parts:
                if "+" in part:
                    # "1+02:30" = 1 day, 2 hours, 30 min
                    return 999999
                if ":" in part and part.replace(":", "").isdigit():
                    h, m = part.split(":")
                    return int(h) * 3600 + int(m) * 60
                if part.isdigit():
                    return int(part) * 60  # bare number = minutes
    except Exception:
        pass
    return 999999  # assume idle if we can't determine


def should_work(idle_threshold: int) -> bool:
    """Check if we should accept work (user is idle enough)."""
    if idle_threshold <= 0:
        return True  # disabled
    idle = get_idle_seconds()
    if idle < idle_threshold:
        logger.debug("User active (idle %.0fs < %ds), waiting...", idle, idle_threshold)
        return False
    return True


# ---- Work discovery ----


def find_unlabeled_segments() -> list[tuple[str, str, Path]]:
    """Find all segments that need labeling. Returns (game_id, segment_stem, video_path)."""
    work = []
    for game_id, video_src in GAMES.items():
        video_dir = Path(video_src)
        if not video_dir.exists():
            continue
        label_dir = Path(LABELS_DIR) / game_id
        segments = sorted([p for p in video_dir.rglob("*.mp4") if "[F][0@0]" in p.name])
        for seg in segments:
            # Check if labels already exist
            if label_dir.exists():
                escaped = glob_mod.escape(seg.stem)
                existing = list(label_dir.glob(f"{escaped}_frame_*.txt"))
                if existing:
                    continue
            work.append((game_id, seg.stem, seg))
    return work


def find_untiled_segments() -> list[tuple[str, str, Path]]:
    """Find all segments that need tiling. Returns (game_id, segment_stem, video_path)."""
    work = []
    for game_id, video_src in GAMES.items():
        video_dir = Path(video_src)
        if not video_dir.exists():
            continue
        tile_dir = Path(TILES_DIR) / game_id
        segments = sorted([p for p in video_dir.rglob("*.mp4") if "[F][0@0]" in p.name])
        for seg in segments:
            # Check if tiles already exist (just check first tile of first frame)
            if tile_dir.exists():
                escaped = glob_mod.escape(seg.stem)
                existing = list(tile_dir.glob(f"{escaped}_frame_000000_r0_c0.jpg"))
                if existing:
                    continue
            work.append((game_id, seg.stem, seg))
    return work


# ---- Task execution ----


def do_label(game_id: str, video_path: Path):
    """Label one segment."""
    try:
        from training.distributed.label_job import process_segment
    except ImportError:
        from label_job import process_segment

    import onnxruntime as ort

    model = Path(MODEL_PATH)
    if not model.exists():
        logger.error("Model not found: %s", MODEL_PATH)
        return

    providers = [
        "CUDAExecutionProvider",
        "DmlExecutionProvider",
        "CPUExecutionProvider",
    ]
    sess = ort.InferenceSession(str(model), providers=providers)
    logger.info("ONNX providers: %s", sess.get_providers())

    output_dir = Path(LABELS_DIR) / game_id
    output_dir.mkdir(parents=True, exist_ok=True)

    n = process_segment(video_path, sess, output_dir)
    logger.info("Labeled %s/%s: %d files", game_id, video_path.stem[:40], n)


def do_tile(game_id: str, video_path: Path, frame_interval: int = 4):
    """Tile one segment."""
    import queue
    import threading

    import cv2

    try:
        from training.distributed.label_job import (
            TILE_SIZE,
            NUM_COLS,
            NUM_ROWS,
            STEP_X,
            STEP_Y,
        )
    except ImportError:
        from label_job import TILE_SIZE, NUM_COLS, NUM_ROWS, STEP_X, STEP_Y

    seg_id = video_path.stem
    tiles_dir = Path(TILES_DIR) / game_id
    tiles_dir.mkdir(parents=True, exist_ok=True)

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        logger.error("Cannot open: %s", video_path)
        return

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fi = 0
    n_tiles = 0

    # Background writer
    write_queue = queue.Queue()

    def writer():
        while True:
            item = write_queue.get()
            if item is None:
                break
            for fname, jpg_bytes in item:
                try:
                    with open(tiles_dir / fname, "wb") as f:
                        f.write(jpg_bytes)
                except OSError as e:
                    logger.warning("Write failed %s: %s", fname, e)

    writer_t = threading.Thread(target=writer, daemon=True)
    writer_t.start()

    batch = []
    FLUSH_EVERY = 50

    while True:
        ret = cap.grab()
        if not ret:
            break
        if fi % frame_interval == 0:
            ret, frame = cap.retrieve()
            if ret and frame is not None:
                frame_tiles = []
                for row in range(NUM_ROWS):
                    for col in range(NUM_COLS):
                        x0 = col * STEP_X
                        y0 = row * STEP_Y
                        tile = frame[y0 : y0 + TILE_SIZE, x0 : x0 + TILE_SIZE]
                        fname = f"{seg_id}_frame_{fi:06d}_r{row}_c{col}.jpg"
                        _, jpg = cv2.imencode(
                            ".jpg", tile, [cv2.IMWRITE_JPEG_QUALITY, 95]
                        )
                        frame_tiles.append((fname, jpg.tobytes()))
                        n_tiles += 1
                batch.extend(frame_tiles)

            if len(batch) >= FLUSH_EVERY * NUM_COLS * NUM_ROWS:
                write_queue.put(batch)
                batch = []
        fi += 1

    if batch:
        write_queue.put(batch)
    write_queue.put(None)
    writer_t.join()
    cap.release()

    logger.info(
        "Tiled %s/%s: %d tiles from %d frames",
        game_id,
        seg_id[:40],
        n_tiles,
        total_frames,
    )


# ---- Job execution dispatch ----


def execute_job(job: dict):
    """Execute a job based on its type."""
    job_type = job["type"]

    if job_type == "label":
        do_label(job["game_id"], Path(job["video_path"]))

    elif job_type == "tile":
        do_tile(job["game_id"], Path(job["video_path"]))

    elif job_type == "train":
        do_train(job)

    else:
        raise ValueError(f"Unknown job type: {job_type}")


def do_train(job: dict):
    """Run a training job."""
    import subprocess

    config = job.get("config_path", "")
    logger.info("Training with config: %s", config)
    result = subprocess.run(
        ["uv", "run", "python", "-m", "training.train", "--config", config],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Training failed: {result.stderr[:500]}")
    logger.info("Training complete")


# ---- Main loop ----


def main():
    parser = argparse.ArgumentParser(description="Independent GPU/CPU worker")
    parser.add_argument(
        "--capabilities",
        nargs="*",
        default=["gpu"],
        help="Worker capabilities (default: gpu). Jobs requiring capabilities not listed here will be skipped.",
    )
    parser.add_argument(
        "--idle-threshold",
        type=int,
        default=0,
        help="Pause if user idle < N seconds (0=disabled, 300=5min)",
    )
    parser.add_argument("--once", action="store_true", help="Process one job and exit")
    args = parser.parse_args()

    hostname = socket.gethostname()
    logger.info("Worker starting on %s (capabilities: %s)", hostname, args.capabilities)

    # Map share if needed (remote workers)
    if not ensure_share():
        logger.error("Cannot access share, exiting")
        return

    # Ensure script directory is on sys.path for standalone deploys
    import sys

    script_dir = str(Path(__file__).resolve().parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)

    try:
        from training.distributed.jobs import claim_job, complete_job, fail_job
    except ImportError:
        from jobs import claim_job, complete_job, fail_job

    idle_count = 0
    while True:
        # Check idle
        if not should_work(args.idle_threshold):
            time.sleep(30)
            continue

        # Claim next job from queue
        claimed = claim_job(capabilities=args.capabilities)

        if claimed:
            job, job_path = claimed
            idle_count = 0
            logger.info(
                "[%s] %s/%s",
                job["type"].upper(),
                job.get("game_id", ""),
                job.get("segment", job.get("config_path", ""))[:40],
            )
            try:
                execute_job(job)
                complete_job(job_path, {"hostname": hostname})
            except Exception as e:
                logger.error("Job failed: %s", e, exc_info=True)
                fail_job(job_path, str(e))
        else:
            idle_count += 1
            if idle_count == 1:
                logger.info("No jobs in queue. Polling every 30s...")
            time.sleep(30)

        if args.once:
            break


if __name__ == "__main__":
    main()
