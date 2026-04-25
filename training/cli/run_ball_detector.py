"""Run external ball detection from the command line.

Usage:
    # Detect on a single video segment
    python -m training.cli.run_ball_detector \\
        --video "F:/training_data/temp_video/<segment>.mp4" \\
        --output detections.json \\
        --model "F:/models/<ball-model>.onnx"

    # Detect on a segment and convert to per-tile YOLO labels
    python -m training.cli.run_ball_detector \\
        --video "F:/path/to/video.mp4" \\
        --output detections.json \\
        --model "F:/models/<ball-model>.onnx" \\
        --labels-dir "F:/training_data/labels_640_ext/game_name" \\
        --segment-name "<segment>"
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from video_grouper.inference.ball_detector import (
    CONF_THRESHOLD,
    create_session,
    detect_video,
    pano_to_tile,
)

logger = logging.getLogger(__name__)


def save_tile_labels(
    detections: list[dict], labels_dir: Path, segment_name: str
) -> None:
    """Convert panoramic detections to per-tile YOLO label files."""
    labels_dir.mkdir(parents=True, exist_ok=True)

    by_frame: dict[int, list[dict]] = {}
    for d in detections:
        by_frame.setdefault(d["frame_idx"], []).append(d)

    files_written = 0
    labels_written = 0

    for frame_idx, frame_dets in sorted(by_frame.items()):
        tile_labels: dict[tuple[int, int], list[str]] = {}
        for det in frame_dets:
            for tl in pano_to_tile(det["cx"], det["cy"], det["w"], det["h"]):
                key = (tl["row"], tl["col"])
                line = (
                    f"0 {tl['cx_norm']:.6f} {tl['cy_norm']:.6f} "
                    f"{tl['w_norm']:.6f} {tl['h_norm']:.6f}"
                )
                tile_labels.setdefault(key, []).append(line)

        for (row, col), lines in tile_labels.items():
            fname = f"{segment_name}_frame_{frame_idx:06d}_r{row}_c{col}.txt"
            with open(labels_dir / fname, "w") as f:
                for line in lines:
                    f.write(line + "\n")
            files_written += 1
            labels_written += len(lines)

    logger.info(
        "Wrote %d label files (%d labels) to %s",
        files_written,
        labels_written,
        labels_dir,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run external ball detection")
    parser.add_argument("--video", type=Path, required=True, help="Input video")
    parser.add_argument("--output", type=Path, default=None, help="Output JSON")
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--frame-interval", type=int, default=8)
    parser.add_argument("--conf", type=float, default=CONF_THRESHOLD)
    parser.add_argument(
        "--labels-dir",
        type=Path,
        default=None,
        help="Output dir for per-tile YOLO labels",
    )
    parser.add_argument(
        "--segment-name",
        type=str,
        default=None,
        help="Segment name prefix for label filenames",
    )
    parser.add_argument("--cpu", action="store_true", help="Force CPU inference")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    sess = create_session(args.model, use_gpu=not args.cpu)
    detections = detect_video(args.video, sess, args.frame_interval, args.conf)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w") as f:
            json.dump(detections, f)
        logger.info("Saved %d detections to %s", len(detections), args.output)

    if args.labels_dir and args.segment_name:
        save_tile_labels(detections, args.labels_dir, args.segment_name)


if __name__ == "__main__":
    main()
