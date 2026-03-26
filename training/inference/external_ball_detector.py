"""External ball detection using a pre-trained ONNX model.

Runs a commercial-grade ball detection model on full-resolution panoramic
frames for high-quality ball position labels.

Usage:
    # Detect on a single video segment
    python -m training.inference.external_ball_detector \
        --video "F:/training_data/temp_video/18.02.52-18.19.36[F][0@0][236858].mp4" \
        --output detections.json

    # Detect on a segment and convert to per-tile YOLO labels
    python -m training.inference.external_ball_detector \
        --video "F:/path/to/video.mp4" \
        --output detections.json \
        --labels-dir "F:/training_data/labels_640_ext/game_name" \
        --segment-name "18.02.52-18.19.36[F][0@0][236858]"
"""

import argparse
import json
import logging
import time
from pathlib import Path

import cv2
import numpy as np

# Import ultralytics first to set up CUDA DLL paths on Windows
try:
    import ultralytics  # noqa: F401
except ImportError:
    pass

import onnxruntime as ort

logger = logging.getLogger(__name__)

# Model paths
DEFAULT_MODEL = Path("F:/test/onnx_models/decrypted/balldet_fp16.onnx")

# Detection parameters — conf=0.45 based on 300-tile Sonnet spot check
# 67% precision at 0.45 vs 5% at 0.10. Matches Autocam's production threshold.
CONF_THRESHOLD = 0.45
NMS_IOU_THRESHOLD = 0.5

# Tile layout (for converting panoramic coords to tile coords)
TILE_SIZE = 640
STEP_X = 576  # (4096 - 640) / (7 - 1)
STEP_Y = 580  # (1800 - 640) / (3 - 1)
NUM_COLS = 7
NUM_ROWS = 3
PANO_W = 4096
PANO_H = 1800


def create_session(
    model_path: Path = DEFAULT_MODEL, use_gpu: bool = True
) -> ort.InferenceSession:
    """Create an ONNX inference session, trying GPU first."""
    providers = []
    if use_gpu:
        providers.append("CUDAExecutionProvider")
    providers.append("CPUExecutionProvider")

    sess = ort.InferenceSession(str(model_path), providers=providers)
    actual = sess.get_providers()
    logger.info("ONNX session using: %s", actual)
    return sess


def detect_balls(
    frame_bgr: np.ndarray,
    sess: ort.InferenceSession,
    conf_threshold: float = CONF_THRESHOLD,
    nms_iou: float = NMS_IOU_THRESHOLD,
) -> list[dict]:
    """Detect balls in a BGR frame at full resolution.

    Args:
        frame_bgr: Input frame (H x W x 3, BGR)
        sess: ONNX inference session
        conf_threshold: Minimum confidence threshold
        nms_iou: NMS IoU threshold

    Returns:
        List of detections: [{cx, cy, w, h, conf}] in original pixel coords
    """
    orig_h, orig_w = frame_bgr.shape[:2]

    # Pad to nearest multiple of 32 (required by YOLO architecture stride)
    stride = 32
    pad_h = (stride - orig_h % stride) % stride
    pad_w = (stride - orig_w % stride) % stride

    # Preprocess: BGR -> RGB, normalize to [0, 1]
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    if pad_h > 0 or pad_w > 0:
        rgb = cv2.copyMakeBorder(
            rgb, 0, pad_h, 0, pad_w, cv2.BORDER_CONSTANT, value=(0, 0, 0)
        )

    blob = (rgb.astype(np.float32) / 255.0).transpose(2, 0, 1)[np.newaxis]

    # Run inference
    outputs = sess.run(None, {"images": blob})
    det = outputs[0][0]  # (N, 6): [cx, cy, w, h, 1.0, confidence]

    # Filter by confidence (column 5)
    mask = det[:, 5] > conf_threshold
    filtered = det[mask]
    if len(filtered) == 0:
        return []

    # NMS
    boxes = np.zeros((len(filtered), 4))
    boxes[:, 0] = filtered[:, 0] - filtered[:, 2] / 2  # x1
    boxes[:, 1] = filtered[:, 1] - filtered[:, 3] / 2  # y1
    boxes[:, 2] = filtered[:, 0] + filtered[:, 2] / 2  # x2
    boxes[:, 3] = filtered[:, 1] + filtered[:, 3] / 2  # y2

    indices = cv2.dnn.NMSBoxes(
        boxes.tolist(),
        filtered[:, 5].tolist(),
        conf_threshold,
        nms_iou,
    )

    # Get mask coefficients from output[2] for surviving detections
    mask_data = outputs[2][0] if len(outputs) > 2 else None  # (N, 33)

    results = []
    for i in indices:
        row = filtered[i]
        det = {
            "cx": float(row[0]),
            "cy": float(row[1]),
            "w": float(row[2]),
            "h": float(row[3]),
            "conf": float(row[5]),
        }

        # Store mask coefficients if available (32 floats per detection)
        if mask_data is not None:
            # Map filtered index back to original index
            orig_indices = np.where(mask)[0]
            orig_idx = int(orig_indices[i])
            det["mask_coeffs"] = mask_data[orig_idx, 1:].tolist()  # skip col 0

        results.append(det)

    return results


def pano_to_tile(cx: float, cy: float, w: float, h: float) -> list[dict]:
    """Convert a panoramic detection to per-tile YOLO labels.

    A single panoramic detection may overlap multiple tiles due to the
    overlapping tile grid. Returns labels for all overlapping tiles.

    Returns list of {row, col, cx_norm, cy_norm, w_norm, h_norm}.
    """
    labels = []
    for row in range(NUM_ROWS):
        for col in range(NUM_COLS):
            # Tile origin in panoramic coords
            tile_x0 = col * STEP_X
            tile_y0 = row * STEP_Y

            # Detection center in tile coords
            tcx = cx - tile_x0
            tcy = cy - tile_y0

            # Check if detection center falls within this tile
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


def detect_video(
    video_path: Path,
    sess: ort.InferenceSession,
    frame_interval: int = 8,
    conf_threshold: float = CONF_THRESHOLD,
) -> list[dict]:
    """Run ball detection on every Nth frame of a video.

    Returns list of {frame_idx, cx, cy, w, h, conf} in panoramic coords.
    """
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        logger.error("Cannot open video: %s", video_path)
        return []

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    logger.info("Video: %d frames, processing every %d", total_frames, frame_interval)

    detections = []
    frame_idx = 0
    frames_processed = 0
    start = time.time()

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        if frame_idx % frame_interval == 0:
            dets = detect_balls(frame, sess, conf_threshold)
            for d in dets:
                d["frame_idx"] = frame_idx
                detections.append(d)

            frames_processed += 1

            if frames_processed % 100 == 0:
                elapsed = time.time() - start
                rate = frames_processed / elapsed
                det_count = len(detections)
                logger.info(
                    "%d/%d frames (%.1f f/s) | %d detections",
                    frame_idx,
                    total_frames,
                    rate,
                    det_count,
                )

        frame_idx += 1

    cap.release()
    elapsed = time.time() - start

    frames_with_dets = len(set(d["frame_idx"] for d in detections))
    logger.info(
        "DONE: %d frames processed, %d detections (%d frames with dets) in %.0fs (%.1f f/s)",
        frames_processed,
        len(detections),
        frames_with_dets,
        elapsed,
        frames_processed / elapsed if elapsed > 0 else 0,
    )

    return detections


def save_tile_labels(
    detections: list[dict],
    labels_dir: Path,
    segment_name: str,
):
    """Convert panoramic detections to per-tile YOLO label files.

    Args:
        detections: List of {frame_idx, cx, cy, w, h, conf}
        labels_dir: Output directory for YOLO label files
        segment_name: Segment prefix for filenames
    """
    labels_dir.mkdir(parents=True, exist_ok=True)

    # Group by frame
    by_frame: dict[int, list[dict]] = {}
    for d in detections:
        by_frame.setdefault(d["frame_idx"], []).append(d)

    files_written = 0
    labels_written = 0

    for frame_idx, frame_dets in sorted(by_frame.items()):
        # Convert each detection to tile labels
        tile_labels: dict[tuple[int, int], list[str]] = {}

        for det in frame_dets:
            tiles = pano_to_tile(det["cx"], det["cy"], det["w"], det["h"])
            for tl in tiles:
                key = (tl["row"], tl["col"])
                line = f"0 {tl['cx_norm']:.6f} {tl['cy_norm']:.6f} {tl['w_norm']:.6f} {tl['h_norm']:.6f}"
                tile_labels.setdefault(key, []).append(line)

        # Write label files
        for (row, col), lines in tile_labels.items():
            fname = f"{segment_name}_frame_{frame_idx:06d}_r{row}_c{col}.txt"
            with open(labels_dir / fname, "w") as f:
                for line in lines:
                    f.write(line + "\n")
            files_written += 1
            labels_written += len(lines)

    logger.info(
        "Wrote %d label files (%d labels) to %s",
        files_written,
        labels_written,
        labels_dir,
    )


def main():
    parser = argparse.ArgumentParser(description="Run external ball detection")
    parser.add_argument("--video", type=Path, required=True, help="Input video")
    parser.add_argument("--output", type=Path, default=None, help="Output JSON")
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--frame-interval", type=int, default=8)
    parser.add_argument("--conf", type=float, default=CONF_THRESHOLD)
    parser.add_argument(
        "--labels-dir",
        type=Path,
        default=None,
        help="Output dir for per-tile YOLO labels",
    )
    parser.add_argument(
        "--segment-name",
        type=str,
        default=None,
        help="Segment name prefix for label filenames",
    )
    parser.add_argument("--cpu", action="store_true", help="Force CPU inference")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    sess = create_session(args.model, use_gpu=not args.cpu)
    detections = detect_video(args.video, sess, args.frame_interval, args.conf)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w") as f:
            json.dump(detections, f)
        logger.info("Saved %d detections to %s", len(detections), args.output)

    if args.labels_dir and args.segment_name:
        save_tile_labels(detections, args.labels_dir, args.segment_name)


if __name__ == "__main__":
    main()
