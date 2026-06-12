"""Side-by-side comparison: Original / Baseline / Polygon-blend.

Reuses the per-zone motion.json files already on disk:
  .verify/motion.json              (current production grid-similarity)
  .verify/motion_zone_sky.json     (sky-band similarity fit)
  .verify/motion_zone_field.json   (field-band similarity fit)
  .verify/motion_zone_near.json    (foreground-band similarity fit)

Produces a vertical 3-panel video at 1920×~1720 — each panel ~540 tall so
the full ultrawide source aspect is preserved and three panels fit on a
single screen height. Adds a yellow horizontal reference line on each
panel at the same panel-relative y so motion of the horizon stands out.
"""

from __future__ import annotations

import argparse
import logging
import sys
from fractions import Fraction
from pathlib import Path

import av
import cv2
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from video_grouper.inference.field_geometry import load_field  # noqa: E402
from video_grouper.inference.stabilization import FrameStabilizer  # noqa: E402

PANEL_W = 1920
PANEL_H = 540
LABEL_H = 32
DIVIDER_PX = 2
OUT_W = PANEL_W
OUT_H = 3 * (PANEL_H + LABEL_H) + 2 * DIVIDER_PX
REF_LINE_Y_FRAC = 0.30  # yellow reference line, panel-relative
REF_COLOR = (40, 220, 240)


def label(text):
    bar = np.full((LABEL_H, PANEL_W, 3), 30, dtype=np.uint8)
    cv2.putText(
        bar,
        text,
        (16, 24),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.65,
        (240, 240, 240),
        2,
        cv2.LINE_AA,
    )
    return bar


def fit_panel(rgb, ref_line=True):
    src_h, src_w = rgb.shape[:2]
    panel = np.zeros((PANEL_H, PANEL_W, 3), dtype=np.uint8)
    scale = min(PANEL_W / src_w, PANEL_H / src_h)
    fit_w, fit_h = int(src_w * scale), int(src_h * scale)
    resized = cv2.resize(rgb, (fit_w, fit_h), interpolation=cv2.INTER_AREA)
    y0 = (PANEL_H - fit_h) // 2
    x0 = (PANEL_W - fit_w) // 2
    panel[y0 : y0 + fit_h, x0 : x0 + fit_w] = resized
    if ref_line:
        ly = int(PANEL_H * REF_LINE_Y_FRAC)
        cv2.line(panel, (0, ly), (PANEL_W, ly), REF_COLOR, 2)
    return panel


def build_zone_mask(polygon, src_h, src_w):
    poly = np.asarray(polygon, dtype=np.float32)
    mid_y = 0.5 * (poly[:, 1].min() + poly[:, 1].max())
    top_half = poly[poly[:, 1] < mid_y]
    top_x_min = int(top_half[:, 0].min())
    top_x_max = int(top_half[:, 0].max())
    poly_int = poly.astype(np.int32).reshape(-1, 1, 2)
    poly_fill = np.zeros((src_h, src_w), dtype=np.uint8)
    cv2.fillPoly(poly_fill, [poly_int], 255)
    top_y_per_x = np.full(src_w, src_h, dtype=np.int32)
    ts = top_half[np.argsort(top_half[:, 0])]
    for i in range(len(ts) - 1):
        x0, y0 = int(ts[i, 0]), int(ts[i, 1])
        x1, y1 = int(ts[i + 1, 0]), int(ts[i + 1, 1])
        if x1 == x0:
            continue
        for x in range(x0, x1 + 1):
            t = (x - x0) / (x1 - x0)
            top_y_per_x[x] = int(y0 + t * (y1 - y0))
    zone = np.full((src_h, src_w), 3, dtype=np.uint8)
    yy = np.arange(src_h).reshape(-1, 1)
    xx = np.arange(src_w).reshape(1, -1)
    poly_top_y = top_y_per_x.reshape(1, -1)
    in_polygon = poly_fill > 0
    above_polygon = yy < poly_top_y
    in_lateral_sky = (xx >= top_x_min) & (xx <= top_x_max)
    zone[in_polygon] = 2
    zone[above_polygon & in_lateral_sky & ~in_polygon] = 1
    return zone


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, required=True)
    p.add_argument(
        "--output", type=Path, default=Path(".verify/compare_polygon_blend.mp4")
    )
    p.add_argument("--polygon", type=Path, default=Path(".verify/field_polygon.json"))
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    polygon, _ = load_field(str(args.polygon))
    logging.info("loading stabilizers + building zone mask...")
    baseline = FrameStabilizer.from_json(".verify/motion.json")
    stab_sky = FrameStabilizer.from_json(".verify/motion_zone_sky.json")
    stab_field = FrameStabilizer.from_json(".verify/motion_zone_field.json")
    stab_near = FrameStabilizer.from_json(".verify/motion_zone_near.json")
    zone = build_zone_mask(polygon, 2160, 7680)
    iy, ix = stab_sky.safe_inset
    out_h, out_w = stab_sky.output_shape
    zone_crop = zone[iy : iy + out_h, ix : ix + out_w]
    mask_sky = (zone_crop == 1)[..., None]
    mask_field = (zone_crop == 2)[..., None]

    def polygon_blend(rgb, n):
        s = stab_sky.apply(rgb, n)
        f = stab_field.apply(rgb, n)
        nr = stab_near.apply(rgb, n)
        return np.where(mask_sky, s, np.where(mask_field, f, nr))

    logging.info("output %dx%d → %s", OUT_W, OUT_H, args.output)
    with av.open(str(args.input)) as in_c:
        in_v = in_c.streams.video[0]
        fps = in_v.average_rate or Fraction(20, 1)
        out_c = av.open(str(args.output), mode="w")
        out_s = out_c.add_stream("h264", rate=fps)
        out_s.width = OUT_W
        out_s.height = OUT_H
        out_s.pix_fmt = "yuv420p"
        out_s.codec_context.time_base = in_v.time_base
        out_s.codec_context.bit_rate = 10_000_000
        out_s.options = {"maxrate": "10M", "bufsize": "20M"}
        n = 0
        try:
            for packet in in_c.demux(in_v):
                if packet.dts is None:
                    continue
                for frame in packet.decode():
                    rgb = frame.to_ndarray(format="rgb24")
                    p_orig = fit_panel(rgb)
                    p_base = fit_panel(baseline.apply(rgb, n))
                    p_blend = fit_panel(polygon_blend(rgb, n))
                    stacked = np.vstack(
                        [
                            label("Original (raw)"),
                            p_orig,
                            np.full((DIVIDER_PX, OUT_W, 3), 80, dtype=np.uint8),
                            label("Baseline (current production grid-similarity)"),
                            p_base,
                            np.full((DIVIDER_PX, OUT_W, 3), 80, dtype=np.uint8),
                            label("Polygon-zone blend (new: sky/field/near per pixel)"),
                            p_blend,
                        ]
                    )
                    vf = av.VideoFrame.from_ndarray(stacked, format="rgb24")
                    vf.pts = frame.pts
                    for pkt in out_s.encode(vf):
                        out_c.mux(pkt)
                    n += 1
                    if n % 100 == 0:
                        logging.info("...frame %d", n)
            for pkt in out_s.encode():
                out_c.mux(pkt)
        finally:
            out_c.close()
        logging.info("done — %d frames", n)


if __name__ == "__main__":
    sys.exit(main())
