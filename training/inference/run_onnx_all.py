"""Run ONNX ball detection via shared JSON queue.

Workers claim games atomically via lock files. Queue is a JSON file
on F: listing all games and their segments — no directory scanning.

Usage:
    python -u run_onnx_all.py [WORKER_ID]

Env vars:
    QUEUE_FILE   — path to onnx_queue.json (default: //server/video/training_data/onnx_queue.json)
    LABELS_DIR   — where to write labels (default: //server/video/training_data/labels_640_ext)
    STAGING_DIR  — where staging videos are (default: //server/training/staging)
    ONNX_MODEL   — path to model.onnx
    LOCAL_CACHE  — local dir for video caching (default: C:\soccer-cam-label\video_cache)
"""

import json
import logging
import os
import shutil
import socket
import sys
import time
from pathlib import Path

WORKER_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 0
HOSTNAME = socket.gethostname()

sys.path.insert(0, r"C:\soccer-cam-label")
from map_share import map_share  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format=f"%(asctime)s [W{WORKER_ID}] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(rf"C:\soccer-cam-label\onnx_w{WORKER_ID}.log"),
    ],
)
logger = logging.getLogger()

SERVER = "192.168.86.152"

# Map shares
try:
    map_share(f"\\\\{SERVER}\\training", "DESKTOP-5L867J8\\training", "amy4ever")
    map_share(f"\\\\{SERVER}\\video", "DESKTOP-5L867J8\\training", "amy4ever")
    logger.info("Shares mapped")
except Exception as e:
    logger.warning("Share mapping: %s", e)

# Paths
queue_file = Path(
    os.environ.get("QUEUE_FILE", f"//{SERVER}/video/training_data/onnx_queue.json")
)
labels_dir = Path(os.environ.get("LABELS_DIR", f"//{SERVER}/training/labels_640_ext"))
staging_dir = Path(os.environ.get("STAGING_DIR", f"//{SERVER}/training/staging"))
model_path = Path(
    os.environ.get(
        "ONNX_MODEL", f"//{SERVER}/video/test/***REDACTED***/model.onnx"
    )
)
LOCAL_CACHE = Path(
    os.environ.get("LOCAL_CACHE", rf"C:\soccer-cam-label\video_cache_w{WORKER_ID}")
)
LOCAL_CACHE.mkdir(parents=True, exist_ok=True)

FRAME_INTERVAL = 4
IDLE_CHECK_GAMES = (
    os.environ.get("IDLE_CHECK_GAMES", "").split(",")
    if os.environ.get("IDLE_CHECK_GAMES")
    else []
)
IDLE_CHECK_INTERVAL = 60  # seconds between idle checks while paused
CONF_THRESHOLD = 0.45
NMS_IOU_THRESHOLD = 0.5
TILE_SIZE = 640
STEP_X = 576
STEP_Y = 580
NUM_COLS = 7
NUM_ROWS = 3

# Load queue
with open(queue_file) as f:
    queue = json.load(f)
logger.info("Queue: %d games", len(queue))

# Check model
if not model_path.exists():
    logger.error("ONNX model not found: %s", model_path)
    sys.exit(1)

# Load ONNX model
import cv2  # noqa: E402
import numpy as np  # noqa: E402

try:
    import ultralytics  # noqa: E402,F401  # side-effect: loads CUDA DLL paths
except ImportError:
    pass

import onnxruntime as ort  # noqa: E402

logger.info("Loading ONNX model...")
providers = ["CUDAExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider"]
sess = ort.InferenceSession(str(model_path), providers=providers)
logger.info("ONNX provider: %s", sess.get_providers()[0])


def wait_for_idle():
    """If IDLE_CHECK_GAMES is set, pause while any listed process is running."""
    if not IDLE_CHECK_GAMES:
        return
    import psutil

    while True:
        running = []
        for proc in psutil.process_iter(["name"]):
            try:
                name = proc.info["name"].lower()
                for game in IDLE_CHECK_GAMES:
                    if game.strip().lower() in name:
                        running.append(proc.info["name"])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        if not running:
            return
        logger.info(
            "Paused — game running: %s. Checking in %ds...",
            running[0],
            IDLE_CHECK_INTERVAL,
        )
        time.sleep(IDLE_CHECK_INTERVAL)


def claim_game(gid):
    """Atomically claim a game via O_CREAT|O_EXCL lock file."""
    game_labels = labels_dir / gid
    game_labels.mkdir(parents=True, exist_ok=True)
    lock_file = str(game_labels / ".lock")
    try:
        fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{HOSTNAME}:W{WORKER_ID}:{time.time():.0f}".encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


F_LABELS = Path(f"//{SERVER}/video/training_data/labels_640_ext")


def is_complete(gid):
    """Check if a game already has labels on D: or F: (dir with .txt or pending zip)."""
    for base in [labels_dir, F_LABELS]:
        # Check for pending zip (transferred but not yet extracted)
        if (base / f"{gid}_labels.zip").exists():
            return True
        game_labels = base / gid
        if not game_labels.exists():
            continue
        if (game_labels / ".lock").exists():
            return True
        try:
            if any(f.endswith(".txt") for f in os.listdir(game_labels)[:5]):
                return True
        except OSError:
            pass
    return False


def detect_balls(frame_bgr):
    """Detect balls in a BGR frame at full resolution."""
    orig_h, orig_w = frame_bgr.shape[:2]
    stride = 32
    pad_h = (stride - orig_h % stride) % stride
    pad_w = (stride - orig_w % stride) % stride

    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    if pad_h > 0 or pad_w > 0:
        rgb = cv2.copyMakeBorder(
            rgb, 0, pad_h, 0, pad_w, cv2.BORDER_CONSTANT, value=(0, 0, 0)
        )

    blob = (rgb.astype(np.float32) / 255.0).transpose(2, 0, 1)[np.newaxis]
    outputs = sess.run(None, {"images": blob})
    det = outputs[0][0]

    mask = det[:, 5] > CONF_THRESHOLD
    filtered = det[mask]
    if len(filtered) == 0:
        return []

    boxes = np.zeros((len(filtered), 4))
    boxes[:, 0] = filtered[:, 0] - filtered[:, 2] / 2
    boxes[:, 1] = filtered[:, 1] - filtered[:, 3] / 2
    boxes[:, 2] = filtered[:, 0] + filtered[:, 2] / 2
    boxes[:, 3] = filtered[:, 1] + filtered[:, 3] / 2

    indices = cv2.dnn.NMSBoxes(
        boxes.tolist(), filtered[:, 5].tolist(), CONF_THRESHOLD, NMS_IOU_THRESHOLD
    )
    if len(indices) == 0:
        return []

    results = []
    for idx in indices:
        i = idx[0] if isinstance(idx, (list, np.ndarray)) else idx
        results.append(
            {
                "cx": float(filtered[i, 0]),
                "cy": float(filtered[i, 1]),
                "w": float(filtered[i, 2]),
                "h": float(filtered[i, 3]),
                "conf": float(filtered[i, 5]),
            }
        )
    return results


def pano_to_tile(cx, cy, w, h):
    """Convert panoramic detection to per-tile coordinates."""
    tiles = []
    for row in range(NUM_ROWS):
        for col in range(NUM_COLS):
            tx = col * STEP_X
            ty = row * STEP_Y
            if tx <= cx <= tx + TILE_SIZE and ty <= cy <= ty + TILE_SIZE:
                tiles.append(
                    {
                        "row": row,
                        "col": col,
                        "cx_norm": (cx - tx) / TILE_SIZE,
                        "cy_norm": (cy - ty) / TILE_SIZE,
                        "w_norm": w / TILE_SIZE,
                        "h_norm": h / TILE_SIZE,
                    }
                )
    return tiles


def process_segment(video_path, segment_name, game_labels_dir, needs_flip=False):
    """Process one video segment."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        logger.error("Cannot open: %s", video_path)
        return 0, 0

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    detections = []
    frame_idx = 0
    frames_processed = 0
    t0 = time.time()

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if frame_idx % FRAME_INTERVAL == 0:
            if needs_flip:
                frame = cv2.flip(frame, -1)
            dets = detect_balls(frame)
            for d in dets:
                d["frame_idx"] = frame_idx
                detections.append(d)
            frames_processed += 1
            if frames_processed % 100 == 0:
                elapsed = time.time() - t0
                rate = frames_processed / elapsed if elapsed > 0 else 0
                logger.info(
                    "    %d/%d (%.1f f/s) %d dets",
                    frame_idx,
                    total_frames,
                    rate,
                    len(detections),
                )
        frame_idx += 1

    cap.release()
    elapsed = time.time() - t0
    rate = frames_processed / elapsed if elapsed > 0 else 0
    logger.info(
        "  %s: %d frames, %d dets (%.1f f/s)",
        segment_name,
        frames_processed,
        len(detections),
        rate,
    )

    # Write per-tile labels
    game_labels_dir.mkdir(parents=True, exist_ok=True)
    by_frame = {}
    for d in detections:
        by_frame.setdefault(d["frame_idx"], []).append(d)

    files_written = 0
    for fidx, frame_dets in sorted(by_frame.items()):
        tile_labels = {}
        for det in frame_dets:
            tiles = pano_to_tile(det["cx"], det["cy"], det["w"], det["h"])
            for tl in tiles:
                key = (tl["row"], tl["col"])
                line = f"0 {tl['cx_norm']:.6f} {tl['cy_norm']:.6f} {tl['w_norm']:.6f} {tl['h_norm']:.6f}"
                tile_labels.setdefault(key, []).append(line)
        for (row, col), lines in tile_labels.items():
            fname = f"{segment_name}_frame_{fidx:06d}_r{row}_c{col}.txt"
            with open(game_labels_dir / fname, "w") as f:
                f.write("\n".join(lines) + "\n")
            files_written += 1

    return frames_processed, files_written


# Resume support: check for in-progress local labels from a previous crash
LOCAL_LABELS_BASE = Path(r"C:\soccer-cam-label\labels_local")
LOCAL_LABELS_BASE.mkdir(parents=True, exist_ok=True)


def find_resumable_game():
    """Check if we have a partially-completed game from a previous run."""
    for d in LOCAL_LABELS_BASE.iterdir():
        if not d.is_dir():
            continue
        gid = d.name
        # Must have at least one .done_ marker (completed segment)
        if any(f.name.startswith(".done_") for f in d.iterdir()):
            # Find the matching queue entry
            for entry in queue:
                if entry["game_id"] == gid:
                    logger.info("Resuming interrupted game: %s", gid)
                    return gid, entry
    return None, None


# Main loop — claim from queue
start = time.time()
done = 0

while True:
    # First check for resumable games from a previous crash
    gid, game_entry = find_resumable_game()

    if gid is None:
        # Claim a new game
        for entry in queue:
            if not is_complete(entry["game_id"]):
                if claim_game(entry["game_id"]):
                    gid = entry["game_id"]
                    game_entry = entry
                    break

    if gid is None:
        logger.info("No more games to claim. Done.")
        break

    game_labels = labels_dir / gid
    local_labels = Path(rf"C:\soccer-cam-label\labels_local\{gid}")
    logger.info(
        "=== %s (%d segments, flip=%s) ===",
        gid,
        len(game_entry["segments"]),
        game_entry["needs_flip"],
    )

    try:
        local_labels.mkdir(parents=True, exist_ok=True)

        for seg_file in game_entry["segments"]:
            seg_name = seg_file.replace(".mp4", "")
            video_path = staging_dir / gid / seg_file

            # Resume support: skip segments that already have local labels
            seg_marker = local_labels / f".done_{seg_name}"
            if seg_marker.exists():
                logger.info("  Skipping %s (already done locally)", seg_name)
                continue

            wait_for_idle()
            logger.info("  Segment: %s", seg_name)

            # Cache video locally
            local_video = LOCAL_CACHE / seg_file
            if not local_video.exists():
                logger.info(
                    "  Downloading %s (%.1f GB)...",
                    seg_file,
                    video_path.stat().st_size / 1e9,
                )
                shutil.copy2(str(video_path), str(local_video))

            # Write labels locally (fast SSD writes)
            nf, nl = process_segment(
                local_video, seg_name, local_labels, game_entry["needs_flip"]
            )
            local_video.unlink(missing_ok=True)

            # Mark segment done so we can resume if killed
            seg_marker.write_text(f"{nf} frames, {nl} labels")
            logger.info("  Segment done: %d frames, %d labels", nf, nl)

        # Zip labels locally, transfer one zip file, extract on server
        import zipfile as zf_mod

        label_files = [f for f in local_labels.iterdir() if f.suffix == ".txt"]
        zip_path = local_labels.parent / f"{gid}_labels.zip"
        logger.info("  Zipping %d label files...", len(label_files))
        with zf_mod.ZipFile(zip_path, "w", zf_mod.ZIP_STORED) as zf:
            for f in label_files:
                zf.write(str(f), f"{gid}/{f.name}")
        zip_size = zip_path.stat().st_size / 1e6
        logger.info("  Transferring %.1f MB zip...", zip_size)
        remote_zip = labels_dir / f"{gid}_labels.zip"
        shutil.copy2(str(zip_path), str(remote_zip))
        # Don't extract over SMB — server-side extractor handles it
        zip_path.unlink()
        shutil.rmtree(local_labels, ignore_errors=True)
        logger.info("  Transfer complete (%.1f MB) — server will extract", zip_size)

        # Remove lock — labels prove completion
        (game_labels / ".lock").unlink(missing_ok=True)

        done += 1
        elapsed = time.time() - start
        logger.info("DONE %s | %d games in %.0f min", gid, done, elapsed / 60)

    except Exception as e:
        logger.exception("FAILED %s: %s", gid, e)
        shutil.rmtree(local_labels, ignore_errors=True)
        try:
            (game_labels / ".lock").unlink(missing_ok=True)
            if not any(game_labels.glob("*.txt")):
                shutil.rmtree(game_labels, ignore_errors=True)
        except Exception:
            pass

elapsed = time.time() - start
logger.info("=== Complete: %d games in %.0f min ===", done, elapsed / 60)
