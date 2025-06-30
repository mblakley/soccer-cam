import os
import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

ffmpeg_lock = asyncio.Lock()


async def verify_ffmpeg_install() -> bool:
    """Verify that FFmpeg is installed and accessible."""
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate()
        return process.returncode == 0
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

        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await process.communicate()

        if process.returncode != 0:
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
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await process.wait()
    except Exception as e:
        logger.error(f"FFmpeg command failed: {e}")


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

    output_path = file_path.replace(".dav", ".mp4")

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        file_path,
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        output_path,
    ]

    logger.info(f"Running ffmpeg command: {' '.join(cmd)}")

    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )

    await process.wait()

    if process.returncode == 0:
        logger.info(
            f"Successfully converted {os.path.basename(file_path)} to {os.path.basename(output_path)}"
        )
        return output_path
    else:
        logger.error(f"Failed to convert {os.path.basename(file_path)}")
        return None


async def create_screenshot(
    video_path: str, output_path: str, time_offset: str = "00:00:01"
) -> bool:
    """
    Creates a screenshot from a video file using ffmpeg.

    Args:
        video_path: Path to the input video file.
        output_path: Path to save the output screenshot.
        time_offset: Time in seconds to take the screenshot from.

    Returns:
        True if successful, False otherwise.
    """
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
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    await process.wait()
    if process.returncode == 0:
        logger.info(
            f"Successfully created screenshot for {os.path.basename(video_path)}"
        )
        return True
    else:
        logger.error(f"Failed to create screenshot for {os.path.basename(video_path)}")
        return False


async def trim_video(
    input_path: str, output_path: str, start_offset: str, duration: Optional[str] = None
) -> bool:
    """
    Trims a video file using ffmpeg.

    Args:
        input_path: Path to the input video file.
        output_path: Path to save the output trimmed video.
        start_offset: The start time for the trim (e.g., "00:00:10").
        duration: The duration of the trim (e.g., "00:05:00").

    Returns:
        True if successful, False otherwise.
    """
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        input_path,
        "-ss",
        start_offset,
    ]

    if duration:
        cmd.extend(["-t", duration])

    cmd.extend(["-c", "copy", output_path])

    logger.info(f"Running ffmpeg trim command: {' '.join(cmd)}")
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    await process.wait()

    if process.returncode == 0:
        logger.info(
            f"Successfully trimmed {os.path.basename(input_path)} to {os.path.basename(output_path)}"
        )
        return True
    else:
        logger.error(f"Failed to trim {os.path.basename(input_path)}")
        return False
