"""Distributed labeling job for node agents.

Self-contained script that runs on a remote node to label one game's
video segments using the external ball detector. Requires only:
- onnxruntime (or onnxruntime-gpu)
- opencv-python
- numpy
- The ONNX model file

Does NOT require ultralytics, torch, or the full training codebase.

Usage (on the node):
    python label_job.py \
        --video-dir "D:/videos/09.30.2024 - vs Chili (home)" \
        --model "D:/onnx_models/model.onnx" \
        --output "D:/labels/flash__09.30.2024_vs_Chili_home" \
        --conf 0.45 \
        --frame-interval 4

    # Or receive job config from coordinator:
    python label_job.py --config job_config.json
"""

import argparse
import json
import logging
import time
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

try:
    import onnxruntime as ort
except ImportError:
    print("ERROR: onnxruntime not installed. Run: pip install onnxruntime-gpu")
    exit(1)

logger = logging.getLogger(__name__)

# ---- Detection parameters ----
CONF_THRESHOLD = 0.45
NMS_IOU_THRESHOLD = 0.5
FRAME_INTERVAL = 4

# ---- Field boundary (fisheye panoramic, 4096x1800) ----
PANO_CENTER_X = 2048.0


def field_y_far(x):
    return 310.0 + 0.0000285 * (x - PANO_CENTER_X) ** 2


def field_y_near(x):
    return 1600.0 - 0.0000220 * (x - PANO_CENTER_X) ** 2


def is_on_field(x, y, margin=50.0):
    return (field_y_far(x) - margin) <= y <= (field_y_near(x) + margin)


# ---- Tile layout ----
TILE_SIZE = 640
STEP_X = 576
STEP_Y = 580
NUM_COLS = 7
NUM_ROWS = 3


def detect_balls(frame_bgr, sess, conf_threshold=CONF_THRESHOLD):
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

    mask = det[:, 5] > conf_threshold
    filtered = det[mask]
    if len(filtered) == 0:
        return []

    boxes = np.zeros((len(filtered), 4))
    boxes[:, 0] = filtered[:, 0] - filtered[:, 2] / 2
    boxes[:, 1] = filtered[:, 1] - filtered[:, 3] / 2
    boxes[:, 2] = filtered[:, 0] + filtered[:, 2] / 2
    boxes[:, 3] = filtered[:, 1] + filtered[:, 3] / 2

    indices = cv2.dnn.NMSBoxes(
        boxes.tolist(),
        filtered[:, 5].tolist(),
        conf_threshold,
        NMS_IOU_THRESHOLD,
    )

    return [
        {
            "cx": float(filtered[i][0]),
            "cy": float(filtered[i][1]),
            "w": float(filtered[i][2]),
            "h": float(filtered[i][3]),
            "conf": float(filtered[i][5]),
        }
        for i in indices
    ]


def pano_to_tile_labels(cx, cy, w, h):
    """Convert panoramic detection to per-tile YOLO labels."""
    labels = []
    for row in range(NUM_ROWS):
        for col in range(NUM_COLS):
            tile_x0 = col * STEP_X
            tile_y0 = row * STEP_Y
            tcx = cx - tile_x0
            tcy = cy - tile_y0
            if 0 <= tcx < TILE_SIZE and 0 <= tcy < TILE_SIZE:
                labels.append(
                    {
                        "row": row,
                        "col": col,
                        "cx_norm": tcx / TILE_SIZE,
                        "cy_norm": tcy / TILE_SIZE,
                        "w_norm": w / TILE_SIZE,
                        "h_norm": h / TILE_SIZE,
                    }
                )
    return labels


def _format_frame_labels(seg_name, frame_idx, dets):
    """Convert detections to list of (filename, content) pairs for writing."""
    tile_labels = defaultdict(list)
    for det in dets:
        for tl in pano_to_tile_labels(det["cx"], det["cy"], det["w"], det["h"]):
            key = (tl["row"], tl["col"])
            line = f"0 {tl['cx_norm']:.6f} {tl['cy_norm']:.6f} {tl['w_norm']:.6f} {tl['h_norm']:.6f}"
            tile_labels[key].append(line)

    files = []
    for (row, col), lines in tile_labels.items():
        fname = f"{seg_name}_frame_{frame_idx:06d}_r{row}_c{col}.txt"
        content = "\n".join(lines) + "\n"
        files.append((fname, content))
    return files


def _writer_thread(write_queue, output_dir):
    """Background thread that writes label files from a queue."""
    files_written = 0
    while True:
        item = write_queue.get()
        if item is None:  # poison pill
            write_queue.task_done()
            break
        for fname, content in item:
            with open(output_dir / fname, "w") as f:
                f.write(content)
            files_written += 1
        write_queue.task_done()
    return files_written


def process_segment(
    video_path,
    sess,
    output_dir,
    frame_interval=FRAME_INTERVAL,
    conf_threshold=CONF_THRESHOLD,
    static_threshold=200,
):
    """Process one video segment and write per-tile YOLO labels.

    GPU detection and file I/O run in parallel: detections are queued
    for a background writer thread so GPU never waits on disk/network.
    """
    import queue
    import threading

    seg_name = video_path.stem
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        logger.error("Cannot open: %s", video_path)
        return 0

    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    logger.info("  %s (%d frames, interval=%d)", seg_name[:50], total, frame_interval)

    output_dir.mkdir(parents=True, exist_ok=True)

    # Start background writer thread — unbounded queue so GPU never blocks.
    # If network write fails, falls back to local disk for later sync.
    write_queue = queue.Queue()  # unbounded — whole segment fits in memory
    writer_count = [0]
    local_fallback_dir = None

    def writer():
        nonlocal local_fallback_dir
        while True:
            item = write_queue.get()
            if item is None:
                write_queue.task_done()
                break
            for fname, content in item:
                try:
                    with open(output_dir / fname, "w") as f:
                        f.write(content)
                except OSError:
                    # Network write failed — fall back to local disk
                    if local_fallback_dir is None:
                        from pathlib import Path

                        local_fallback_dir = (
                            Path(r"C:\soccer-cam-label\output_pending")
                            / output_dir.name
                        )
                        local_fallback_dir.mkdir(parents=True, exist_ok=True)
                        logger.warning(
                            "Network write failed, falling back to %s",
                            local_fallback_dir,
                        )
                    with open(local_fallback_dir / fname, "w") as f:
                        f.write(content)
                writer_count[0] += 1
            write_queue.task_done()

    writer_t = threading.Thread(target=writer, daemon=True)
    writer_t.start()

    fi = 0
    det_count = 0
    start = time.time()

    while True:
        ret = cap.grab()
        if not ret:
            break
        if fi % frame_interval == 0:
            ret, frame = cap.retrieve()
            if ret:
                dets = detect_balls(frame, sess, conf_threshold)
                det_count += len(dets)

                if dets:
                    # Format labels (cheap CPU work) then queue for async write
                    files = _format_frame_labels(seg_name, fi, dets)
                    write_queue.put(files)
        fi += 1

    cap.release()

    # Signal writer to finish and wait
    write_queue.put(None)
    writer_t.join()

    # Sync any local fallback files to the share
    if local_fallback_dir and local_fallback_dir.exists():
        pending = list(local_fallback_dir.glob("*.txt"))
        if pending:
            logger.info("    Syncing %d fallback files to share...", len(pending))
            import shutil

            for f in pending:
                try:
                    shutil.copy2(f, output_dir / f.name)
                    f.unlink()
                except OSError:
                    pass  # will retry next run
            remaining = list(local_fallback_dir.glob("*.txt"))
            if not remaining:
                local_fallback_dir.rmdir()
                logger.info("    Sync complete")
            else:
                logger.warning("    %d files still pending sync", len(remaining))

    elapsed = time.time() - start
    logger.info(
        "    %d detections -> %d files in %.0fs", det_count, writer_count[0], elapsed
    )
    return writer_count[0]


def run_label_job(
    video_dir,
    model_path,
    output_dir,
    conf=CONF_THRESHOLD,
    frame_interval=FRAME_INTERVAL,
    use_gpu=True,
):
    """Run labeling on all segments in a video directory."""
    providers = []
    if use_gpu:
        providers.append("CUDAExecutionProvider")
        providers.append("DmlExecutionProvider")
    providers.append("CPUExecutionProvider")

    sess = ort.InferenceSession(str(model_path), providers=providers)
    actual = sess.get_providers()
    logger.info("ONNX providers: %s", actual)

    # Search for segment videos (may be in subdirectories for tournaments)
    segments = sorted([p for p in video_dir.rglob("*.mp4") if "[F][0@0]" in p.name])
    if not segments:
        logger.error("No segments found in %s", video_dir)
        return

    logger.info("=== %s: %d segments ===", video_dir.name, len(segments))

    total_files = 0
    for seg in segments:
        total_files += process_segment(
            seg,
            sess,
            output_dir,
            frame_interval,
            conf,
            static_threshold=200 if frame_interval <= 4 else 100,
        )

    logger.info("=== DONE: %d label files ===", total_files)
    return total_files


def main():
    parser = argparse.ArgumentParser(description="Run ball detection labeling job")
    parser.add_argument(
        "--video-dir", type=Path, help="Directory with segment .mp4 files"
    )
    parser.add_argument("--model", type=Path, default=Path("model.onnx"))
    parser.add_argument("--output", type=Path, help="Output labels directory")
    parser.add_argument("--conf", type=float, default=CONF_THRESHOLD)
    parser.add_argument("--frame-interval", type=int, default=FRAME_INTERVAL)
    parser.add_argument("--cpu", action="store_true", help="Force CPU inference")
    parser.add_argument(
        "--config", type=Path, help="Job config JSON (overrides other args)"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if args.config:
        with open(args.config) as f:
            cfg = json.load(f)
        args.video_dir = Path(cfg["video_dir"])
        args.model = Path(cfg["model"])
        args.output = Path(cfg["output"])
        args.conf = cfg.get("conf", CONF_THRESHOLD)
        args.frame_interval = cfg.get("frame_interval", FRAME_INTERVAL)
        args.cpu = cfg.get("cpu", False)

    run_label_job(
        args.video_dir,
        args.model,
        args.output,
        args.conf,
        args.frame_interval,
        use_gpu=not args.cpu,
    )


if __name__ == "__main__":
    main()
