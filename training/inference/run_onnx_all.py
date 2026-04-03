"""Run ONNX ball detection on all games that don't have labels yet.

Uses the existing external_ball_detector module directly.
Reads video segments from staging, writes per-tile YOLO labels.

Usage:
    python -u run_onnx_all.py
"""
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, r"C:\soccer-cam-label")
from map_share import map_share

# Also need the project on the path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

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

# Map shares
try:
    map_share(f"\\\\{SERVER}\\training", f"DESKTOP-5L867J8\\training", "amy4ever")
    map_share(f"\\\\{SERVER}\\video", f"DESKTOP-5L867J8\\training", "amy4ever")
    logger.info("Shares mapped")
except Exception as e:
    logger.warning("Share mapping: %s", e)

# Paths
staging = Path(f"//{SERVER}/training/staging")
labels_dir = Path(f"//{SERVER}/training/labels_640_ext")
registry_path = Path(f"//{SERVER}/training/game_registry.json")
model_path = Path(f"//{SERVER}/video/test/***REDACTED***/model.onnx")
LOCAL_CACHE = Path(r"C:\soccer-cam-label\video_cache")
LOCAL_CACHE.mkdir(parents=True, exist_ok=True)

FRAME_INTERVAL = 4  # Match tiling interval

# Check model
if not model_path.exists():
    logger.error("ONNX model not found: %s", model_path)
    sys.exit(1)

# Import the real detector
import cv2
import numpy as np

try:
    import ultralytics  # noqa: CUDA DLL paths
except ImportError:
    pass

import onnxruntime as ort

# Create session with DirectML or CUDA
providers = ["CUDAExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider"]
sess = ort.InferenceSession(str(model_path), providers=providers)
logger.info("ONNX provider: %s", sess.get_providers()[0])

# Detection parameters from external_ball_detector
CONF_THRESHOLD = 0.45
NMS_IOU_THRESHOLD = 0.5
TILE_SIZE = 640
STEP_X = 576
STEP_Y = 580
NUM_COLS = 7
NUM_ROWS = 3


def detect_balls(frame_bgr, sess, conf_threshold=CONF_THRESHOLD):
    """Detect balls — mirrors external_ball_detector.detect_balls exactly."""
    orig_h, orig_w = frame_bgr.shape[:2]

    stride = 32
    pad_h = (stride - orig_h % stride) % stride
    pad_w = (stride - orig_w % stride) % stride

    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    if pad_h > 0 or pad_w > 0:
        rgb = cv2.copyMakeBorder(rgb, 0, pad_h, 0, pad_w, cv2.BORDER_CONSTANT, value=(0, 0, 0))

    blob = (rgb.astype(np.float32) / 255.0).transpose(2, 0, 1)[np.newaxis]

    outputs = sess.run(None, {"images": blob})
    det = outputs[0][0]  # (N, 6): [cx, cy, w, h, 1.0, confidence]

    mask = det[:, 5] > conf_threshold
    filtered = det[mask]
    if len(filtered) == 0:
        return []

    # NMS
    boxes = np.zeros((len(filtered), 4))
    boxes[:, 0] = filtered[:, 0] - filtered[:, 2] / 2
    boxes[:, 1] = filtered[:, 1] - filtered[:, 3] / 2
    boxes[:, 2] = filtered[:, 0] + filtered[:, 2] / 2
    boxes[:, 3] = filtered[:, 1] + filtered[:, 3] / 2

    indices = cv2.dnn.NMSBoxes(
        boxes.tolist(), filtered[:, 5].tolist(), conf_threshold, NMS_IOU_THRESHOLD
    )
    if len(indices) == 0:
        return []

    results = []
    for idx in indices:
        i = idx[0] if isinstance(idx, (list, np.ndarray)) else idx
        results.append({
            "cx": float(filtered[i, 0]),
            "cy": float(filtered[i, 1]),
            "w": float(filtered[i, 2]),
            "h": float(filtered[i, 3]),
            "conf": float(filtered[i, 5]),
        })
    return results


def pano_to_tile(cx, cy, w, h):
    """Convert panoramic detection to per-tile coordinates."""
    tiles = []
    for row in range(NUM_ROWS):
        for col in range(NUM_COLS):
            tx = col * STEP_X
            ty = row * STEP_Y
            if tx <= cx <= tx + TILE_SIZE and ty <= cy <= ty + TILE_SIZE:
                tiles.append({
                    "row": row, "col": col,
                    "cx_norm": (cx - tx) / TILE_SIZE,
                    "cy_norm": (cy - ty) / TILE_SIZE,
                    "w_norm": w / TILE_SIZE,
                    "h_norm": h / TILE_SIZE,
                })
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
            dets = detect_balls(frame, sess)
            for d in dets:
                d["frame_idx"] = frame_idx
                detections.append(d)
            frames_processed += 1
            if frames_processed % 100 == 0:
                elapsed = time.time() - t0
                rate = frames_processed / elapsed if elapsed > 0 else 0
                logger.info("    %d/%d (%.1f f/s) %d dets",
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
                f.write("\n".join(lines) + "\n")
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
        logger.info("  Segment: %s", seg_name)

        # Copy locally for faster decode
        local_video = LOCAL_CACHE / video.name
        if not local_video.exists():
            logger.info("  Downloading %s (%.1f GB)...", video.name, video.stat().st_size / 1e9)
            shutil.copy2(str(video), str(local_video))

        nf, nl = process_segment(local_video, seg_name, game_labels, needs_flip)
        total_frames += nf
        total_labels += nl
        local_video.unlink(missing_ok=True)

    done += 1
    elapsed = time.time() - start
    logger.info("DONE %s: %d frames, %d labels | %d games in %.0f min",
                gid, total_frames, total_labels, done, elapsed / 60)

elapsed = time.time() - start
logger.info("=== Complete: %d games in %.0f min ===", done, elapsed / 60)
