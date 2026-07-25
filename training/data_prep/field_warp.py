"""Perspective-normalizing field warp for the full-frame ball detector.

The production camera is a Reolink 7680x2160 panorama. The field occupies a
narrow horizontal band (~600-800px of the 2160 vertical); the rest is
sky/trees/spectators. Because of perspective, the ball's apparent size varies
with field position: ~8.5px in the far rows up to ~33px in the near rows -- a
2-4x far->near gradient.

The old detector handled this by tiling the band into a 7x3 grid (21 native-
resolution tiles) and running inference per tile. This module replaces that
with a single full-frame inference:

1. Crop to the field band ``[y_top, y_bot]``.
2. Apply an **anisotropic vertical warp** that compresses near rows ~3-4x
   while leaving far rows near native. The per-row vertical scale is
   ``far_native_size / size(row)`` clipped to ``(0, 1]`` so the far field is
   NEVER upscaled -- the far rows are an information ceiling.
3. Resize horizontally to a target width ``TW``.

The result is a compact image in which the ball is ~uniform size everywhere
(~the far-native size). Run ONE inference on it, then map the detected
``(x, y)`` back to source pixels with :func:`unwarp_points` so the downstream
broadcast renderer (which is unchanged and works in source coords) sees real
panoramic coordinates.

This module is pure numpy + cv2 (no torch / onnxruntime / heavy deps) so it can
be imported standalone at BOTH training time (warp frames into the dataset) and
inference time (warp before detect, unwarp after).

The forward-warp math was validated in a throwaway experiment script; this
module productionizes it and adds the precise inverse (:func:`unwarp_points`)
that the experiment lacked.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

# Number of bands the ball-size gradient is bucketed into when fitting the
# size(row) curve. 10 bands (11 edges) matched the validated experiment.
DEFAULT_N_BANDS = 10

# Default output width for the horizontally-resized warped image. Detector
# input is square-ish; the band is wide and short after vertical compression.
DEFAULT_TARGET_WIDTH = 1280


@dataclass(frozen=True)
class FieldWarp:
    """Precomputed forward + inverse field warp.

    Carries everything needed to warp a frame forward and to map detected
    points back to source pixels. Immutable: build once per (gradient, frame
    size, TW) and reuse across all frames of a recording.

    Attributes:
        src_w: Source frame width ``W`` (full panorama width).
        src_h: Source frame height (full panorama height).
        y_top: Top row (inclusive) of the field band in source coords.
        y_bot: Bottom row (inclusive) of the field band in source coords.
        out_h: Height of the warped (vertically compressed) image, BEFORE the
            horizontal resize.
        target_width: Output width ``TW`` after the horizontal resize.
        final_h: Final warped image height after the horizontal resize
            (vertical scaled by ``TW / W`` to preserve aspect).
        target_size: Far-native ball size used as the uniform target size.
        map_x: ``cv2.remap`` x-map for the vertical warp, shape ``(out_h, W)``.
        map_y: ``cv2.remap`` y-map for the vertical warp, shape ``(out_h, W)``.
            Values are source rows relative to ``y_top`` (i.e. into the crop).
        inv_lut: ``inv_lut[r]`` = the source row (absolute, NOT relative to
            ``y_top``) that maps to warped row ``r``. Length ``out_h``. This is
            the inverse of the cumulative forward mapping and is used by
            :func:`unwarp_points`.
        scale: Per-source-row vertical scale, length ``y_bot - y_top + 1``.
            Every entry is in ``(0, 1]``; the far end is exactly ``1.0``.
    """

    src_w: int
    src_h: int
    y_top: int
    y_bot: int
    out_h: int
    target_width: int
    final_h: int
    target_size: float
    map_x: np.ndarray
    map_y: np.ndarray
    inv_lut: np.ndarray
    scale: np.ndarray

    # ---- reporting helpers -------------------------------------------------

    @property
    def band_height(self) -> int:
        """Height of the source field band in pixels."""
        return self.y_bot - self.y_top + 1

    @property
    def vertical_compression(self) -> float:
        """Band height / warped height (before horizontal resize).

        How much the anisotropic vertical warp squeezes the band. >= 1.0.
        """
        return self.band_height / self.out_h

    @property
    def output_shape(self) -> tuple[int, int]:
        """Final warped image shape ``(height, width)`` fed to the detector."""
        return (self.final_h, self.target_width)

    @property
    def output_megapixels(self) -> float:
        """Megapixels of the single warped detector input."""
        return self.final_h * self.target_width / 1e6

    def tiled_megapixels(self, tile_size: int = 640, n_tiles: int = 21) -> float:
        """Megapixels the old N-tile path pushed through the detector.

        The 7x3 path ran ``n_tiles`` square ``tile_size`` inferences per frame.
        """
        return n_tiles * tile_size * tile_size / 1e6

    def speedup_vs_tiled(self, tile_size: int = 640, n_tiles: int = 21) -> float:
        """Pixel-throughput ratio of the tiled path to this single warp.

        A rough proxy for the inference speed win (detector cost scales with
        pixels processed). >1 means the warp pushes fewer pixels.
        """
        return self.tiled_megapixels(tile_size, n_tiles) / self.output_megapixels


def build_field_warp(
    rows: np.ndarray,
    sizes: np.ndarray,
    src_w: int,
    src_h: int,
    target_width: int = DEFAULT_TARGET_WIDTH,
    n_bands: int = DEFAULT_N_BANDS,
) -> FieldWarp:
    """Build a :class:`FieldWarp` from a ball-size-vs-row gradient.

    Args:
        rows: Source pixel rows (y coords) at which ball size was measured.
            Shape ``(N,)``. Need not be sorted or unique; any spread across the
            field band works. These define the band extent ``[y_top, y_bot]``.
        sizes: Measured ball apparent size (px) at each row in ``rows``. Shape
            ``(N,)``, same length as ``rows``. Larger = nearer the camera.
        src_w: Source frame width ``W``.
        src_h: Source frame height.
        target_width: Output width ``TW`` after horizontal resize.
        n_bands: Number of bands to bucket the gradient into when fitting the
            monotone size(row) curve.

    Returns:
        A :class:`FieldWarp` with forward remap maps and the inverse LUT.

    Raises:
        ValueError: if inputs are empty, mismatched, or degenerate.

    The size(row) curve is fitted by bucketing ``rows`` into ``n_bands`` equal
    bands, taking the per-band median size at the per-band median row, then
    enforcing monotone-non-decreasing (far small -> near large) via a running
    max. The far-native size ``target = min(size)`` is the uniform target the
    warp normalizes everything to; per-row vertical scale is
    ``clip(target / size(row), 0, 1]`` so the far field is never upscaled.
    """
    rows = np.asarray(rows, dtype=np.float64).ravel()
    sizes = np.asarray(sizes, dtype=np.float64).ravel()
    if rows.size == 0 or sizes.size == 0:
        raise ValueError("rows and sizes must be non-empty")
    if rows.size != sizes.size:
        raise ValueError(f"rows and sizes length mismatch: {rows.size} != {sizes.size}")
    if not np.all(np.isfinite(rows)) or not np.all(np.isfinite(sizes)):
        raise ValueError("rows and sizes must be finite")
    if np.any(sizes <= 0):
        raise ValueError("sizes must be positive")
    if src_w <= 0 or src_h <= 0 or target_width <= 0:
        raise ValueError("src_w, src_h, target_width must be positive")
    if n_bands < 1:
        raise ValueError("n_bands must be >= 1")

    # --- fit a monotone size(row) curve by banding the gradient ---
    band_edges = np.linspace(rows.min(), rows.max(), n_bands + 1)
    by_list: list[float] = []
    bs_list: list[float] = []
    for i in range(n_bands):
        lo, hi = band_edges[i], band_edges[i + 1]
        # Last band is closed on the right so the max row is included.
        if i == n_bands - 1:
            in_band = (rows >= lo) & (rows <= hi)
        else:
            in_band = (rows >= lo) & (rows < hi)
        if not np.any(in_band):
            continue
        by_list.append(float(np.median(rows[in_band])))
        bs_list.append(float(np.median(sizes[in_band])))

    if len(by_list) < 1:
        raise ValueError("no populated bands; check rows/sizes")

    by = np.asarray(by_list, dtype=np.float64)
    bs = np.asarray(bs_list, dtype=np.float64)
    # Sort by row so interpolation is well-defined, then enforce monotone size.
    order = np.argsort(by)
    by = by[order]
    bs = bs[order]
    bs = np.maximum.accumulate(bs)  # far small -> near large

    y_top = int(np.floor(by.min()))
    y_bot = int(np.ceil(by.max()))
    # Clamp to the frame in case rounding pushed past the edge.
    y_top = max(0, min(y_top, src_h - 1))
    y_bot = max(y_top, min(y_bot, src_h - 1))

    target = float(bs.min())  # far native size -- the uniform target, never upscaled

    # --- per-source-row vertical scale, clipped so far field is never upscaled ---
    src = np.arange(y_top, y_bot + 1, dtype=np.float64)
    size_at = np.interp(src, by, bs)
    scale = np.clip(target / size_at, 0.0, 1.0)

    # --- cumulative output-row mapping ---
    # out_y[k] = warped row that source row src[k] lands at (its top edge).
    out_y = np.concatenate([[0.0], np.cumsum(scale)])[:-1]
    out_h = int(out_y[-1]) + 1

    # Inverse LUT: warped row index -> source row (absolute, +y_top).
    # np.interp on the (out_y -> src) pairs gives source row per warped row.
    inv_lut = np.interp(np.arange(out_h, dtype=np.float64), out_y, src)

    # --- forward remap maps for cv2.remap on the cropped band ---
    # map_y values are source rows RELATIVE to y_top (into the crop).
    map_y_col = (inv_lut - y_top).astype(np.float32)
    map_y = np.repeat(map_y_col[:, None], src_w, axis=1)
    map_x = np.repeat(np.arange(src_w, dtype=np.float32)[None, :], out_h, axis=0)

    # Final height after the horizontal resize (preserve aspect: scale by TW/W).
    final_h = max(1, int(round(out_h * target_width / src_w)))

    return FieldWarp(
        src_w=int(src_w),
        src_h=int(src_h),
        y_top=y_top,
        y_bot=y_bot,
        out_h=out_h,
        target_width=int(target_width),
        final_h=final_h,
        target_size=target,
        map_x=map_x,
        map_y=map_y,
        inv_lut=inv_lut,
        scale=scale,
    )


def warp_frame(frame: np.ndarray, warp: FieldWarp) -> np.ndarray:
    """Forward-warp a source frame into the compact detector input.

    Crops to the field band, applies the anisotropic vertical remap, then
    resizes horizontally to ``target_width`` (vertical scaled to preserve
    aspect). Uses ``INTER_AREA`` throughout since this is always a downscale.

    Args:
        frame: Source image, shape ``(src_h, src_w[, C])``. Must match the
            ``src_w`` / ``src_h`` the warp was built for.
        warp: A :class:`FieldWarp` from :func:`build_field_warp`.

    Returns:
        The warped image, shape ``(final_h, target_width[, C])``.

    Raises:
        ValueError: if the frame dimensions don't match the warp.
    """
    h, w = frame.shape[:2]
    if w != warp.src_w or h != warp.src_h:
        raise ValueError(
            f"frame size ({w}x{h}) does not match warp ({warp.src_w}x{warp.src_h})"
        )

    band = frame[warp.y_top : warp.y_bot + 1]
    warped = cv2.remap(
        band,
        warp.map_x,
        warp.map_y,
        interpolation=cv2.INTER_AREA,
        borderMode=cv2.BORDER_REPLICATE,
    )
    resized = cv2.resize(
        warped,
        (warp.target_width, warp.final_h),
        interpolation=cv2.INTER_AREA,
    )
    return resized


def warp_points(points_xy: np.ndarray, warp: FieldWarp) -> np.ndarray:
    """Map source ``(x, y)`` points into warped/resized detector coords.

    The forward analogue of :func:`unwarp_points` (used in tests and to warp
    label coordinates into the dataset). Points outside the field band are
    still mapped (the y-mapping extrapolates via the LUT inverse), but callers
    generally only warp points known to lie in the band.

    Args:
        points_xy: ``(M, 2)`` array of ``(x, y)`` in source pixel coords.
        warp: The :class:`FieldWarp`.

    Returns:
        ``(M, 2)`` array of ``(x, y)`` in warped/resized coords (``float64``).
    """
    pts = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
    xs = pts[:, 0]
    ys = pts[:, 1]

    # Horizontal: uniform scale W -> TW.
    hscale = warp.target_width / warp.src_w
    out_x = xs * hscale

    # Vertical: source row -> warped row via the forward cumulative mapping,
    # which is the inverse of inv_lut. inv_lut maps warped_row -> source_row
    # and is monotone non-decreasing, so np.interp inverts it.
    warped_rows = np.arange(warp.out_h, dtype=np.float64)
    out_y_warp = np.interp(ys, warp.inv_lut, warped_rows)
    # Then the horizontal-resize vertical scale (final_h / out_h).
    vscale = warp.final_h / warp.out_h
    out_y = out_y_warp * vscale

    return np.column_stack([out_x, out_y])


def unwarp_points(points_xy: np.ndarray, warp: FieldWarp) -> np.ndarray:
    """Map warped/resized detector ``(x, y)`` points back to source pixels.

    The precise inverse of the forward warp (:func:`warp_frame` /
    :func:`warp_points`). Used at inference time to convert detected ball
    coordinates back into the source panorama frame for the broadcast renderer.

    Inversion steps (exactly undoing the forward chain):

    1. Horizontal: ``x_src = x * W / TW`` (undo the ``TW`` resize).
    2. Vertical, undo the horizontal-resize vertical scale:
       ``y_warp = y * out_h / final_h``.
    3. Vertical, undo the anisotropic warp: look ``y_warp`` up in ``inv_lut``
       (warped row -> source row, already absolute incl. ``y_top``).

    Args:
        points_xy: ``(M, 2)`` array of ``(x, y)`` in warped/resized coords.
        warp: The :class:`FieldWarp`.

    Returns:
        ``(M, 2)`` array of ``(x, y)`` in source pixel coords (``float64``).
    """
    pts = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
    xs = pts[:, 0]
    ys = pts[:, 1]

    # Horizontal inverse: TW -> W.
    out_x = xs * (warp.src_w / warp.target_width)

    # Vertical inverse: undo the resize's vertical scale, then the warp LUT.
    vscale = warp.final_h / warp.out_h
    y_warp = ys / vscale
    warped_rows = np.arange(warp.out_h, dtype=np.float64)
    # inv_lut already includes +y_top (absolute source rows).
    out_y = np.interp(y_warp, warped_rows, warp.inv_lut)

    return np.column_stack([out_x, out_y])
