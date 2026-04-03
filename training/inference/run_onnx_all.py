"""Run ONNX ball detection on all games that don't have labels yet.

Reads video segments from staging, writes per-tile YOLO labels.
Can run on any machine with access to the network share.

Usage:
    python -u run_onnx_all.py
"""
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, r"C:\soccer-cam-label")
from map_share import map_share

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(r"C:\soccer-cam-label\onnx_detection.log"),
    ],
)
logger = logging.getLogger()

SERVER = "192.168.86.152"

# Map share
try:
    map_share(f"\\\\{SERVER}\\training", f"DESKTOP-5L867J8\\training", "amy4ever")
    logger.info("Share mapped")
except Exception as e:
    logger.error("Failed to map share: %s", e)
    sys.exit(1)

# Also map the video share for ONNX model
try:
    map_share(f"\\\\{SERVER}\\video", f"DESKTOP-5L867J8\\training", "amy4ever")
    logger.info("Video share mapped")
except Exception:
    pass  # May already be mapped

# Paths
staging = Path(f"//{SERVER}/training/staging")
labels_dir = Path(f"//{SERVER}/training/labels_640_ext")
registry_path = Path(f"//{SERVER}/training/game_registry.json")
model_path = Path(f"//{SERVER}/video/test/***REDACTED***/model.onnx")

# Check model exists
if not model_path.exists():
    logger.error("ONNX model not found at %s", model_path)
    sys.exit(1)

# Load ONNX model once
import cv2
import numpy as np

try:
    import ultralytics  # noqa: for CUDA DLL paths
except ImportError:
    pass

import onnxruntime as ort

logger.info("Loading ONNX model from %s", model_path)
providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
sess = ort.InferenceSession(str(model_path), providers=providers)
actual_provider = sess.get_providers()[0]
logger.info("ONNX provider: %s", actual_provider)

# Detection parameters
CONF_THRESHOLD = 0.45
NMS_IOU_THRESHOLD = 0.5
FRAME_INTERVAL = 4  # Match tiling interval
INPUT_SIZE = 640

# Tile layout
TILE_SIZE = 640
STEP_X = 576
STEP_Y = 580
COLS = 7
ROWS = 3


def detect_balls(frame, sess, conf_threshold=CONF_THRESHOLD):
    """Run ONNX detection on a single frame."""
    h, w = frame.shape[:2]
    # Resize to model input
    img = cv2.resize(frame, (INPUT_SIZE, INPUT_SIZE))
    img = img.astype(np.float32) / 255.0
    img = np.transpose(img, (2, 0, 1))[np.newaxis]

    outputs = sess.run(None, {sess.get_inputs()[0].name: img})
    preds = outputs[0][0]  # shape: (num_dets, 6) or transposed

    if preds.shape[0] == 6:
        preds = preds.T

    dets = []
    for pred in preds:
        if len(pred) >= 5:
            conf = pred[4] if len(pred) == 5 else max(pred[4:])
            if conf < conf_threshold:
                continue
            cx, cy, bw, bh = pred[:4]
            # Convert from model coords to panoramic coords
            dets.append({
                "cx": float(cx / INPUT_SIZE * w),
                "cy": float(cy / INPUT_SIZE * h),
                "w": float(bw / INPUT_SIZE * w),
                "h": float(bh / INPUT_SIZE * h),
                "conf": float(conf),
            })
    return dets


def pano_to_tile(cx, cy, w, h):
    """Convert panoramic detection to per-tile coordinates."""
    tiles = []
    for row in range(ROWS):
        for col in range(COLS):
            tx = col * STEP_X
            ty = row * STEP_Y
            # Check if detection overlaps this tile
            if (tx <= cx <= tx + TILE_SIZE and ty <= cy <= ty + TILE_SIZE):
                tiles.append({
                    "row": row,
                    "col": col,
                    "cx_norm": (cx - tx) / TILE_SIZE,
                    "cy_norm": (cy - ty) / TILE_SIZE,
                    "w_norm": w / TILE_SIZE,
                    "h_norm": h / TILE_SIZE,
                })
    return tiles


def process_video(video_path, segment_name, game_labels_dir):
    """Process one video segment."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        logger.error("Cannot open video: %s", video_path)
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
            dets = detect_balls(frame, sess, CONF_THRESHOLD)
            for d in dets:
                d["frame_idx"] = frame_idx
                detections.append(d)
            frames_processed += 1

            if frames_processed % 200 == 0:
                elapsed = time.time() - t0
                rate = frames_processed / elapsed if elapsed > 0 else 0
                logger.info("  %d/%d frames (%.1f f/s) | %d dets",
                            frame_idx, total_frames, rate, len(detections))

        frame_idx += 1

    cap.release()
    elapsed = time.time() - t0
    rate = frames_processed / elapsed if elapsed > 0 else 0
    logger.info("  %s: %d frames, %d dets (%.1f f/s)",
                segment_name, frames_processed, len(detections), rate)

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
                for line in lines:
                    f.write(line + "\n")
            files_written += 1

    return frames_processed, files_written


# Load registry
with open(registry_path) as f:
    registry = {g["game_id"]: g for g in json.load(f)}

logger.info("Registry: %d games", len(registry))

# Process each game
start = time.time()
done = 0

for gid in sorted(registry):
    game_labels = labels_dir / gid
    if game_labels.exists() and any(game_labels.glob("*.txt")):
        logger.info("Already labeled: %s", gid)
        continue

    game_dir = staging / gid
    if not game_dir.exists() or not (game_dir / "READY").exists():
        logger.info("Not staged: %s", gid)
        continue

    videos = sorted(game_dir.glob("*.mp4"))
    if not videos:
        logger.info("No videos: %s", gid)
        continue

    game = registry.get(gid, {})
    needs_flip = game.get("needs_flip", False)
    logger.info("=== %s (%d segments, flip=%s) ===", gid, len(videos), needs_flip)

    total_frames = 0
    total_labels = 0

    for video in videos:
        seg_name = video.stem
        logger.info("  Processing %s...", seg_name)

        # Copy video locally for faster decode
        local_video = Path(r"C:\soccer-cam-label\video_cache") / video.name
        local_video.parent.mkdir(parents=True, exist_ok=True)
        if not local_video.exists():
            import shutil
            shutil.copy2(str(video), str(local_video))

        nf, nl = process_video(local_video, seg_name, game_labels)
        total_frames += nf
        total_labels += nl

        local_video.unlink(missing_ok=True)

    done += 1
    elapsed = time.time() - start
    logger.info("DONE %s: %d frames, %d label files | %d games in %.0f min",
                gid, total_frames, total_labels, done, elapsed / 60)

elapsed = time.time() - start
logger.info("=== Complete: %d games in %.0f min ===", done, elapsed / 60)
