"""Ball CANDIDATE detection on panoramic frames — the homegrown heatmap detector.

The detector is a small fully-convolutional U-Net (see
``training/models/heatmap_net.py``) exported to ONNX: it takes THREE stacked
grayscale frames (temporal context — a moving ball pops against the static field)
of the isotropically-dewarped field band and emits a per-pixel ball-center
heatmap (sigmoid baked into the export). Peaks of the heatmap are the per-frame
CANDIDATES: this step deliberately emits the raw top-K above a low floor — which
candidate is the game ball is the SELECTOR's job
(:mod:`video_grouper.inference.ball_selector` + ``ball_tracker.rerank``), applied
cheaply downstream where it can be re-tuned without re-running detection.

Runtime-friendly: top-level imports limited to numpy, onnxruntime, cv2 (lazy).
No torch — that lives in the training extra and is not bundled into the
service / tray exes.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort

from video_grouper.inference.iso_warp import (
    CropIsoWarp,
    band_mask,
    dewarp_mask_gray,
    far_margin_polygon,
    native_iso_warp,
)

logger = logging.getLogger(__name__)

# Champion inference geometry (matches the training dumps that validated the stack).
TOP_K = 24
SCORE_FLOOR = 0.1
PEAK_MIN_DISTANCE = 3
TILE_W = 2560
TILE_OVERLAP = 256
FAR_MARGIN_PX = 400.0


def create_session(model_path: Path, use_gpu: bool = True) -> ort.InferenceSession:
    """Create an ONNX inference session.

    Provider order when ``use_gpu`` is True: CUDA (onnxruntime-gpu wheel), then
    DirectML (onnxruntime-directml wheel — the norm on customer Windows installs,
    where the GPU is whatever the machine has), then CPU. Only providers the
    installed wheel actually offers are requested.
    """
    available = set(ort.get_available_providers())
    providers: list[str] = []
    if use_gpu:
        for p in ("CUDAExecutionProvider", "DmlExecutionProvider"):
            if p in available:
                providers.append(p)
    providers.append("CPUExecutionProvider")

    sess = ort.InferenceSession(str(model_path), providers=providers)
    logger.info("ONNX session using: %s", sess.get_providers())
    return sess


def extract_peaks(
    heatmap: np.ndarray,
    top_k: int = TOP_K,
    threshold: float = SCORE_FLOOR,
    min_distance: int = PEAK_MIN_DISTANCE,
) -> list[tuple[float, float, float]]:
    """Extract up to ``top_k`` local-maxima peaks from a 2-D heatmap.

    Returns ``(x, y, score)`` rows in heatmap pixel coords, score-descending.
    ``min_distance`` is the NMS radius — peaks closer than this are suppressed
    via a ``(2*min_distance+1)`` dilation; only true local maxima survive.
    """
    import cv2  # noqa: PLC0415

    hm = np.asarray(heatmap, dtype=np.float32)
    if hm.ndim != 2:
        raise ValueError(f"heatmap must be 2-D, got shape {hm.shape}")
    ksize = 2 * int(min_distance) + 1
    dilated = cv2.dilate(hm, np.ones((ksize, ksize), np.uint8))
    mask = (hm >= dilated) & (hm >= threshold)
    ys, xs = np.where(mask)
    if ys.size == 0:
        return []
    scores = hm[ys, xs]
    order = np.argsort(scores)[::-1][:top_k]
    return [(float(xs[i]), float(ys[i]), float(scores[i])) for i in order]


def _pad8(a: np.ndarray) -> tuple[np.ndarray, int, int]:
    """Pad a ``(C, H, W)`` stack so H, W are multiples of 8 (the net's 3 downsamples)."""
    _, h, w = a.shape
    ph, pw = (-h) % 8, (-w) % 8
    if ph or pw:
        a = np.pad(a, ((0, 0), (0, ph), (0, pw)))
    return a, h, w


def infer_band(
    sess: ort.InferenceSession,
    stack: np.ndarray,
    tile_w: int = TILE_W,
    overlap: int = TILE_OVERLAP,
) -> np.ndarray:
    """Run the fully-conv detector over a wide field band in horizontal tiles;
    stitch the sigmoid heatmaps by max in the overlaps. ``stack`` is
    ``(3, bh, bw)`` float32 in [0, 1]. Returns ``(bh, bw)``.

    Mirrors ``training/cli/eval_detector.py::infer_band`` (torch) — the export
    bakes the sigmoid into the graph, so the session output IS the heatmap.
    """
    input_name = sess.get_inputs()[0].name
    _, bh, bw = stack.shape
    hm = np.zeros((bh, bw), np.float32)
    x0 = 0
    while x0 < bw:
        x1 = min(x0 + tile_w, bw)
        tile = stack[:, :, x0:x1]
        padded, th, tw = _pad8(tile)
        out = sess.run(None, {input_name: padded[None]})[0][0, 0, :th, :tw]
        hm[:, x0:x1] = np.maximum(hm[:, x0:x1], out)
        if x1 >= bw:
            break
        x0 = x1 - overlap
    return hm


def detect_video_candidates(
    video_path: Path,
    sess: ort.InferenceSession,
    polygon: np.ndarray,
    *,
    stride: int = 8,
    top_k: int = TOP_K,
    threshold: float = SCORE_FLOOR,
    min_distance: int = PEAK_MIN_DISTANCE,
    tile_w: int = TILE_W,
    overlap: int = TILE_OVERLAP,
    far_margin: float = FAR_MARGIN_PX,
    target_width: int | None = None,
) -> tuple[dict[int, list[tuple[float, float, float]]], dict]:
    """Run the heatmap detector over a video at ``stride`` -> per-frame candidates.

    The band is cropped from the far-margin-expanded ``polygon`` (a 10-point
    field outline; airborne balls above the far line stay in-band) and
    isotropically scaled to ``target_width`` (cross-camera ball-size
    normalization — None = native). Each sampled frame is inferred from its
    3-frame grayscale history (consecutive SOURCE frames, so every frame is
    decoded; only inference runs at ``stride``).

    Returns ``({global_frame: [(x, y, score), ...]}, info)`` with candidate
    coordinates mapped back to SOURCE pixels and ``info`` carrying
    ``{src_w, src_h, fps, n_frames}``.
    """
    import av  # noqa: PLC0415

    far_poly = far_margin_polygon(polygon, far_margin)
    cands: dict[int, list[tuple[float, float, float]]] = {}
    t0 = time.time()

    with av.open(str(video_path)) as container:
        vs = container.streams.video[0]
        src_w = vs.codec_context.width
        src_h = vs.codec_context.height
        fps = float(vs.average_rate) if vs.average_rate else 20.0
        warp: CropIsoWarp = native_iso_warp(far_poly, src_w, src_h, target_width)
        mask = band_mask(warp, far_poly)
        grays: list[np.ndarray] = []

        frame_idx = 0
        for frame in container.decode(video=0):
            bgr = frame.to_ndarray(format="bgr24")
            grays.append(dewarp_mask_gray(bgr, warp, mask))
            if len(grays) > 3:
                grays.pop(0)
            if frame_idx % stride == 0:
                seq = (
                    grays if len(grays) == 3 else [grays[0]] * (3 - len(grays)) + grays
                )
                stack = np.stack(seq, 0).astype(np.float32) / 255.0
                hm = infer_band(sess, stack, tile_w, overlap)
                peaks = extract_peaks(hm, top_k, threshold, min_distance)
                cands[frame_idx] = [
                    (
                        round(float(hx) / warp.scale, 1),
                        round(float(hy) / warp.scale + warp.y_top, 1),
                        round(float(sc), 4),
                    )
                    for (hx, hy, sc) in peaks
                ]
                if len(cands) % 100 == 0:
                    el = time.time() - t0
                    logger.info(
                        "detect: %d frames sampled (%.1f inferred/s)",
                        len(cands),
                        len(cands) / el if el > 0 else 0.0,
                    )
            frame_idx += 1

    logger.info(
        "detect DONE: %d/%d frames sampled in %.0fs",
        len(cands),
        frame_idx,
        time.time() - t0,
    )
    return cands, {"src_w": src_w, "src_h": src_h, "fps": fps, "n_frames": frame_idx}
