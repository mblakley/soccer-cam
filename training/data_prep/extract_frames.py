"""Extract frames from panoramic video files for annotation and training.

Uses PyAV (ffmpeg bindings) for robust H.264 decoding — corrupt frames
are skipped instead of crashing the process. Falls back to cv2 if av
is not installed.
"""

import logging
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SEC = 2.0
DEFAULT_DIFF_THRESHOLD = 5.0
DEFAULT_JPEG_QUALITY = 95

try:
    import av
    HAS_AV = True
except ImportError:
    HAS_AV = False


def extract_frames(
    video_path: Path,
    output_dir: Path,
    interval_sec: float = DEFAULT_INTERVAL_SEC,
    diff_threshold: float = DEFAULT_DIFF_THRESHOLD,
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
    frame_interval: int | None = None,
    flip: bool = False,
) -> int:
    """Extract frames from a video file at regular intervals."""
    if HAS_AV:
        return _extract_frames_av(
            video_path, output_dir, interval_sec, diff_threshold,
            jpeg_quality, frame_interval, flip,
        )
    return _extract_frames_cv2(
        video_path, output_dir, interval_sec, diff_threshold,
        jpeg_quality, frame_interval, flip,
    )


def _extract_frames_av(
    video_path: Path,
    output_dir: Path,
    interval_sec: float,
    diff_threshold: float,
    jpeg_quality: int,
    frame_interval: int | None,
    flip: bool,
) -> int:
    """Extract frames using PyAV (ffmpeg). Robust against corrupt frames."""
    container = av.open(str(video_path))
    stream = container.streams.video[0]
    fps = float(stream.average_rate or 25)
    total_frames = stream.frames or 0

    if frame_interval is None:
        frame_interval = max(1, int(fps * interval_sec))

    output_dir.mkdir(parents=True, exist_ok=True)
    video_name = video_path.stem

    logger.info(
        "Extracting frames from %s (%.1f fps, %d total, every %d frames) [PyAV]",
        video_path.name, fps, total_frames, frame_interval,
    )

    prev_frame = None
    extracted = 0
    frame_idx = 0

    stream.thread_type = "AUTO"
    for packet in container.demux(stream):
        try:
            for av_frame in packet.decode():
                if frame_idx % frame_interval == 0:
                    frame = av_frame.to_ndarray(format="bgr24")

                    if flip:
                        frame = cv2.flip(frame, -1)

                    if prev_frame is not None:
                        try:
                            diff = np.mean(
                                np.abs(frame.astype(np.float32) - prev_frame.astype(np.float32))
                            )
                        except (MemoryError, ValueError):
                            frame_idx += 1
                            continue
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
                            extracted, frame_idx, total_frames,
                        )

                frame_idx += 1
        except av.error.InvalidDataError:
            continue
        except Exception as e:
            logger.debug("Skipping corrupt frame at %d: %s", frame_idx, e)
            continue

    container.close()
    logger.info("Extracted %d frames from %s", extracted, video_path.name)
    return extracted


def _extract_frames_cv2(
    video_path: Path,
    output_dir: Path,
    interval_sec: float,
    diff_threshold: float,
    jpeg_quality: int,
    frame_interval: int | None,
    flip: bool,
) -> int:
    """Extract frames using cv2. Fallback if PyAV not available."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise ValueError(f"Cannot open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if frame_interval is None:
        frame_interval = max(1, int(fps * interval_sec))

    output_dir.mkdir(parents=True, exist_ok=True)
    video_name = video_path.stem

    logger.info(
        "Extracting frames from %s (%.1f fps, %d total, every %d frames) [cv2]",
        video_path.name, fps, total_frames, frame_interval,
    )

    prev_frame = None
    extracted = 0
    frame_idx = 0

    while True:
        try:
            ret, frame = cap.read()
        except Exception:
            frame_idx += 1
            continue
        if not ret:
            break
        if frame is None:
            frame_idx += 1
            continue

        if frame_idx % frame_interval == 0:
            if flip:
                frame = cv2.flip(frame, -1)

            if prev_frame is not None:
                try:
                    diff = np.mean(
                        np.abs(frame.astype(np.float32) - prev_frame.astype(np.float32))
                    )
                except (MemoryError, ValueError):
                    frame_idx += 1
                    continue
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
                    extracted, frame_idx, total_frames,
                )

        frame_idx += 1

    cap.release()
    logger.info("Extracted %d frames from %s", extracted, video_path.name)
    return extracted
