"""Extract frames from panoramic video files for annotation and training.

Extracts frames at configurable intervals, skipping near-identical frames
via pixel differencing. Outputs full-res 4096x1800 JPEGs.
"""

import argparse
import logging
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SEC = 2.0
DEFAULT_DIFF_THRESHOLD = 5.0  # mean absolute pixel difference to consider "different"
DEFAULT_JPEG_QUALITY = 95


def extract_frames(
    video_path: Path,
    output_dir: Path,
    interval_sec: float = DEFAULT_INTERVAL_SEC,
    diff_threshold: float = DEFAULT_DIFF_THRESHOLD,
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
) -> int:
    """Extract frames from a video file at regular intervals.

    Args:
        video_path: Path to the input video (.mp4)
        output_dir: Directory to write extracted frames
        interval_sec: Seconds between extracted frames
        diff_threshold: Minimum mean absolute pixel difference to keep a frame
        jpeg_quality: JPEG compression quality (0-100)

    Returns:
        Number of frames extracted
    """
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise ValueError(f"Cannot open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    frame_interval = max(1, int(fps * interval_sec))

    output_dir.mkdir(parents=True, exist_ok=True)
    video_name = video_path.stem

    logger.info(
        "Extracting frames from %s (%.1f fps, %d total, every %d frames)",
        video_path.name,
        fps,
        total_frames,
        frame_interval,
    )

    prev_frame = None
    extracted = 0
    frame_idx = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        if frame_idx % frame_interval == 0:
            if prev_frame is not None:
                diff = np.mean(
                    np.abs(frame.astype(np.float32) - prev_frame.astype(np.float32))
                )
                if diff < diff_threshold:
                    frame_idx += 1
                    continue

            out_path = output_dir / f"{video_name}_frame_{frame_idx:06d}.jpg"
            cv2.imwrite(str(out_path), frame, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
            prev_frame = frame.copy()
            extracted += 1

            if extracted % 100 == 0:
                logger.info(
                    "Extracted %d frames so far (at frame %d/%d)",
                    extracted,
                    frame_idx,
                    total_frames,
                )

        frame_idx += 1

    cap.release()
    logger.info("Extracted %d frames from %s", extracted, video_path.name)
    return extracted


def main():
    parser = argparse.ArgumentParser(
        description="Extract frames from panoramic video for training"
    )
    parser.add_argument(
        "video", type=Path, help="Input video file or directory of videos"
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("training/data/raw_frames"),
        help="Output directory",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_SEC,
        help="Seconds between frames",
    )
    parser.add_argument(
        "--diff-threshold",
        type=float,
        default=DEFAULT_DIFF_THRESHOLD,
        help="Min pixel diff to keep",
    )
    parser.add_argument(
        "--quality", type=int, default=DEFAULT_JPEG_QUALITY, help="JPEG quality (0-100)"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    video_path = args.video
    if video_path.is_dir():
        videos = sorted(video_path.glob("*.mp4")) + sorted(video_path.glob("*.dav"))
        logger.info("Found %d video files in %s", len(videos), video_path)
    else:
        videos = [video_path]

    total = 0
    for v in videos:
        total += extract_frames(
            v, args.output / v.stem, args.interval, args.diff_threshold, args.quality
        )

    logger.info("Total frames extracted: %d", total)


if __name__ == "__main__":
    main()
