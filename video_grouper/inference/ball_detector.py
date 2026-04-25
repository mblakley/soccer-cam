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


def _tile_origins(
    frame_w: int, frame_h: int, tile_size: int, step: int
) -> list[tuple[int, int]]:
    """Return (x0, y0) origins for tiles covering the frame.

    Tiles are tile_size x tile_size with `step` pixels between origins
    (so tile_size - step pixels of overlap). The right and bottom edges
    are anchored to (frame_w - tile_size, frame_h - tile_size) so the
    full frame is covered without partial tiles.
    """
    if frame_w <= tile_size:
        xs = [0]
    else:
        xs = list(range(0, frame_w - tile_size, step))
        if not xs or xs[-1] != frame_w - tile_size:
            xs.append(frame_w - tile_size)
    if frame_h <= tile_size:
        ys = [0]
    else:
        ys = list(range(0, frame_h - tile_size, step))
        if not ys or ys[-1] != frame_h - tile_size:
            ys.append(frame_h - tile_size)
    return [(x, y) for y in ys for x in xs]


def detect_balls(
    frame_bgr: np.ndarray,
    sess: ort.InferenceSession,
    conf_threshold: float = CONF_THRESHOLD,
    nms_iou: float = NMS_IOU_THRESHOLD,
    tile_size: int = TILE_SIZE,
    tile_step: int = STEP_X,
) -> list[dict]:
    """Detect balls in a BGR panoramic frame by tiling into square windows.

    The ONNX model expects ``(1, 3, tile_size, tile_size)`` and was trained on
    tiles cut from a ``PANO_W x PANO_H`` panoramic. If the input frame is
    larger than that (cameras have shipped 4K and 8K-wide panoramics), the
    frame is first resized to the training resolution so ball pixel-size
    matches the training distribution. Detections are then mapped back to
    the original frame's pixel coords.

    Model output is the post-NMS Ultralytics format: ``(N, 6)`` rows of
    ``[x1, y1, x2, y2, conf, class]`` in tile pixel coords.

    Returns ``{cx, cy, w, h, conf}`` dicts in the input frame's pixel coords.
    """
    orig_h, orig_w = frame_bgr.shape[:2]

    # Downscale to training resolution if the input is larger.
    if orig_w > PANO_W or orig_h > PANO_H:
        scale_x = orig_w / PANO_W
        scale_y = orig_h / PANO_H
        work = cv2.resize(frame_bgr, (PANO_W, PANO_H), interpolation=cv2.INTER_AREA)
    else:
        scale_x = 1.0
        scale_y = 1.0
        work = frame_bgr

    rgb = cv2.cvtColor(work, cv2.COLOR_BGR2RGB)
    work_h, work_w = rgb.shape[:2]

    all_boxes: list[list[float]] = []
    all_scores: list[float] = []
    all_centers: list[
        tuple[float, float, float, float]
    ] = []  # (cx, cy, w, h) in original frame coords

    for x0, y0 in _tile_origins(work_w, work_h, tile_size, tile_step):
        tile = rgb[y0 : y0 + tile_size, x0 : x0 + tile_size]
        blob = (tile.astype(np.float32) / 255.0).transpose(2, 0, 1)[np.newaxis]
        outputs = sess.run(None, {"images": blob})
        det = outputs[0][0]  # (N, 6): [x1, y1, x2, y2, conf, class]

        mask = det[:, 4] > conf_threshold
        if not mask.any():
            continue

        for row in det[mask]:
            x1, y1, x2, y2, conf, _cls = row.tolist()
            # Tile-local -> work-frame coords
            wx1 = x1 + x0
            wy1 = y1 + y0
            wx2 = x2 + x0
            wy2 = y2 + y0
            # Work-frame -> original-frame coords
            ox1, oy1 = wx1 * scale_x, wy1 * scale_y
            ox2, oy2 = wx2 * scale_x, wy2 * scale_y
            cx = (ox1 + ox2) / 2
            cy = (oy1 + oy2) / 2
            w = ox2 - ox1
            h = oy2 - oy1
            all_centers.append((cx, cy, w, h))
            all_boxes.append([ox1, oy1, ox2, oy2])
            all_scores.append(conf)

    if not all_centers:
        return []

    indices = cv2.dnn.NMSBoxes(all_boxes, all_scores, conf_threshold, nms_iou)
    if indices is None or len(indices) == 0:
        return []

    results = []
    for i in np.asarray(indices).flatten():
        cx, cy, w, h = all_centers[i]
        results.append(
            {
                "cx": float(cx),
                "cy": float(cy),
                "w": float(w),
                "h": float(h),
                "conf": float(all_scores[i]),
            }
        )
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
