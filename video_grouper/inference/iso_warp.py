"""Isotropic field-band 'dewarp' — the ball detector's input geometry.

The heatmap detector runs on a horizontal band cropped around the field (the
sky/foreground above and below the field polygon carry no ball) and isotropically
resized to ``target_width`` (round balls at a constant px-per-degree — the
cross-camera size normalization knob: a 4096 px Dahua panorama at
``target_width=7680`` shows the detector training-scale balls).

Pure numpy + cv2 (lazy) — no torch/onnxruntime, importable in every bundle.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class CropIsoWarp:
    """Crop the field band + isotropic resize to ``target_width`` (round balls)."""

    src_w: int
    src_h: int
    y_top: int
    y_bot: int
    target_width: int

    @property
    def scale(self) -> float:
        return self.target_width / self.src_w

    @property
    def final_h(self) -> int:
        return max(1, int(round((self.y_bot - self.y_top + 1) * self.scale)))

    @property
    def shape(self) -> tuple[int, int]:
        return (self.final_h, self.target_width)

    def frame(self, img: np.ndarray) -> np.ndarray:
        import cv2  # noqa: PLC0415 — lazy: keeps the module importable sans cv2

        band = img[self.y_top : self.y_bot + 1]
        return cv2.resize(
            band, (self.target_width, self.final_h), interpolation=cv2.INTER_AREA
        )

    def points(self, xy: np.ndarray) -> np.ndarray:
        """Source-pixel ``(x, y)`` -> band coords (same convention both ways)."""
        xy = np.asarray(xy, dtype=np.float64).reshape(-1, 2)
        return np.column_stack(
            [xy[:, 0] * self.scale, (xy[:, 1] - self.y_top) * self.scale]
        )


def field_band_from_polygon(polygon, margin: int = 20) -> tuple[int, int]:
    """(y_top, y_bot) of the field band from the field-outline polygon (+margin)."""
    ys = np.asarray(polygon, dtype=np.float64)[:, 1]
    return int(max(0, np.floor(ys.min()) - margin)), int(np.ceil(ys.max()) + margin)


def far_margin_polygon(polygon, far_margin: float) -> np.ndarray:
    """Push the far sideline (points 5-9) up by ``far_margin`` px so the band keeps a
    margin above the far touchline — airborne / very-far balls above the ground far
    line stay in-band (cropping at the raw far line dropped ~1/3 of very-far balls)."""
    poly = np.asarray(polygon, dtype=np.float64).copy()
    if len(poly) >= 10:
        poly[5:10, 1] = np.maximum(poly[5:10, 1] - far_margin, 0.0)
    return poly


def native_iso_warp(
    polygon, src_w: int, src_h: int, target_width: int | None = None
) -> CropIsoWarp:
    """Build the band warp for a polygon. ``target_width`` defaults to ``src_w``
    (native scale 1); lower values downscale the band isotropically — the
    speed/accuracy knob: fewer pixels (cheaper inference) but a smaller ball."""
    yt, yb = field_band_from_polygon(polygon)
    # y_bot is NOT clamped to the frame (numpy band slicing clamps implicitly) —
    # byte-parity with the training dump geometry, which trained the detector.
    return CropIsoWarp(
        int(src_w), int(src_h), int(yt), int(yb), int(target_width or src_w)
    )


def dewarp_mask_gray(frame_bgr, warp: CropIsoWarp, mask: np.ndarray) -> np.ndarray:
    """Iso-dewarp (band crop) + grayscale + apply the precomputed band mask."""
    import cv2  # noqa: PLC0415

    band = warp.frame(frame_bgr)
    gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
    gray[mask == 0] = 0
    return gray


def band_mask(warp: CropIsoWarp, polygon) -> np.ndarray:
    """Binary mask of the (far-margin-expanded) field polygon in band coords."""
    import cv2  # noqa: PLC0415

    bh, bw = warp.shape
    mask = np.zeros((bh, bw), np.uint8)
    cv2.fillPoly(mask, [warp.points(polygon).astype(np.int32)], 255)
    return mask
