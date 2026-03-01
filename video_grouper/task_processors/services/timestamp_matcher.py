"""
Timestamp matcher — pure functions for computing video offsets from wall-clock tags.

Given a tag's UTC timestamp and the recording file list, computes:
1. The offset into the combined video (combined.mp4)
2. The offset into the trimmed video (trimmed.mp4)
3. Clip boundaries (start/end) around the moment
"""

import logging
from datetime import datetime
from typing import Optional

from zoneinfo import ZoneInfo

from video_grouper.models import RecordingFile

logger = logging.getLogger(__name__)


def compute_combined_offset(
    tag_utc: datetime,
    recording_files: list[RecordingFile],
    camera_timezone: str = "America/New_York",
    gap_tolerance_seconds: float = 5.0,
) -> Optional[float]:
    """Compute the offset (in seconds) into combined.mp4 for a given tag timestamp.

    The combined video is a concatenation of all recording files in order.
    We find which file the tag falls into, compute the offset within that file,
    then add the cumulative duration of all preceding files.

    Args:
        tag_utc: The moment tag timestamp in UTC.
        recording_files: Ordered list of recording files (by start_time).
        camera_timezone: IANA timezone of the camera (e.g. "America/New_York").
        gap_tolerance_seconds: Max gap between files to treat as continuous.

    Returns:
        Offset in seconds from the start of combined.mp4, or None if
        the tag doesn't fall within any recording file.
    """
    if not recording_files:
        return None

    # Convert tag to camera-local time for comparison with recording file timestamps
    cam_tz = ZoneInfo(camera_timezone)
    tag_local = (
        tag_utc.astimezone(cam_tz) if tag_utc.tzinfo else tag_utc.replace(tzinfo=cam_tz)
    )

    # Sort files by start_time
    sorted_files = sorted(recording_files, key=lambda f: f.start_time)

    # Filter out skipped files (matching what combine_videos does)
    active_files = [f for f in sorted_files if not f.skip]
    if not active_files:
        return None

    cumulative_seconds = 0.0

    for i, rec in enumerate(active_files):
        file_start = _ensure_tz(rec.start_time, cam_tz)
        file_end = _ensure_tz(rec.end_time, cam_tz)
        file_duration = (file_end - file_start).total_seconds()

        if file_start <= tag_local <= file_end:
            offset_within_file = (tag_local - file_start).total_seconds()
            return cumulative_seconds + offset_within_file

        # Check gap tolerance: if the tag falls in a small gap between files,
        # snap to the start of the next file
        if i + 1 < len(active_files):
            next_start = _ensure_tz(active_files[i + 1].start_time, cam_tz)
            gap = (next_start - file_end).total_seconds()
            if 0 < gap <= gap_tolerance_seconds and file_end < tag_local < next_start:
                # Tag is in the gap — snap to start of next file
                return cumulative_seconds + file_duration

        cumulative_seconds += file_duration

    # Tag is after all files — check if it's within tolerance of the last file's end
    last = active_files[-1]
    last_end = _ensure_tz(last.end_time, cam_tz)
    if 0 <= (tag_local - last_end).total_seconds() <= gap_tolerance_seconds:
        return cumulative_seconds  # snap to end of combined

    logger.warning(
        "Tag at %s does not fall within any recording file (range: %s – %s)",
        tag_local,
        _ensure_tz(active_files[0].start_time, cam_tz),
        last_end,
    )
    return None


def compute_trimmed_offset(
    combined_offset: float,
    start_time_offset: str,
) -> Optional[float]:
    """Compute the offset into trimmed.mp4 given a combined.mp4 offset.

    Args:
        combined_offset: Offset in seconds into combined.mp4.
        start_time_offset: The trim start time in HH:MM:SS format
            (from match_info.start_time_offset).

    Returns:
        Offset in seconds into trimmed.mp4, or None if the moment is
        before the trim start (i.e., cut away during trimming).
    """
    trim_start_seconds = _parse_time_offset(start_time_offset)
    trimmed = combined_offset - trim_start_seconds
    if trimmed < 0:
        logger.debug(
            "Tag at combined offset %.1f is before trim start (%.1f), skipping",
            combined_offset,
            trim_start_seconds,
        )
        return None
    return trimmed


def compute_clip_boundaries(
    trimmed_offset: float,
    buffer_seconds: float = 15.0,
    video_duration: Optional[float] = None,
) -> tuple[float, float]:
    """Compute clip start/end times around a moment in the trimmed video.

    The clip is centered on the moment with `buffer_seconds` before and after.

    Args:
        trimmed_offset: Offset in seconds into the trimmed video.
        buffer_seconds: Seconds of context before and after the moment.
        video_duration: Total duration of the trimmed video (for clamping).

    Returns:
        (clip_start, clip_end) in seconds.
    """
    clip_start = max(0.0, trimmed_offset - buffer_seconds)
    clip_end = trimmed_offset + buffer_seconds

    if video_duration is not None and clip_end > video_duration:
        clip_end = video_duration

    return clip_start, clip_end


def _ensure_tz(dt: datetime, tz: ZoneInfo) -> datetime:
    """Ensure a datetime has timezone info, applying tz if naive."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def _parse_time_offset(time_str: str) -> float:
    """Parse HH:MM:SS or MM:SS to total seconds."""
    if not time_str:
        return 0.0
    parts = time_str.strip().split(":")
    try:
        if len(parts) == 3:
            h, m, s = parts
            return int(h) * 3600 + int(m) * 60 + float(s)
        elif len(parts) == 2:
            m, s = parts
            return int(m) * 60 + float(s)
        else:
            return float(parts[0])
    except (ValueError, IndexError):
        logger.warning("Could not parse time offset: %s", time_str)
        return 0.0
