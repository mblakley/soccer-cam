"""Pure geometry helper for the field-keypoint pipeline.

Derives the rendered camera's lateral pan limits from the detected field
polygon. Has no model code — it operates on an already-decoded polygon.

Keypoint layout (panoramic view from the side)::

         9---8---7---6---5       <- far sideline (top of image)
        /                 \\
       /                   \\
      0---1---2---3---4          <- near sideline (bottom of image)
"""

from __future__ import annotations

import numpy as np


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
