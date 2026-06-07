"""Side-by-side stabilization comparison tool.

One-off verification utility (not part of the production pipeline) per the
approved plan's verification section. Runs the ``stabilize`` step on a short
slice of real footage and composes a vertical-stack comparison video where
the top panel is the original (wobbling) source and the bottom panel is
the stabilized output. Each panel is downscaled to 1920x1080 with the
panorama centred and lightly cropped to a 16:9 broadcast aspect, so the
horizon-level / wobble comparison is visually readable.

The wobble is in the source itself — applying the stabilizer's per-frame
warpAffine to the source frames (BEFORE any downstream cylindrical warp +
crop) is the cleanest verification that the analysis pass is doing its
job. The downstream render step's behaviour with `render_stabilize=True`
is exercised separately by the pipeline integration tests.

Usage:
    python tools/stabilization_compare.py \\
        --input .verify/stabilize_test_30s.mp4 \\
        --output .verify/compare_stabilization.mp4
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

from video_grouper.inference.stabilization import FrameStabilizer  # noqa: E402
from video_grouper.pipeline import create_step  # noqa: E402
from video_grouper.pipeline.base import StepContext  # noqa: E402
from video_grouper.pipeline.manifest import PipelineManifest  # noqa: E402
import video_grouper.pipeline.register_steps  # noqa: F401, E402

PANEL_W, PANEL_H = 1920, 1080
DIVIDER_PX = 2
OUT_W = 1920
OUT_H = 2 * PANEL_H + DIVIDER_PX
LABEL_BG = (32, 32, 32)
LABEL_FG = (240, 240, 240)
LABEL_HEIGHT = 48


def _label_panel(img: np.ndarray, text: str) -> None:
    """Burn a small label strip at the top of *img* (in-place)."""
    h, w = img.shape[:2]
    cv2.rectangle(img, (0, 0), (w, LABEL_HEIGHT), LABEL_BG, -1)
    cv2.putText(
        img,
        text,
        (16, 32),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.0,
        LABEL_FG,
        2,
        cv2.LINE_AA,
    )


def _fit_panel(rgb: np.ndarray) -> np.ndarray:
    """Downscale ``rgb`` to (PANEL_H, PANEL_W) preserving aspect via letterbox.

    For 7680x2160 source, output is 1920x540 letterboxed inside 1920x1080,
    so the eye sees the full 21:6 panoramic field of view rather than a
    cropped 16:9 slice — the horizon-tilt comparison reads cleanly.
    """
    src_h, src_w = rgb.shape[:2]
    panel = np.zeros((PANEL_H, PANEL_W, 3), dtype=np.uint8)
    # Aspect-preserving fit inside the panel.
    scale = min(PANEL_W / src_w, PANEL_H / src_h)
    fit_w = int(src_w * scale)
    fit_h = int(src_h * scale)
    resized = cv2.resize(rgb, (fit_w, fit_h), interpolation=cv2.INTER_AREA)
    y0 = (PANEL_H - fit_h) // 2
    x0 = (PANEL_W - fit_w) // 2
    panel[y0 : y0 + fit_h, x0 : x0 + fit_w] = resized
    return panel


def run_stabilize_step(input_path: Path, work_dir: Path) -> tuple[Path, dict]:
    """Run StabilizeStep on *input_path*; return (motion_path, summary)."""
    work_dir.mkdir(parents=True, exist_ok=True)
    manifest = PipelineManifest.load_or_init(
        work_dir, str(input_path), str(work_dir / "unused_output.mp4")
    )
    manifest.put("input_path", str(input_path))

    step = create_step(
        "stabilize",
        {
            # Permissive budgets — first time through, we want to see what the
            # actual wobble looks like before clamping it.
            "stabilize_max_tx_px": 80.0,
            "stabilize_max_ty_px": 80.0,
            "stabilize_max_rotation_deg": 1.0,
        },
    )
    ctx = StepContext(group_dir=work_dir, team_name=None, storage_path=work_dir)
    ok = asyncio.run(step.run(manifest, ctx))
    if not ok:
        raise RuntimeError("StabilizeStep returned False")
    motion_path = Path(manifest.get("motion_path"))

    with open(motion_path, encoding="utf-8") as f:
        payload = json.load(f)
    summary = {
        "src_size": payload["src_size"],
        "output_size": payload["output_size"],
        "safe_inset": payload["safe_inset"],
        "n_frames": len(payload["frames"]),
        "mean_confidence": (
            float(np.mean([fr["confidence"] for fr in payload["frames"]]))
        ),
        "peak_translation_px": float(
            max(abs(fr["M"][0][2]) + abs(fr["M"][1][2]) for fr in payload["frames"])
        ),
    }
    return motion_path, summary


def compose_comparison(
    input_path: Path,
    motion_path: Path,
    output_path: Path,
) -> None:
    """Decode *input_path*, apply stabilizer to each frame in memory, and
    encode the side-by-side comparison to *output_path*."""
    stabilizer = FrameStabilizer.from_json(motion_path)

    with av.open(str(input_path)) as in_container:
        in_video = in_container.streams.video[0]
        src_w = in_video.width
        src_h = in_video.height
        fps = in_video.average_rate or Fraction(20, 1)

        logging.info(
            "compose: src %dx%d @ %s fps; stabilized %s",
            src_w,
            src_h,
            float(fps),
            stabilizer.output_shape,
        )

        out_container = av.open(str(output_path), mode="w")
        out_stream = out_container.add_stream("h264", rate=fps)
        out_stream.width = OUT_W
        out_stream.height = OUT_H
        out_stream.pix_fmt = "yuv420p"
        out_stream.codec_context.time_base = in_video.time_base
        out_stream.codec_context.bit_rate = 8_000_000
        out_stream.options = {"maxrate": "8M", "bufsize": "16M"}

        frame_idx = 0
        try:
            for packet in in_container.demux(in_video):
                if packet.dts is None:
                    continue
                for frame in packet.decode():
                    rgb = frame.to_ndarray(format="rgb24")
                    # Top panel: raw source — show the wobble.
                    top = _fit_panel(rgb)
                    _label_panel(top, "Original (wobbling)")
                    # Bottom panel: stabilized source — show the cancellation.
                    stabilized = stabilizer.apply(rgb, frame_idx)
                    bot = _fit_panel(stabilized)
                    _label_panel(bot, "Stabilized")
                    # Vertical stack with a 2-px divider between panels.
                    stacked = np.zeros((OUT_H, OUT_W, 3), dtype=np.uint8)
                    stacked[:PANEL_H] = top
                    stacked[PANEL_H : PANEL_H + DIVIDER_PX] = LABEL_FG
                    stacked[PANEL_H + DIVIDER_PX : PANEL_H + DIVIDER_PX + PANEL_H] = bot
                    new_frame = av.VideoFrame.from_ndarray(stacked, format="rgb24")
                    new_frame.pts = frame.pts
                    for pkt in out_stream.encode(new_frame):
                        out_container.mux(pkt)
                    frame_idx += 1
            for pkt in out_stream.encode():
                out_container.mux(pkt)
        finally:
            out_container.close()
        logging.info("compose: wrote %d frames to %s", frame_idx, output_path)


def plot_residuals(motion_path: Path, output_png: Path) -> None:
    """Render the per-frame stabilizing residuals (vertical, horizontal,
    roll) as a stacked OpenCV strip plot. Visible documentation of what
    motion the L1 LP is actually canceling on real footage."""
    with open(motion_path, encoding="utf-8") as f:
        data = json.load(f)
    iy, ix = data["safe_inset"]
    txs, tys, thetas = [], [], []
    for fr in data["frames"]:
        M = fr["M"]
        a, b = M[0][0], M[1][0]
        txs.append(M[0][2] - ix)
        tys.append(M[1][2] - iy)
        thetas.append(math.degrees(math.atan2(b, a)))
    txs_a = np.asarray(txs)
    tys_a = np.asarray(tys)
    thetas_a = np.asarray(thetas)

    def strip(
        values: np.ndarray, title: str, w: int = 1200, h: int = 240
    ) -> np.ndarray:
        img = np.full((h, w, 3), 250, dtype=np.uint8)
        cv2.putText(
            img,
            title,
            (10, 20),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (40, 40, 40),
            1,
            cv2.LINE_AA,
        )
        if values.size == 0:
            return img
        a = max(abs(values.max()), abs(values.min())) * 1.15
        if a == 0:
            a = 1.0
        ymin, ymax = -a, a
        cv2.line(img, (60, h - 30), (w - 10, h - 30), (180, 180, 180), 1)
        cv2.line(img, (60, 30), (60, h - 30), (180, 180, 180), 1)
        zero_y = int(h - 30 - (0 - ymin) / (ymax - ymin) * (h - 60))
        cv2.line(img, (60, zero_y), (w - 10, zero_y), (220, 220, 220), 1)
        cv2.putText(
            img, f"{ymax:+.1f}", (5, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (60, 60, 60), 1
        )
        cv2.putText(
            img,
            f"{ymin:+.1f}",
            (5, h - 25),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.4,
            (60, 60, 60),
            1,
        )
        cv2.putText(
            img, "0", (45, zero_y + 4), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (60, 60, 60), 1
        )
        n = len(values)
        prev = None
        for i, v in enumerate(values):
            x = int(60 + (w - 70) * i / max(1, n - 1))
            y = int(h - 30 - (v - ymin) / (ymax - ymin) * (h - 60))
            y = max(30, min(h - 30, y))
            if prev is not None:
                cv2.line(img, prev, (x, y), (40, 40, 200), 1, cv2.LINE_AA)
            prev = (x, y)
        return img

    p1 = strip(
        tys_a, "Per-frame VERTICAL wobble residual (px) — what the L1 LP is canceling"
    )
    p2 = strip(txs_a, "Per-frame HORIZONTAL wobble residual (px)")
    p3 = strip(thetas_a, "Per-frame ROLL wobble residual (deg)")
    banner = np.full((60, p1.shape[1], 3), 32, dtype=np.uint8)
    cv2.putText(
        banner,
        "Stabilization residual (per-frame) — peaks = wobble being canceled",
        (10, 35),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.6,
        (240, 240, 240),
        1,
        cv2.LINE_AA,
    )
    stacked = np.vstack([banner, p1, p2, p3])
    cv2.imwrite(str(output_png), stacked)


def save_keyframes(
    output_path: Path,
    keyframe_dir: Path,
    fractions: tuple[float, ...] = (0.05, 0.35, 0.65, 0.95),
) -> list[Path]:
    """Extract a few keyframe PNGs from the composed comparison video."""
    keyframe_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    with av.open(str(output_path)) as container:
        stream = container.streams.video[0]
        total = stream.frames or 0
        if total <= 0:
            # No metadata frame count — count manually.
            for _ in container.demux(stream):
                pass
            container.seek(0)
            stream = container.streams.video[0]
            total = stream.frames or 600

    cap = cv2.VideoCapture(str(output_path))
    try:
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or total
        for f in fractions:
            idx = int(total_frames * f)
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ok, frame = cap.read()
            if not ok:
                continue
            out_p = keyframe_dir / f"frame_{int(f * 100):02d}pct.png"
            cv2.imwrite(str(out_p), frame)
            paths.append(out_p)
    finally:
        cap.release()
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="Path to the wobbling source video slice.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".verify/compare_stabilization.mp4"),
        help="Path to the composed side-by-side mp4.",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path(".verify"),
        help="Working directory for motion.json + keyframes.",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    args.work_dir.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    cached_motion = args.work_dir / "motion.json"
    if cached_motion.exists():
        logging.info("=== Using cached motion.json at %s ===", cached_motion)
        with open(cached_motion, encoding="utf-8") as f:
            payload = json.load(f)
        motion_path = cached_motion
        summary = {
            "src_size": payload["src_size"],
            "output_size": payload["output_size"],
            "safe_inset": payload["safe_inset"],
            "n_frames": len(payload["frames"]),
        }
    else:
        logging.info("=== Stage A+B: running StabilizeStep on %s ===", args.input)
        motion_path, summary = run_stabilize_step(args.input, args.work_dir)
    logging.info("stabilize summary:")
    for k, v in summary.items():
        logging.info("  %s: %s", k, v)

    logging.info("=== Composing side-by-side video ===")
    compose_comparison(args.input, motion_path, args.output)

    logging.info("=== Saving keyframes ===")
    keyframe_dir = args.work_dir / "keyframes"
    paths = save_keyframes(args.output, keyframe_dir)
    for p in paths:
        logging.info("  %s", p)

    logging.info("=== Plotting wobble residuals ===")
    residual_png = args.work_dir / "wobble_residual.png"
    plot_residuals(motion_path, residual_png)
    logging.info("  %s", residual_png)

    logging.info("Done. Side-by-side: %s", args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
