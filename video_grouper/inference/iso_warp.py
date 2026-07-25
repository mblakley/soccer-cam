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


def expand_polygon(polygon, margin: float) -> np.ndarray:
    """Expand a polygon OUTWARD from its centroid by ``margin`` px — a tolerance
    band around ALL boundaries (end lines behind the goals, the near touchline,
    and extra dome above the far line).

    Balls leaving the field-of-play region — a corner popped behind the goal, a
    goal kick arcing out the top — are OUTSIDE the raw field outline and get
    masked out of detection, so the tracker loses them and the OOB/aerial physics
    can never engage (verified 2026-07-10 on held-out Spencerport). Widening the
    detection mask keeps them detectable through the exit + re-entry. ``far_margin``
    only lifts the far touchline; this adds the missing end-line + dome margin.
    """
    p = np.asarray(polygon, dtype=np.float64)
    if margin <= 0 or len(p) < 3:
        return p
    c = p.mean(axis=0)
    v = p - c
    n = np.linalg.norm(v, axis=1, keepdims=True)
    return c + v * (1.0 + margin / np.maximum(n, 1e-6))


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


class BandStabilizer:
    """Per-frame translation alignment of the dewarped band against an anchor frame.

    Wind sways the camera mast: measured on the windy 2026-06-06 games, cumulative
    excursions vs the polygon's reference geometry reach 5-60 px and put ~21% of
    ball positions outside the static field mask (vs ~4% calm baseline;
    EXP-DIST-57) — and masked pixels are zeroed before detection, so a displaced
    ball is undetectable by construction. This estimates each band frame's global
    (dx, dy) vs the ANCHOR (the first frame it sees — game/segment start, the
    polygon-fit era) by median phase-correlation over three patches (left/center/
    right — the median rejects local motion like players and one bad patch), then
    warps the content back onto the anchor so the mask, in-field gates, and
    expected-size geometry stay registered. Frame-to-frame jitter inside a
    3-frame detector stack is removed as a side effect (the diff-encoding win).

    Translation-only by design: cumulative excursions measured uniform across
    L/C/R (translation-dominant); gust-second roll (L/R >> C) remains a residual.

    ``last`` is the measured (dx, dy) of the CURRENT frame's content relative to
    the anchor, in band px. A source point on the raw frame maps into the aligned
    band as ``warp.points(p) - last``; aligned-band detections map back to raw
    band coords as ``+ last``.

    Estimate on the UNMASKED band: the static mask edges would anchor the
    correlation at zero shift.
    """

    def __init__(
        self,
        downscale: int = 2,
        max_shift: float = 150.0,
        min_response: float = 0.03,
    ):
        self.downscale = int(downscale)
        self.max_shift = float(max_shift)
        self.min_response = float(min_response)
        self.last: tuple[float, float] = (0.0, 0.0)
        self._anchor: list[np.ndarray] | None = None
        self._win: list[np.ndarray] | None = None
        self._boxes: list[tuple[int, int, int, int]] | None = None

    def reset(self) -> None:
        """Drop the anchor — the next frame becomes the new reference."""
        self._anchor = None
        self._win = None
        self._boxes = None
        self.last = (0.0, 0.0)

    def _patches(self, gray: np.ndarray) -> list[np.ndarray]:
        import cv2  # noqa: PLC0415

        h, w = gray.shape
        if self._boxes is None:
            pw = max(32, w // 6)
            self._boxes = [
                (max(0, int(cx * w) - pw // 2), min(w, int(cx * w) + pw // 2), 0, h)
                for cx in (0.15, 0.5, 0.85)
            ]
        d = self.downscale
        out = []
        for x0, x1, y0, y1 in self._boxes:
            p = gray[y0:y1, x0:x1]
            out.append(
                cv2.resize(
                    p.astype(np.float32),
                    (max(8, (x1 - x0) // d), max(8, (y1 - y0) // d)),
                    interpolation=cv2.INTER_AREA,
                )
            )
        return out

    def set_anchor(self, band_gray: np.ndarray) -> None:
        import cv2  # noqa: PLC0415

        self._anchor = self._patches(band_gray)
        self._win = [
            cv2.createHanningWindow((p.shape[1], p.shape[0]), cv2.CV_32F)
            for p in self._anchor
        ]
        self.last = (0.0, 0.0)

    def estimate(self, band_gray: np.ndarray) -> tuple[float, float]:
        """(dx, dy) of ``band_gray``'s content relative to the anchor, band px.
        First call adopts the frame as the anchor and returns (0, 0)."""
        import cv2  # noqa: PLC0415

        if self._anchor is None:
            self.set_anchor(band_gray)
            return self.last
        assert self._win is not None
        shifts = []
        for ref, cur, win in zip(
            self._anchor, self._patches(band_gray), self._win, strict=False
        ):
            (dx, dy), resp = cv2.phaseCorrelate(ref, cur, win)
            if resp >= self.min_response:
                shifts.append((dx * self.downscale, dy * self.downscale))
        if shifts:
            dx = float(np.median([s[0] for s in shifts]))
            dy = float(np.median([s[1] for s in shifts]))
            if abs(dx) <= self.max_shift and abs(dy) <= self.max_shift:
                self.last = (dx, dy)
        # no usable patch / implausible jump: keep the previous shift (temporal
        # smoothness beats snapping back to zero mid-gust)
        return self.last

    def align(self, band_gray: np.ndarray) -> np.ndarray:
        """Estimate the shift and warp the content back onto the anchor."""
        import cv2  # noqa: PLC0415

        dx, dy = self.estimate(band_gray)
        if abs(dx) < 0.05 and abs(dy) < 0.05:
            return band_gray
        m = np.array([[1.0, 0.0, -dx], [0.0, 1.0, -dy]], dtype=np.float32)
        return cv2.warpAffine(
            band_gray,
            m,
            (band_gray.shape[1], band_gray.shape[0]),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )


def dewarp_mask_gray(
    frame_bgr,
    warp: CropIsoWarp,
    mask: np.ndarray,
    stabilizer: BandStabilizer | None = None,
) -> np.ndarray:
    """Iso-dewarp (band crop) + grayscale (+ optional wind alignment) + apply the
    precomputed band mask. Alignment runs BEFORE the mask so the correlation sees
    real content and a wind-displaced ball is pulled back inside the mask instead
    of being zeroed; the caller maps coordinates via ``stabilizer.last``."""
    import cv2  # noqa: PLC0415

    band = warp.frame(frame_bgr)
    gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
    if stabilizer is not None:
        gray = stabilizer.align(gray)
        if gray.base is not None or not gray.flags.writeable:
            gray = gray.copy()
    gray[mask == 0] = 0
    return gray


def band_mask(warp: CropIsoWarp, polygon) -> np.ndarray:
    """Binary mask of the (far-margin-expanded) field polygon in band coords."""
    import cv2  # noqa: PLC0415

    bh, bw = warp.shape
    mask = np.zeros((bh, bw), np.uint8)
    cv2.fillPoly(mask, [warp.points(polygon).astype(np.int32)], 255)
    return mask
