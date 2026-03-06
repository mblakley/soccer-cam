"""Generate test file metadata for camera emulator.

Produces 6 files in 2 groups of 3 (10-second gap between groups),
timestamped 12 hours ago. Same logic as SimulatorCamera but decoupled
from the Camera class so the HTTP emulator can reuse it.
"""

import os
import logging
from datetime import datetime, timedelta

import pytz

logger = logging.getLogger(__name__)


def generate_test_files(clips_dir: str = "/clips"):
    """Generate metadata for 6 test video files in 2 groups.

    Returns a list of dicts with keys:
        filename, start_time, end_time, size, clip_path
    """
    utc_now = datetime.now(pytz.utc)
    base_time = utc_now - timedelta(hours=12)
    file_duration = 60  # seconds

    # Find available clip files
    clip_files = (
        sorted(
            [
                os.path.join(clips_dir, f)
                for f in os.listdir(clips_dir)
                if f.endswith(".mp4")
            ]
        )
        if os.path.isdir(clips_dir)
        else []
    )

    files = []
    current_time = base_time

    for i in range(6):
        # 10-second gap between group 1 (0-2) and group 2 (3-5)
        if i == 3:
            current_time = current_time + timedelta(seconds=10)

        start_time = current_time
        end_time = current_time + timedelta(seconds=file_duration)

        start_str = start_time.strftime("%H.%M.%S")
        end_str = end_time.strftime("%H.%M.%S")
        filename = f"{start_str}-{end_str}[F][0@0][{134510 + i}].dav"

        clip_path = clip_files[i % len(clip_files)] if clip_files else None
        size = os.path.getsize(clip_path) if clip_path else 1024 * 1024 * 119

        files.append(
            {
                "filename": filename,
                "start_time": start_time,
                "end_time": end_time,
                "size": size,
                "clip_path": clip_path,
            }
        )
        current_time = end_time

    logger.info(
        f"Generated {len(files)} test file entries ({len(clip_files)} clips available)"
    )
    return files
