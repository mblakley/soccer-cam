"""Render a vertical-stack comparison video with 5 stabilization strength
levels — so the user can subjectively pick the right amount of correction.

Reuses motion.json produced by the production StabilizeStep but applies a
per-strength scaling factor to the residual: 0% = no correction, 100% =
production current, 150% = over-corrected. The same source frame is warped
five times in parallel and stacked vertically.

Output: ``.verify/strength_compare.mp4`` — 1920×~2150 (5 panels at 1920×420
each, plus labels).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from fractions import Fraction
from pathlib import Path

import av
import cv2
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from video_grouper.pipeline import create_step  # noqa: E402
from video_grouper.pipeline.base import StepContext  # noqa: E402
from video_grouper.pipeline.manifest import PipelineManifest  # noqa: E402
import video_grouper.pipeline.register_steps  # noqa: F401, E402

# Panel layout: 7 panels stacked vertically. Each panel preserves source's
# 21:6 aspect (downscaled to 1920 wide → 540 tall natively, trimmed to 360
# so 7 panels + labels fit reasonably tall on a monitor).
PANEL_W = 1920
PANEL_H = 360
LABEL_H = 32
DIVIDER_PX = 2

STRENGTHS = [
    ("0% — Original (no stabilization)", 0.0),
    ("50% — half correction", 0.50),
    ("100% — current production", 1.00),
    ("150%", 1.50),
    ("200%", 2.00),
    ("300%", 3.00),
    ("400% — strong over-correction", 4.00),
]


def fit_panel(rgb: np.ndarray) -> np.ndarray:
    """Downscale source RGB to PANEL_W×PANEL_H, aspect-preserving + letterbox."""
    src_h, src_w = rgb.shape[:2]
    panel = np.zeros((PANEL_H, PANEL_W, 3), dtype=np.uint8)
    scale = min(PANEL_W / src_w, PANEL_H / src_h)
    fit_w = int(src_w * scale)
    fit_h = int(src_h * scale)
    resized = cv2.resize(rgb, (fit_w, fit_h), interpolation=cv2.INTER_AREA)
    y0 = (PANEL_H - fit_h) // 2
    x0 = (PANEL_W - fit_w) // 2
    panel[y0 : y0 + fit_h, x0 : x0 + fit_w] = resized
    return panel


def label_strip(text: str) -> np.ndarray:
    bar = np.full((LABEL_H, PANEL_W, 3), 30, dtype=np.uint8)
    cv2.putText(
        bar,
        text,
        (16, 26),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.65,
        (240, 240, 240),
        2,
        cv2.LINE_AA,
    )
    return bar


def apply_scaled_motion(
    rgb: np.ndarray,
    motion: dict,
    frame_idx: int,
    strength: float,
) -> np.ndarray:
    """Apply motion[frame_idx]'s M scaled by strength.

    M decomposes into a residual translation + the constant inset. We pull
    the residual out, scale it, recompose: M_scaled = scale*residual + inset.

    Strength 0 = no correction (just the inset crop). Strength 1 = full
    current production. Strength > 1 = over-correction.
    """
    iy, ix = motion["safe_inset"]
    out_h, out_w = motion["output_size"]
    M_raw = motion["frames"][frame_idx]["M"]
    # Translation: residual + inset → scaled residual + inset
    res_tx = M_raw[0][2] - ix
    res_ty = M_raw[1][2] - iy
    M = np.array(M_raw, dtype=np.float32).copy()
    M[0][2] = res_tx * strength + ix
    M[1][2] = res_ty * strength + iy
    return cv2.warpAffine(
        rgb,
        M,
        dsize=(out_w, out_h),
        flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
        borderMode=cv2.BORDER_REPLICATE,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument(
        "--polygon", type=Path, default=Path(".verify/field_polygon.json")
    )
    parser.add_argument(
        "--output", type=Path, default=Path(".verify/strength_compare.mp4")
    )
    parser.add_argument("--work-dir", type=Path, default=Path(".verify"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    # Re-use cached motion.json if it exists (from prior runs), else produce.
    motion_path = args.work_dir / "motion.json"
    if not motion_path.exists():
        logging.info("computing motion.json...")
        manifest = PipelineManifest.load_or_init(
            args.work_dir, str(args.input), str(args.work_dir / "unused.mp4")
        )
        manifest.put("input_path", str(args.input))
        if args.polygon.exists():
            manifest.put("field_polygon_path", str(args.polygon))
        step = create_step("stabilize", {})
        ctx = StepContext(
            group_dir=args.work_dir, team_name=None, storage_path=args.work_dir
        )
        asyncio.run(step.run(manifest, ctx))

    with open(motion_path, encoding="utf-8") as f:
        motion = json.load(f)
    out_h_stab, out_w_stab = motion["output_size"]
    n_motion_frames = len(motion["frames"])
    logging.info(
        "motion.json: src %s, output %s, safe_inset %s, %d frames",
        motion["src_size"],
        motion["output_size"],
        motion["safe_inset"],
        n_motion_frames,
    )

    # Output dims: N panels stacked vertically with label bars + dividers.
    n_panels = len(STRENGTHS)
    OUT_W = PANEL_W
    OUT_H = n_panels * PANEL_H + n_panels * LABEL_H + (n_panels - 1) * DIVIDER_PX

    with av.open(str(args.input)) as in_container:
        in_video = in_container.streams.video[0]
        fps = in_video.average_rate or Fraction(20, 1)
        logging.info(
            "encoding %dx%d @ %s fps to %s", OUT_W, OUT_H, float(fps), args.output
        )

        out_container = av.open(str(args.output), mode="w")
        out_stream = out_container.add_stream("h264", rate=fps)
        out_stream.width = OUT_W
        out_stream.height = OUT_H
        out_stream.pix_fmt = "yuv420p"
        out_stream.codec_context.time_base = in_video.time_base
        out_stream.codec_context.bit_rate = 12_000_000
        out_stream.options = {"maxrate": "12M", "bufsize": "24M"}

        frame_idx = 0
        try:
            for packet in in_container.demux(in_video):
                if packet.dts is None:
                    continue
                for frame in packet.decode():
                    rgb = frame.to_ndarray(format="rgb24")
                    panels = []
                    for label, strength in STRENGTHS:
                        panels.append(label_strip(label))
                        if strength == 0.0:
                            panels.append(fit_panel(rgb))
                        else:
                            mi = min(frame_idx, n_motion_frames - 1)
                            stab = apply_scaled_motion(rgb, motion, mi, strength)
                            panels.append(fit_panel(stab))
                    stacked_rows = []
                    for i, p in enumerate(panels):
                        stacked_rows.append(p)
                        # Divider after each panel-pair (label + image), except last
                        if i % 2 == 1 and i // 2 < n_panels - 1:
                            stacked_rows.append(
                                np.full((DIVIDER_PX, PANEL_W, 3), 80, dtype=np.uint8)
                            )
                    stacked = np.vstack(stacked_rows)
                    assert stacked.shape[0] == OUT_H, f"row count off: {stacked.shape}"
                    new_frame = av.VideoFrame.from_ndarray(stacked, format="rgb24")
                    new_frame.pts = frame.pts
                    for pkt in out_stream.encode(new_frame):
                        out_container.mux(pkt)
                    frame_idx += 1
            for pkt in out_stream.encode():
                out_container.mux(pkt)
        finally:
            out_container.close()
        logging.info("wrote %d frames", frame_idx)
    return 0


if __name__ == "__main__":
    sys.exit(main())
