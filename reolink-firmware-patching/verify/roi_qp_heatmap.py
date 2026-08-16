"""Per-block detail-energy heatmap for A/B-testing the encoder ROI window.

The camera gives us no per-CTU QP readout, and no HEVC tool we have decodes one
either. What a QP change does leave behind is a change in preserved detail: a
block coded at a lower QP keeps more high-frequency energy. So instead of
reading QP, measure it — decode two recordings of the *same static scene*
(ROI off, then ROI on), compute the mean absolute Laplacian per block, and
divide. A working ROI window shows up as a rectangle in the ratio map.

This is also how the units question gets settled. If the rectangle lands where
the config asked for it, x/y/w/h are pixels. If it lands in the top-left at
1/64 the requested size, they are 64x64 CTUs.

Both clips must be shot with the scene and the exposure locked, or the ratio map
measures the weather. Use runtime/set_exposure.sh manual before recording.

Usage:
    python roi_qp_heatmap.py before.mp4 after.mp4 --out heatmap.png
    python roi_qp_heatmap.py before.mp4 after.mp4 --out heatmap.png \\
        --frames 120 --block 64 --expect 0,760,7680,900
"""

from __future__ import annotations

import argparse
import sys

import av
import numpy as np
from PIL import Image, ImageDraw

# 4-neighbour Laplacian. Absolute response averaged over a block is a cheap,
# reference-free stand-in for "how much detail survived quantisation".
_KERNEL_OFFSETS = ((-1, 0), (1, 0), (0, -1), (0, 1))


def block_detail(gray: np.ndarray, block: int) -> np.ndarray:
    """Mean |Laplacian| per block x block tile of a single-channel frame."""
    lap = -4.0 * gray
    for dy, dx in _KERNEL_OFFSETS:
        lap += np.roll(np.roll(gray, dy, axis=0), dx, axis=1)
    energy = np.abs(lap)
    # Drop the 1px wrap-around border so np.roll's edge artefacts stay out.
    energy[0, :] = energy[-1, :] = energy[:, 0] = energy[:, -1] = 0.0
    h, w = energy.shape
    bh, bw = h // block, w // block
    trimmed = energy[: bh * block, : bw * block]
    return trimmed.reshape(bh, block, bw, block).mean(axis=(1, 3))


def accumulate(path: str, frames: int, block: int) -> tuple[np.ndarray, int]:
    """Average the per-block detail map over the first `frames` decoded frames."""
    total: np.ndarray | None = None
    count = 0
    with av.open(path) as container:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        for frame in container.decode(stream):
            gray = frame.to_ndarray(format="gray").astype(np.float32)
            tile = block_detail(gray, block)
            total = tile if total is None else total + tile
            count += 1
            if count >= frames:
                break
    if total is None or count == 0:
        raise SystemExit(f"no frames decoded from {path}")
    return total / count, count


def render(ratio: np.ndarray, block: int, out: str, expect: str | None) -> None:
    """Write the ratio map as a PNG, red = more detail after, blue = less."""
    # Centre the colour scale on 1.0 and clip at +/-25%, which is a large effect
    # for a handful of QP steps; anything beyond that saturates.
    span = 0.25
    norm = np.clip((ratio - 1.0) / span, -1.0, 1.0)
    rgb = np.zeros((*norm.shape, 3), dtype=np.uint8)
    pos, neg = np.clip(norm, 0, 1), np.clip(-norm, 0, 1)
    base = 40.0
    rgb[..., 0] = (base + pos * (255 - base)).astype(np.uint8)
    rgb[..., 2] = (base + neg * (255 - base)).astype(np.uint8)
    rgb[..., 1] = base

    img = Image.fromarray(rgb).resize(
        (norm.shape[1] * 4, norm.shape[0] * 4), Image.NEAREST
    )
    if expect:
        try:
            ex, ey, ew, eh = (int(v) for v in expect.split(","))
        except ValueError:
            raise SystemExit("--expect wants x,y,w,h in pixels") from None
        scale = 4.0 / block
        draw = ImageDraw.Draw(img)
        draw.rectangle(
            [ex * scale, ey * scale, (ex + ew) * scale - 1, (ey + eh) * scale - 1],
            outline=(255, 255, 255),
            width=2,
        )
    img.save(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("before", help="recording with the ROI disabled (control)")
    ap.add_argument("after", help="recording with the ROI enabled")
    ap.add_argument("--out", default="roi_heatmap.png", help="output PNG")
    ap.add_argument("--frames", type=int, default=120, help="frames to average")
    ap.add_argument("--block", type=int, default=64, help="block size in pixels")
    ap.add_argument(
        "--expect",
        default=None,
        help="x,y,w,h in pixels — drawn as a white outline for comparison",
    )
    args = ap.parse_args()

    a, na = accumulate(args.before, args.frames, args.block)
    b, nb = accumulate(args.after, args.frames, args.block)
    if a.shape != b.shape:
        raise SystemExit(f"geometry differs: {a.shape} vs {b.shape}")

    # Guard against divide-by-zero on flat blocks (clipped sky at night).
    floor = max(float(np.median(a)) * 1e-3, 1e-6)
    ratio = b / np.maximum(a, floor)

    render(ratio, args.block, args.out, args.expect)

    rows = ratio.mean(axis=1)
    print(f"decoded {na} + {nb} frames, blocks {ratio.shape[0]}x{ratio.shape[1]}")
    print(
        f"ratio: min {ratio.min():.3f}  median {np.median(ratio):.3f}  max {ratio.max():.3f}"
    )
    print("per-row-band mean ratio (row = y // block):")
    for i in range(0, len(rows), max(1, len(rows) // 16)):
        y = i * args.block
        print(f"  y={y:5d}  {rows[i]:.3f}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
