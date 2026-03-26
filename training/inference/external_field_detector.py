"""External field boundary detection using keypoint model.

Detects 10 keypoints defining the soccer field boundary, creates a
polygon mask for the playing area, and filters ball detections to
only those inside the field.

Keypoint layout (panoramic view from the side):
         9---8---7---6---5       <- far sideline (top of image)
        /                 \\
       /                   \\
      0---1---2---3---4          <- near sideline (bottom of image)

Usage:
    from training.inference.external_field_detector import (
        create_field_session, detect_field_boundary, is_on_field
    )

    field_sess = create_field_session()
    boundary = detect_field_boundary(frame, field_sess)
    filtered = [d for d in detections if is_on_field(d['cx'], d['cy'], boundary)]
"""

import logging
from pathlib import Path

import cv2
import numpy as np

try:
    import ultralytics  # noqa: F401 — sets up CUDA DLL paths on Windows
except ImportError:
    pass

import onnxruntime as ort

logger = logging.getLogger(__name__)

DEFAULT_MODEL = Path("F:/test/***REDACTED***/model.onnx")
INPUT_W = 768
INPUT_H = 384


def create_field_session(
    model_path: Path = DEFAULT_MODEL, use_gpu: bool = True
) -> ort.InferenceSession:
    """Create an ONNX inference session for the field keypoint model."""
    providers = []
    if use_gpu:
        providers.append("CUDAExecutionProvider")
    providers.append("CPUExecutionProvider")
    return ort.InferenceSession(str(model_path), providers=providers)


def detect_field_keypoints(
    frame_bgr: np.ndarray,
    sess: ort.InferenceSession,
    score_threshold: float = 0.5,
) -> list[tuple[float, float, float]]:
    """Detect field boundary keypoints.

    Args:
        frame_bgr: Input frame (H x W x 3, BGR)
        sess: ONNX inference session
        score_threshold: Minimum keypoint confidence

    Returns:
        List of (x, y, score) in original pixel coordinates.
        Index corresponds to keypoint number (0-9).
        Missing keypoints (below threshold) are (None, None, 0).
    """
    orig_h, orig_w = frame_bgr.shape[:2]
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    resized = cv2.resize(rgb, (INPUT_W, INPUT_H))
    blob = (resized.astype(np.float16) / 255.0).transpose(2, 0, 1)[np.newaxis]

    kpts, scores = sess.run(None, {"input": blob})
    kpts = kpts[0]  # (10, 2)
    scores = scores[0]  # (10,)

    results = []
    for i in range(10):
        if scores[i] >= score_threshold:
            x = float(kpts[i, 0]) * (orig_w / INPUT_W)
            y = float(kpts[i, 1]) * (orig_h / INPUT_H)
            results.append((x, y, float(scores[i])))
        else:
            results.append((None, None, float(scores[i])))

    return results


def build_field_polygon(
    keypoints: list[tuple[float, float, float]],
) -> np.ndarray | None:
    """Build a polygon from detected field keypoints.

    Returns a numpy array of shape (N, 2) suitable for cv2.pointPolygonTest,
    or None if too few keypoints were detected.

    The polygon traces: 0->1->2->3->4 (near sideline) then
    5->6->7->8->9 (far sideline, reversed back left).
    """
    # Near sideline: keypoints 0-4 (left to right)
    near = [(kp[0], kp[1]) for kp in keypoints[:5] if kp[0] is not None]
    # Far sideline: keypoints 5-9 (right to left)
    far = [(kp[0], kp[1]) for kp in keypoints[5:] if kp[0] is not None]

    if len(near) < 2 or len(far) < 2:
        logger.warning(
            "Too few keypoints for field polygon: %d near, %d far", len(near), len(far)
        )
        return None

    # Build polygon: near left-to-right, then far right-to-left (already ordered 5->9 = R->L)
    polygon = near + far
    return np.array(polygon, dtype=np.float32)


def is_on_field(
    x: float, y: float, polygon: np.ndarray | None, margin: float = 50.0
) -> bool:
    """Check if a point is inside the field polygon (with margin).

    Args:
        x, y: Point in panoramic pixel coordinates
        polygon: Field boundary polygon from build_field_polygon()
        margin: Extra margin in pixels outside the polygon to still accept
            (accounts for keypoint imprecision and balls near the line)

    Returns:
        True if the point is on/near the field.
    """
    if polygon is None:
        return True  # No polygon = accept everything

    dist = cv2.pointPolygonTest(polygon.reshape(-1, 1, 2), (x, y), measureDist=True)
    return dist >= -margin


def detect_field_boundary(
    frame_bgr: np.ndarray,
    sess: ort.InferenceSession,
    score_threshold: float = 0.5,
) -> np.ndarray | None:
    """Convenience: detect keypoints and build polygon in one call.

    Returns the field polygon or None.
    """
    kpts = detect_field_keypoints(frame_bgr, sess, score_threshold)
    detected = sum(1 for kp in kpts if kp[0] is not None)
    logger.info("Field keypoints: %d/10 detected", detected)
    return build_field_polygon(kpts)


def filter_detections(
    detections: list[dict],
    polygon: np.ndarray | None,
    margin: float = 50.0,
) -> list[dict]:
    """Filter ball detections to only those on the field.

    Args:
        detections: List of {cx, cy, ...} in panoramic coordinates
        polygon: Field boundary polygon
        margin: Margin in pixels outside polygon to accept

    Returns:
        Filtered list of detections.
    """
    if polygon is None:
        return detections

    filtered = [d for d in detections if is_on_field(d["cx"], d["cy"], polygon, margin)]
    logger.info("Field filter: %d -> %d detections", len(detections), len(filtered))
    return filtered


# ---- Fisheye field boundary filter ----
# The keypoint model doesn't work well on our 180-degree fisheye.
# Instead, use polynomial curves derived from visual analysis of
# the panoramic frame geometry.
#
# The field boundary curves due to barrel distortion:
# - Far sideline: bows downward at edges (Y increases at edges)
# - Near sideline: bows upward at edges (Y decreases at edges)
#
# Polynomials fitted to panoramic coords (4096x1800):
#   Y_far(x)  = 310 + 0.0000285 * (x - 2048)^2
#   Y_near(x) = 1720 - 0.0000220 * (x - 2048)^2

PANO_CENTER_X = 2048.0


def field_y_far(x: float) -> float:
    """Far sideline Y coordinate at panoramic X (top of field)."""
    return 310.0 + 0.0000285 * (x - PANO_CENTER_X) ** 2


def field_y_near(x: float) -> float:
    """Near sideline Y coordinate at panoramic X (bottom of field).

    Tightened from Sonnet's original 1720 to 1600 to reject
    sideline balls/bags that sit just below the near touchline.
    """
    return 1600.0 - 0.0000220 * (x - PANO_CENTER_X) ** 2


def is_on_field_curved(x: float, y: float, margin: float = 50.0) -> bool:
    """Check if a point is inside the curved field boundary.

    Args:
        x, y: Point in panoramic pixel coordinates
        margin: Pixels of margin outside boundary to accept

    Returns:
        True if the point is on/near the field.
    """
    y_top = field_y_far(x) - margin
    y_bot = field_y_near(x) + margin
    return y_top <= y <= y_bot


def filter_detections_field(
    detections: list[dict],
    margin: float = 50.0,
) -> list[dict]:
    """Filter ball detections to the curved field boundary.

    Uses polynomial curves fitted to the fisheye panoramic geometry.

    Args:
        detections: List of {cx, cy, ...} in panoramic coordinates
        margin: Extra margin in pixels outside the boundary to accept

    Returns:
        Filtered list of detections.
    """
    filtered = [d for d in detections if is_on_field_curved(d["cx"], d["cy"], margin)]
    logger.info(
        "Curved field filter: %d -> %d detections (margin=%dpx)",
        len(detections),
        len(filtered),
        margin,
    )
    return filtered
