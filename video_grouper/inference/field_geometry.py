"""Pure geometry helpers for the field-keypoint pipeline.

Lifted from :mod:`field_detector` so the runtime stage (``FieldMaskStage``)
and the training task (``training.tasks.field_boundary``) can both consume
them without dragging the inference-session machinery along. This module
has no model code — it operates on already-decoded keypoints.

Keypoint layout (panoramic view from the side)::

         9---8---7---6---5       <- far sideline (top of image)
        /                 \\
       /                   \\
      0---1---2---3---4          <- near sideline (bottom of image)
"""

from __future__ import annotations

import logging

import cv2
import numpy as np

logger = logging.getLogger(__name__)


# Field positions corresponding to each of the 10 keypoints, normalized
# into [0, 1]^2 (x = lateral, y = camera-near to camera-far). Used to
# build the homography from pixel coords to field coords.
FIELD_KEYPOINT_FIELD_COORDS = np.array(
    [
        [0.00, 0.0],  # 0 near-left
        [0.25, 0.0],  # 1
        [0.50, 0.0],  # 2
        [0.75, 0.0],  # 3
        [1.00, 0.0],  # 4 near-right
        [1.00, 1.0],  # 5 far-right
        [0.75, 1.0],  # 6
        [0.50, 1.0],  # 7
        [0.25, 1.0],  # 8
        [0.00, 1.0],  # 9 far-left
    ],
    dtype=np.float32,
)


def build_field_polygon(
    keypoints: list[tuple[float | None, float | None, float]],
) -> np.ndarray | None:
    """Trace the field polygon: near sideline 0→4, then far sideline 5→9.

    Returns ``None`` if too few keypoints were detected on either sideline
    to define a useful polygon.
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


def field_homography(
    keypoints: list[tuple[float | None, float | None, float]],
) -> np.ndarray | None:
    """Find homography mapping panoramic pixel coords → normalized field coords.

    Field coords are normalized to ``[0, 1]^2`` per
    :data:`FIELD_KEYPOINT_FIELD_COORDS`. Needs at least 4 detected
    keypoints. Uses cv2.findHomography with the default least-squares
    method (RANSAC overkill for 4-10 known correspondences).

    Returns the 3x3 homography matrix, or ``None`` if insufficient
    keypoints were detected.
    """
    src_pts: list[list[float]] = []
    dst_pts: list[list[float]] = []
    for i, kp in enumerate(keypoints):
        x, y, _score = kp
        if x is None or y is None:
            continue
        src_pts.append([float(x), float(y)])
        dst_pts.append(FIELD_KEYPOINT_FIELD_COORDS[i].tolist())

    if len(src_pts) < 4:
        logger.warning("field_homography: %d keypoints detected, need ≥4", len(src_pts))
        return None

    src = np.array(src_pts, dtype=np.float32)
    dst = np.array(dst_pts, dtype=np.float32)
    homography, _mask = cv2.findHomography(src, dst)
    return homography


def pixel_to_field(px: float, py: float, homography: np.ndarray) -> tuple[float, float]:
    """Apply homography to a single pixel; returns ``(field_x, field_y)`` in [0, 1]."""
    pt = np.array([[[float(px), float(py)]]], dtype=np.float32)
    out = cv2.perspectiveTransform(pt, homography)
    return float(out[0, 0, 0]), float(out[0, 0, 1])


def field_lateral_yaw_extent(
    polygon: np.ndarray | None, src_w: int, src_hfov_deg: float
) -> tuple[float, float]:
    """Min and max yaw (deg) covered by the polygon's lateral pixel extent.

    Used downstream to clamp the rendered camera's pan to the field. If
    no polygon is available, returns the full ``[-hfov/2, +hfov/2]`` range
    (no constraint).
    """
    if polygon is None or len(polygon) == 0:
        return -src_hfov_deg / 2.0, src_hfov_deg / 2.0
    xs = polygon[:, 0]
    yaw_min = float((xs.min() / src_w - 0.5) * src_hfov_deg)
    yaw_max = float((xs.max() / src_w - 0.5) * src_hfov_deg)
    return yaw_min, yaw_max
