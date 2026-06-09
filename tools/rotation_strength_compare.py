"""Render a vertical-stack comparison with rotation-only strength scaling.

Translation stays at 100% (the center already looks good per user). Only the
rotation component of M is scaled. Variants: 100% / 150% / 200% / 300% /
400% / 500%. Lets the user pick how aggressively to boost the rotation
correction so the edges stabilise without disturbing the center.

Reuses the production motion.json. Decomposes each per-frame M into a
SimilarityTransform, scales only the theta-residual (NOT the translation),
recomposes, applies via warpAffine. Output: ``.verify/rot_strength.mp4``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sys
from fractions import Fraction
from pathlib import Path

import av
import cv2
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from video_grouper.inference.stabilization import SimilarityTransform  # noqa: E402
from video_grouper.pipeline import create_step  # noqa: E402
from video_grouper.pipeline.base import StepContext  # noqa: E402
from video_grouper.pipeline.manifest import PipelineManifest  # noqa: E402
import video_grouper.pipeline.register_steps  # noqa: F401, E402

PANEL_W = 1920
PANEL_H = 360
LABEL_H = 32
DIVIDER_PX = 2

# Rotation-only strength variants. Translation stays at 1.0 across all.
ROT_STRENGTHS = [
    ("Original (raw)", None),
    ("100% trans, 100% rot — current production", 1.00),
    ("100% trans, 150% rot", 1.50),
    ("100% trans, 200% rot", 2.00),
    ("100% trans, 300% rot", 3.00),
    ("100% trans, 400% rot", 4.00),
    ("100% trans, 500% rot — strong over-rotation", 5.00),
]


def fit_panel(rgb: np.ndarray) -> np.ndarray:
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


def apply_rotation_scaled(
    rgb: np.ndarray,
    motion: dict,
    frame_idx: int,
    rot_strength: float,
) -> np.ndarray:
    """Apply M[frame_idx] with rotation component scaled by rot_strength.

    M is built by compose_stabilizing_transforms as ``residual_inv ∘ t_inset``,
    so its theta IS the per-frame rotation correction (the rotation we want
    to scale). Translation residual = M_t - inset_t; we keep that at 100%.
    """
    iy, ix = motion["safe_inset"]
    out_h, out_w = motion["output_size"]
    M_raw = np.array(motion["frames"][frame_idx]["M"], dtype=np.float32)
    # Decompose to (tx, ty, theta, log_scale)
    sim = SimilarityTransform.from_affine(M_raw)
    # Extract residual translation (subtract inset)
    res_tx = sim.tx - ix
    res_ty = sim.ty - iy
    # Scale rotation; keep translation residual at 100%
    new_theta = sim.theta * rot_strength
    # Rebuild: residual(scaled rotation, original translation) then add inset
    s = math.exp(sim.log_scale)
    c, sn = math.cos(new_theta), math.sin(new_theta)
    M = np.array(
        [
            [s * c, -s * sn, res_tx + ix],
            [s * sn, s * c, res_ty + iy],
        ],
        dtype=np.float32,
    )
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
    parser.add_argument("--output", type=Path, default=Path(".verify/rot_strength.mp4"))
    parser.add_argument("--work-dir", type=Path, default=Path(".verify"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    motion_path = args.work_dir / "motion.json"
    if not motion_path.exists():
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
    n_motion_frames = len(motion["frames"])
    logging.info(
        "motion.json: src %s, output %s, %d frames",
        motion["src_size"],
        motion["output_size"],
        n_motion_frames,
    )

    n_panels = len(ROT_STRENGTHS)
    OUT_W = PANEL_W
    OUT_H = n_panels * PANEL_H + n_panels * LABEL_H + (n_panels - 1) * DIVIDER_PX
    logging.info("encoding %dx%d to %s", OUT_W, OUT_H, args.output)

    with av.open(str(args.input)) as in_container:
        in_video = in_container.streams.video[0]
        fps = in_video.average_rate or Fraction(20, 1)
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
                    for label, rot in ROT_STRENGTHS:
                        panels.append(label_strip(label))
                        if rot is None:
                            panels.append(fit_panel(rgb))
                        else:
                            mi = min(frame_idx, n_motion_frames - 1)
                            stab = apply_rotation_scaled(rgb, motion, mi, rot)
                            panels.append(fit_panel(stab))
                    rows = []
                    for i, p in enumerate(panels):
                        rows.append(p)
                        if i % 2 == 1 and i // 2 < n_panels - 1:
                            rows.append(
                                np.full((DIVIDER_PX, PANEL_W, 3), 80, dtype=np.uint8)
                            )
                    stacked = np.vstack(rows)
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
