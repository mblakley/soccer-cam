"""Ball detection on panoramic frames using a pre-trained ONNX model.

Runtime-friendly: top-level imports limited to numpy, onnxruntime, cv2.
No torch, no ultralytics, no scipy — those live in the [ml] extra and
are not bundled into the service / tray exes.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

logger = logging.getLogger(__name__)

CONF_THRESHOLD = 0.45
NMS_IOU_THRESHOLD = 0.5

TILE_SIZE = 640
STEP_X = 576  # (PANO_W - TILE_SIZE) / (NUM_COLS - 1)
STEP_Y = 580  # (PANO_H - TILE_SIZE) / (NUM_ROWS - 1)
NUM_COLS = 7
NUM_ROWS = 3
PANO_W = 4096
PANO_H = 1800


def create_session(model_path: Path, use_gpu: bool = True) -> ort.InferenceSession:
    """Create an ONNX inference session.

    Provider order: ``[CUDAExecutionProvider, CPUExecutionProvider]`` when
    ``use_gpu`` is True (matches ``onnxruntime-gpu`` wheel), else CPU only.
    """
    providers: list[str] = []
    if use_gpu:
        providers.append("CUDAExecutionProvider")
    providers.append("CPUExecutionProvider")

    sess = ort.InferenceSession(str(model_path), providers=providers)
    logger.info("ONNX session using: %s", sess.get_providers())
    return sess


def detect_balls(
    frame_bgr: np.ndarray,
    sess: ort.InferenceSession,
    conf_threshold: float = CONF_THRESHOLD,
    nms_iou: float = NMS_IOU_THRESHOLD,
) -> list[dict]:
    """Detect balls in a BGR frame at full resolution.

    Returns a list of ``{cx, cy, w, h, conf, mask_coeffs?}`` dicts in the
    input frame's pixel coords.
    """
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
    det = outputs[0][0]  # (N, 6): [cx, cy, w, h, 1.0, confidence]

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
        nms_iou,
    )

    mask_data = outputs[2][0] if len(outputs) > 2 else None  # (N, 33)
    orig_indices = np.where(mask)[0] if mask_data is not None else None

    results = []
    for i in indices:
        row = filtered[i]
        d = {
            "cx": float(row[0]),
            "cy": float(row[1]),
            "w": float(row[2]),
            "h": float(row[3]),
            "conf": float(row[5]),
        }
        if mask_data is not None:
            orig_idx = int(orig_indices[i])
            d["mask_coeffs"] = mask_data[orig_idx, 1:].tolist()
        results.append(d)

    return results


def pano_to_tile(cx: float, cy: float, w: float, h: float) -> list[dict]:
    """Convert a panoramic detection into per-tile YOLO label rows.

    A single panoramic detection may overlap multiple tiles. Returns
    ``{row, col, cx_norm, cy_norm, w_norm, h_norm}`` for each tile whose
    interior contains the detection center.
    """
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


def detect_video(
    video_path: Path,
    sess: ort.InferenceSession,
    frame_interval: int = 8,
    conf_threshold: float = CONF_THRESHOLD,
) -> list[dict]:
    """Run ball detection on every Nth frame of a video.

    Returns a list of ``{frame_idx, cx, cy, w, h, conf, mask_coeffs?}``
    dicts in panoramic pixel coords.
    """
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        logger.error("Cannot open video: %s", video_path)
        return []

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    logger.info("Video: %d frames, processing every %d", total_frames, frame_interval)

    detections: list[dict] = []
    frame_idx = 0
    frames_processed = 0
    start = time.time()

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        if frame_idx % frame_interval == 0:
            for d in detect_balls(frame, sess, conf_threshold):
                d["frame_idx"] = frame_idx
                detections.append(d)

            frames_processed += 1

            # Yield the GIL so heartbeat threads can run.
            if frames_processed % 50 == 0:
                time.sleep(0)

            if frames_processed % 100 == 0:
                elapsed = time.time() - start
                rate = frames_processed / elapsed if elapsed > 0 else 0
                logger.info(
                    "%d/%d frames (%.1f f/s) | %d detections",
                    frame_idx,
                    total_frames,
                    rate,
                    len(detections),
                )

        frame_idx += 1

    cap.release()
    elapsed = time.time() - start
    frames_with_dets = len({d["frame_idx"] for d in detections})
    logger.info(
        "DONE: %d frames processed, %d detections (%d frames with dets) in %.0fs (%.1f f/s)",
        frames_processed,
        len(detections),
        frames_with_dets,
        elapsed,
        frames_processed / elapsed if elapsed > 0 else 0,
    )

    return detections
