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


def detect_field_keypoints(
    frame_bgr: np.ndarray,
    sess: ort.InferenceSession,
    score_threshold: float = 0.5,
) -> list[tuple[float | None, float | None, float]]:
    """Detect field-boundary keypoints.

    Returns a list of length 10. Each entry is ``(x, y, score)`` in input
    pixel coords; missing keypoints (below threshold) are
    ``(None, None, score)``.
    """
    orig_h, orig_w = frame_bgr.shape[:2]
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    resized = cv2.resize(rgb, (INPUT_W, INPUT_H))
    blob = (resized.astype(np.float16) / 255.0).transpose(2, 0, 1)[np.newaxis]

    kpts, scores = sess.run(None, {"input": blob})
    kpts = kpts[0]
    scores = scores[0]

    results: list[tuple[float | None, float | None, float]] = []
    for i in range(10):
        if scores[i] >= score_threshold:
            x = float(kpts[i, 0]) * (orig_w / INPUT_W)
            y = float(kpts[i, 1]) * (orig_h / INPUT_H)
            results.append((x, y, float(scores[i])))
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
