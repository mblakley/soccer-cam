"""Heatmap training data for the v4 ball detector.

Pipeline per labeled frame (matches the runtime design: dewarp → polygon-mask →
heatmap):

1. **Dewarp** = crop the field band at **native resolution** (isotropic, so the
   ball stays round and keeps its native ~8 px size — no downscaling, which is
   what killed the bbox attempt).
2. **Polygon-mask** = zero every pixel outside the field polygon, with a generous
   **far margin** above the far touchline (far-corner balls sit slightly above the
   detected line; a tight crop would slice exactly the balls we care about). This
   removes the off-field false-positive sources (trees / netting / spectators).
3. **3 consecutive grayscale frames** ``[t-2, t-1, t]`` stacked → motion context
   (a moving ball pops against the static field; color is ~useless for the ball).
4. Fixed-size **crops** around the ball (+jitter) with a **Gaussian center target**,
   plus background crops as negatives. The net is fully convolutional, so training
   on crops and running on the whole masked band at inference is consistent.

The camera encodes GOP=1 (all keyframes), so seeking to ``t-2`` and decoding three
frames is exact and cheap.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from training.data_prep.warped_dataset import CropIsoWarp, field_band_from_polygon


def gaussian_heatmap(h: int, w: int, cx: float, cy: float, sigma: float) -> np.ndarray:
    """Single-blob Gaussian heatmap in [0,1], peak 1.0 at (cx, cy)."""
    ys, xs = np.ogrid[:h, :w]
    g = np.exp(-((xs - cx) ** 2 + (ys - cy) ** 2) / (2.0 * sigma * sigma))
    return g.astype(np.float32)


def _far_margin_polygon(polygon, far_margin: float) -> np.ndarray:
    """Push the far sideline (points 5-9) up by ``far_margin`` so the mask keeps a
    margin above the far touchline."""
    poly = np.asarray(polygon, dtype=np.float64).copy()
    if len(poly) >= 10:
        poly[5:10, 1] = np.maximum(poly[5:10, 1] - far_margin, 0.0)
    return poly


def _native_iso_warp(polygon, src_w: int, src_h: int):
    """Isotropic field-band crop at native res (scale 1): the 'dewarp'."""
    yt, yb = field_band_from_polygon(polygon)
    return CropIsoWarp(int(src_w), int(src_h), int(yt), int(yb), int(src_w))


def _dewarp_mask_gray(frame_bgr, warp, mask):
    """Iso-dewarp (band crop) + grayscale + apply the precomputed band mask."""
    import cv2

    band = warp.frame(frame_bgr)  # native band crop (scale 1)
    gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
    gray[mask == 0] = 0
    return gray


def build_heatmap_crops(
    games: list[dict],
    out_dir,
    *,
    crop: int = 256,
    sigma: float = 4.0,
    jitter: int = 48,
    far_margin: float = 120.0,
    neg_ratio: float = 0.7,
    val_game_ids: set[str] | None = None,
) -> dict:
    """Pre-render 3-frame grayscale crops + ball-center targets to ``out_dir``.

    Each ``games`` item: ``{game_id, video, polygon, labels: {frame_idx: (x, y)},
    split?}``. ``labels`` are SOURCE-pixel ball centers (human + trusted reference
    detections). Writes ``crops/*.npy`` (uint8 [3, crop, crop]) and an
    ``index.json`` with per-sample ``{file, x, y|null, split}`` (x/y in crop px;
    null = background/negative). Returns a summary.
    """
    import av
    import cv2

    out_dir = Path(out_dir)
    (out_dir / "crops").mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(1234)
    index: list[dict] = []
    half = crop // 2

    for g in games:
        gid = g["game_id"]
        split = (
            "val" if (val_game_ids and gid in val_game_ids) else g.get("split", "train")
        )
        polygon = g["polygon"]
        labels = {int(k): v for k, v in g["labels"].items()}
        if not labels:
            continue
        container = av.open(str(g["video"]))
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        sw = stream.codec_context.width
        sh = stream.codec_context.height
        fps = float(stream.average_rate) or 20.0
        warp = _native_iso_warp(polygon, sw, sh)
        bh, bw = warp.shape
        # band mask (with far margin), in dewarped/band coords
        mpoly = warp.points(_far_margin_polygon(polygon, far_margin)).astype(np.int32)
        mask = np.zeros((bh, bw), np.uint8)
        cv2.fillPoly(mask, [mpoly], 255)

        for t in sorted(labels):
            bx, by = labels[t]
            # seek to t-2 and decode 3 consecutive frames (GOP=1 -> exact)
            t0 = max(0, t - 2)
            try:
                container.seek(
                    int((t0 / fps) / stream.time_base), stream=stream, backward=True
                )
            except Exception:
                pass
            frames = []
            for fr in container.decode(stream):
                frames.append(fr)
                if len(frames) >= 3:
                    break
            if len(frames) < 3:
                continue
            grays = [
                _dewarp_mask_gray(fr.to_ndarray(format="bgr24"), warp, mask)
                for fr in frames[-3:]
            ]
            # ball in dewarped/band coords
            dxy = warp.points([(bx, by)])[0]
            dbx, dby = float(dxy[0]), float(dxy[1])

            def _emit(ccx, ccy, has_ball, tag):
                x0 = int(np.clip(round(ccx) - half, 0, max(0, bw - crop)))
                y0 = int(np.clip(round(ccy) - half, 0, max(0, bh - crop)))
                stack = np.zeros((3, crop, crop), np.uint8)
                for i, gr in enumerate(grays):
                    patch = gr[y0 : y0 + crop, x0 : x0 + crop]
                    stack[i, : patch.shape[0], : patch.shape[1]] = patch
                if has_ball:
                    lx, ly = dbx - x0, dby - y0
                    if not (0 <= lx < crop and 0 <= ly < crop):
                        return
                else:
                    lx = ly = None
                fname = f"{gid}_f{t:06d}_{tag}.npy"
                np.save(out_dir / "crops" / fname, stack)
                index.append(
                    {
                        "file": fname,
                        "x": None if lx is None else round(float(lx), 1),
                        "y": None if ly is None else round(float(ly), 1),
                        "split": split,
                    }
                )

            # positive: crop around ball (+jitter)
            jx = rng.integers(-jitter, jitter + 1)
            jy = rng.integers(-jitter, jitter + 1)
            _emit(dbx + jx, dby + jy, True, "pos")
            # negative: a random in-mask location away from the ball
            if rng.random() < neg_ratio:
                for _ in range(8):
                    nx = rng.integers(half, max(half + 1, bw - half))
                    ny = rng.integers(half, max(half + 1, bh - half))
                    if (
                        mask[int(ny), int(nx)]
                        and (nx - dbx) ** 2 + (ny - dby) ** 2 > (crop * 0.75) ** 2
                    ):
                        _emit(nx, ny, False, "neg")
                        break
        container.close()

    n_train = sum(1 for r in index if r["split"] == "train")
    n_val = sum(1 for r in index if r["split"] == "val")
    summary = {
        "crop": crop,
        "sigma": sigma,
        "samples": len(index),
        "train": n_train,
        "val": n_val,
        "positives": sum(1 for r in index if r["x"] is not None),
    }
    (out_dir / "index.json").write_text(
        json.dumps({"summary": summary, "items": index})
    )
    return summary


class HeatmapCropDataset:
    """torch Dataset over pre-rendered crops; builds the Gaussian target at load."""

    def __init__(self, root, split: str = "train", crop: int = 256, sigma: float = 4.0):
        self.root = Path(root)
        data = json.loads((self.root / "index.json").read_text())
        self.crop = data.get("summary", {}).get("crop", crop)
        self.sigma = data.get("summary", {}).get("sigma", sigma)
        self.items = [r for r in data["items"] if r["split"] == split]

    def __len__(self):
        return len(self.items)

    def __getitem__(self, i):
        import torch

        r = self.items[i]
        stack = np.load(self.root / "crops" / r["file"]).astype(np.float32) / 255.0
        if r["x"] is None:
            tgt = np.zeros((self.crop, self.crop), np.float32)
        else:
            tgt = gaussian_heatmap(self.crop, self.crop, r["x"], r["y"], self.sigma)
        return torch.from_numpy(stack), torch.from_numpy(tgt[None])
