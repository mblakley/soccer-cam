import os
import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Default timeout for FFmpeg operations (30 minutes).
# Long videos (2+ hours of 180-degree footage) can take a while to process.
FFMPEG_TIMEOUT = 1800


async def _run_ffmpeg_with_timeout(
    cmd: list[str], timeout: int = FFMPEG_TIMEOUT
) -> tuple[int, bytes, bytes]:
    """Run an FFmpeg command with a timeout, capturing stderr for diagnostics.

    Returns (returncode, stdout_bytes, stderr_bytes).
    Kills the process on timeout or cancellation to avoid zombies.
    """
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        return process.returncode, stdout, stderr
    except (asyncio.TimeoutError, asyncio.CancelledError):
        process.kill()
        await process.wait()
        raise


async def verify_ffmpeg_install() -> bool:
    """Verify that FFmpeg is installed and accessible."""
    try:
        returncode, _, _ = await _run_ffmpeg_with_timeout(
            ["ffmpeg", "-version"], timeout=10
        )
        return returncode == 0
    except Exception as e:
        logger.error(f"Error verifying FFmpeg installation: {e}")
        return False


def get_default_date_format():
    return "%Y-%m-%d %H:%M:%S"


async def get_video_duration(file_path: str) -> Optional[float]:
    """Get the duration of a video file using ffprobe."""
    try:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            file_path,
        ]

        returncode, stdout, stderr = await _run_ffmpeg_with_timeout(cmd, timeout=60)

        if returncode != 0:
            logger.error(f"Error getting video duration: {stderr.decode()}")
            return None

        return float(stdout.decode().strip())

    except Exception as e:
        logger.error(f"Error getting video duration: {e}")
        return None


async def verify_mp4_duration(
    dav_file: str, mp4_file: str, tolerance: float = 0.1
) -> bool:
    """Verify that the MP4 file duration matches the DAV file duration."""
    try:
        if not os.path.exists(dav_file) or not os.path.exists(mp4_file):
            return False

        dav_duration = await get_video_duration(dav_file)
        mp4_duration = await get_video_duration(mp4_file)

        if dav_duration is None or mp4_duration is None:
            return False

        # Check if durations are within tolerance
        duration_diff = abs(dav_duration - mp4_duration)
        return duration_diff <= (dav_duration * tolerance)

    except Exception as e:
        logger.error(f"Error verifying MP4 duration: {e}")
        return False


async def run_ffmpeg(command):
    """Run an FFmpeg command. Returns True on success, False on failure."""
    try:
        returncode, _, stderr = await _run_ffmpeg_with_timeout(command)
        if returncode != 0:
            logger.error(f"FFmpeg command failed (rc={returncode}): {stderr.decode()}")
            return False
        return True
    except asyncio.TimeoutError:
        logger.error(f"FFmpeg command timed out after {FFMPEG_TIMEOUT}s")
        return False
    except Exception as e:
        logger.error(f"FFmpeg command failed: {e}")
        return False


async def async_convert_file(file_path: str) -> Optional[str]:
    """
    Converts a video file to MP4 format using ffmpeg.
    -i: input file
    -c:v copy: copy video stream without re-encoding
    -c:a aac: re-encode audio to aac
    -b:a 192k: set audio bitrate to 192k
    """
    if not os.path.exists(file_path):
        logger.error(f"Input file not found: {file_path}")
        return None

    # Use pathlib-style suffix replacement to handle .dav anywhere in the path
    base, ext = os.path.splitext(file_path)
    output_path = base + ".mp4" if ext.lower() == ".dav" else file_path + ".mp4"

    cmd = [
        "ffmpeg",
        "-y",
        "-fflags",
        "+genpts+discardcorrupt",
        "-i",
        file_path,
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-avoid_negative_ts",
        "make_zero",
        "-movflags",
        "+faststart",
        output_path,
    ]

    logger.info(f"Running ffmpeg command: {' '.join(cmd)}")

    try:
        returncode, _, stderr = await _run_ffmpeg_with_timeout(cmd)

        if returncode == 0:
            logger.info(
                f"Successfully converted {os.path.basename(file_path)} to {os.path.basename(output_path)}"
            )
            return output_path
        else:
            logger.error(
                f"Failed to convert {os.path.basename(file_path)}: {stderr.decode()}"
            )
            return None
    except asyncio.TimeoutError:
        logger.error(
            f"Conversion timed out for {os.path.basename(file_path)} after {FFMPEG_TIMEOUT}s"
        )
        return None


async def _run_ffmpeg_checked(
    cmd: list[str], operation: str, timeout: int = FFMPEG_TIMEOUT
) -> bool:
    """Run an FFmpeg command with standardized error handling.

    Args:
        cmd: The FFmpeg command to run.
        operation: Human-readable description for log messages.
        timeout: Timeout in seconds.

    Returns:
        True on success, False on failure.
    """
    try:
        returncode, _, stderr = await _run_ffmpeg_with_timeout(cmd, timeout=timeout)
        if returncode == 0:
            logger.info(f"Successfully {operation}")
            return True
        else:
            logger.error(f"Failed to {operation}: {stderr.decode()}")
            return False
    except asyncio.TimeoutError:
        logger.error(f"{operation} timed out after {timeout}s")
        return False
    except Exception as e:
        logger.error(f"Error during {operation}: {e}")
        return False


async def create_screenshot(
    video_path: str, output_path: str, time_offset: str = "00:00:01"
) -> bool:
    """Creates a screenshot from a video file using ffmpeg."""
    cmd = [
        "ffmpeg",
        "-ss",
        str(time_offset),
        "-i",
        video_path,
        "-vframes",
        "1",
        "-q:v",
        "2",
        output_path,
        "-y",
    ]
    return await _run_ffmpeg_checked(
        cmd,
        f"created screenshot for {os.path.basename(video_path)}",
        timeout=60,
    )


async def trim_video(
    input_path: str, output_path: str, start_offset: str, duration: Optional[str] = None
) -> bool:
    """Trims a video file using ffmpeg."""
    cmd = [
        "ffmpeg",
        "-y",
        "-fflags",
        "+genpts+discardcorrupt",
        "-i",
        input_path,
        "-ss",
        start_offset,
    ]

    if duration:
        cmd.extend(["-t", duration])

    cmd.extend(
        [
            "-c",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            "-movflags",
            "+faststart",
            output_path,
        ]
    )

    logger.info(f"Running ffmpeg trim command: {' '.join(cmd)}")
    return await _run_ffmpeg_checked(
        cmd,
        f"trimmed {os.path.basename(input_path)} to {os.path.basename(output_path)}",
    )


async def combine_videos(file_list_path: str, output_path: str) -> bool:
    """Combines multiple video files into a single MP4 using FFmpeg concat demuxer."""
    cmd = [
        "ffmpeg",
        "-y",
        "-fflags",
        "+genpts+discardcorrupt",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        file_list_path,
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-avoid_negative_ts",
        "make_zero",
        "-movflags",
        "+faststart",
        output_path,
    ]

    logger.info(f"Running ffmpeg combine command: {' '.join(cmd)}")
    return await _run_ffmpeg_checked(
        cmd,
        f"combined videos to {os.path.basename(output_path)}",
    )
