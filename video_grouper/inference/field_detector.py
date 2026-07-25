"""Field boundary detection.

Two strategies are exposed:

1. **ONNX keypoint model** — ``create_field_session`` /
   ``detect_field_keypoints`` / ``build_field_polygon``. Detects 10
   keypoints defining the field perimeter and assembles them into a
   polygon for ``cv2.pointPolygonTest`` filtering.

2. **Fitted polynomial curves** — ``is_on_field_curved`` /
   ``filter_detections_field``. Pure math, no model — used when the
   keypoint model has not been trained for the camera geometry.

Keypoint layout (panoramic view from the side)::

         9---8---7---6---5       <- far sideline (top of image)
        /                 \\
       /                   \\
      0---1---2---3---4          <- near sideline (bottom of image)
"""

from __future__ import annotations

import logging
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

logger = logging.getLogger(__name__)

INPUT_W = 768
INPUT_H = 384


def create_field_session(
    model_path: Path, use_gpu: bool = True
) -> ort.InferenceSession:
    """Create an ONNX inference session for the field keypoint model."""
    providers: list[str] = []
    if use_gpu:
        providers.append("CUDAExecutionProvider")
    providers.append("CPUExecutionProvider")
    return ort.InferenceSession(str(model_path), providers=providers)


def _infer_keypoints(
    frame_bgr: np.ndarray, sess: ort.InferenceSession
) -> tuple[np.ndarray, np.ndarray]:
    """Raw model inference for one frame.

    Returns ``(kpts, scores)`` where ``kpts`` is ``(10, 2)`` in **source** pixel
    coords and ``scores`` is ``(10,)`` — no thresholding (callers threshold).
    """
    orig_h, orig_w = frame_bgr.shape[:2]
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    resized = cv2.resize(rgb, (INPUT_W, INPUT_H))
    blob = (resized.astype(np.float16) / 255.0).transpose(2, 0, 1)[np.newaxis]

    input_name = sess.get_inputs()[0].name
    kpts, scores = sess.run(None, {input_name: blob})
    kpts = np.asarray(kpts[0], dtype=np.float64).copy()
    scores = np.asarray(scores[0], dtype=np.float64)
    kpts[:, 0] *= orig_w / INPUT_W
    kpts[:, 1] *= orig_h / INPUT_H
    return kpts, scores


def detect_field_keypoints(
    frame_bgr: np.ndarray,
    sess: ort.InferenceSession,
    score_threshold: float = 0.5,
) -> list[tuple[float | None, float | None, float]]:
    """Detect field-boundary keypoints in one frame.

    Returns a list of length 10. Each entry is ``(x, y, score)`` in source
    pixel coords; missing keypoints (below threshold) are
    ``(None, None, score)``.
    """
    kpts, scores = _infer_keypoints(frame_bgr, sess)
    results: list[tuple[float | None, float | None, float]] = []
    for i in range(10):
        if scores[i] >= score_threshold:
            results.append((float(kpts[i, 0]), float(kpts[i, 1]), float(scores[i])))
        else:
            results.append((None, None, float(scores[i])))
    return results


def build_field_polygon(
    keypoints: list[tuple[float | None, float | None, float]],
) -> np.ndarray | None:
    """Assemble a polygon from detected keypoints.

    Traces near sideline 0→4, then far sideline 5→9 (already right→left
    by index). Returns ``None`` if too few keypoints were detected.
    """
    near = [(kp[0], kp[1]) for kp in keypoints[:5] if kp[0] is not None]
    far = [(kp[0], kp[1]) for kp in keypoints[5:] if kp[0] is not None]

    if len(near) < 2 or len(far) < 2:
        logger.warning(
            "Too few keypoints for field polygon: %d near, %d far",
            len(near),
            len(far),
        )
        return None

    polygon = near + far
    return np.array(polygon, dtype=np.float32)


def is_on_field(
    x: float, y: float, polygon: np.ndarray | None, margin: float = 50.0
) -> bool:
    """Return True if ``(x, y)`` is inside the polygon (with pixel margin)."""
    if polygon is None:
        return True
    dist = cv2.pointPolygonTest(polygon.reshape(-1, 1, 2), (x, y), measureDist=True)
    return dist >= -margin


def detect_field_boundary(
    frame_bgr: np.ndarray,
    sess: ort.InferenceSession,
    score_threshold: float = 0.5,
) -> np.ndarray | None:
    """Detect keypoints and build the field polygon in one call."""
    kpts = detect_field_keypoints(frame_bgr, sess, score_threshold)
    detected = sum(1 for kp in kpts if kp[0] is not None)
    logger.info("Field keypoints: %d/10 detected", detected)
    return build_field_polygon(kpts)


def aggregate_keypoints(
    per_frame: list[tuple[np.ndarray, np.ndarray]],
    score_threshold: float = 0.5,
    min_confident: int = 1,
    fallback_to_best: bool = True,
) -> list[tuple[float | None, float | None, float]]:
    """Aggregate keypoints across frames of a **static camera** into 10 points.

    The camera is fixed per game, so a keypoint that is occluded in some frames
    (players, foreground spectators) is visible in others. For each of the 10
    keypoints we take the **median** ``(x, y)`` over the frames where its score
    clears ``score_threshold`` (requiring at least ``min_confident`` such frames).
    A keypoint that never clears the threshold — common for the near-sideline
    foreground under heavy barrel distortion — falls back to its single
    highest-scoring frame when ``fallback_to_best`` so the polygon stays
    complete (its returned score stays below threshold, so callers can still
    tell it apart from a truly-confident point).

    Args:
        per_frame: list of ``(kpts (10,2), scores (10,))`` from
            :func:`_infer_keypoints`, one per sampled frame.
        score_threshold: per-keypoint confidence floor.
        min_confident: min frames a keypoint must clear the floor to be medianed.
        fallback_to_best: fill never-confident keypoints from their best frame.

    Returns:
        A length-10 list of ``(x, y, score)`` (``score`` = the keypoint's best
        score across frames); never-detected keypoints are ``(None, None, score)``.
    """
    out: list[tuple[float | None, float | None, float]] = []
    for k in range(10):
        confident = [
            (p[0][k, 0], p[0][k, 1]) for p in per_frame if p[1][k] >= score_threshold
        ]
        best_score = max((float(p[1][k]) for p in per_frame), default=0.0)
        if len(confident) >= max(1, min_confident):
            arr = np.asarray(confident, dtype=np.float64)
            out.append(
                (float(np.median(arr[:, 0])), float(np.median(arr[:, 1])), best_score)
            )
        elif fallback_to_best and per_frame:
            bi = int(np.argmax([p[1][k] for p in per_frame]))
            out.append(
                (
                    float(per_frame[bi][0][k, 0]),
                    float(per_frame[bi][0][k, 1]),
                    best_score,
                )
            )
        else:
            out.append((None, None, best_score))
    return out


def detect_field_boundary_multiframe(
    frames_bgr: list[np.ndarray],
    sess: ort.InferenceSession,
    score_threshold: float = 0.5,
    min_confident: int = 1,
    fallback_to_best: bool = True,
) -> tuple[np.ndarray | None, list[tuple[float | None, float | None, float]]]:
    """Static-camera field polygon from several frames (robust to occlusion).

    Runs the model on each frame, aggregates per keypoint (see
    :func:`aggregate_keypoints`), and builds the polygon. Returns
    ``(polygon, aggregated_keypoints)`` — far more likely to yield a complete
    10-point polygon than any single frame. Callers use the per-keypoint scores
    to gate (e.g. require N truly-confident points before trusting the polygon).
    """
    per_frame = [_infer_keypoints(f, sess) for f in frames_bgr]
    agg = aggregate_keypoints(
        per_frame, score_threshold, min_confident, fallback_to_best
    )
    return build_field_polygon(agg), agg


def filter_detections(
    detections: list[dict],
    polygon: np.ndarray | None,
    margin: float = 50.0,
) -> list[dict]:
    """Filter ball detections to only those on the field."""
    if polygon is None:
        return detections
    filtered = [d for d in detections if is_on_field(d["cx"], d["cy"], polygon, margin)]
    logger.info("Field filter: %d -> %d detections", len(detections), len(filtered))
    return filtered


# ---- Fitted polynomial fallback for fisheye cameras ----
# The keypoint model doesn't generalize well to 180-degree fisheye output.
# These polynomials were fitted to the panoramic geometry by hand.
PANO_CENTER_X = 2048.0


def field_y_far(x: float) -> float:
    """Far sideline Y coordinate at panoramic X (top of field)."""
    return 310.0 + 0.0000285 * (x - PANO_CENTER_X) ** 2


def field_y_near(x: float) -> float:
    """Near sideline Y coordinate at panoramic X (bottom of field)."""
    return 1600.0 - 0.0000220 * (x - PANO_CENTER_X) ** 2


def is_on_field_curved(x: float, y: float, margin: float = 50.0) -> bool:
    """Check if ``(x, y)`` is inside the curved field boundary."""
    y_top = field_y_far(x) - margin
    y_bot = field_y_near(x) + margin
    return y_top <= y <= y_bot


def filter_detections_field(
    detections: list[dict],
    margin: float = 50.0,
) -> list[dict]:
    """Filter ball detections to the curved field boundary."""
    filtered = [d for d in detections if is_on_field_curved(d["cx"], d["cy"], margin)]
    logger.info(
        "Curved field filter: %d -> %d detections (margin=%dpx)",
        len(detections),
        len(filtered),
        margin,
    )
    return filtered
