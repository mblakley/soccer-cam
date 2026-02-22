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
    try:
        returncode, _, stderr = await _run_ffmpeg_with_timeout(cmd, timeout=60)
        if returncode == 0:
            logger.info(
                f"Successfully created screenshot for {os.path.basename(video_path)}"
            )
            return True
        else:
            logger.error(
                f"Failed to create screenshot for {os.path.basename(video_path)}: {stderr.decode()}"
            )
            return False
    except asyncio.TimeoutError:
        logger.error(f"Screenshot timed out for {os.path.basename(video_path)}")
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
    try:
        returncode, _, stderr = await _run_ffmpeg_with_timeout(cmd)

        if returncode == 0:
            logger.info(
                f"Successfully trimmed {os.path.basename(input_path)} to {os.path.basename(output_path)}"
            )
            return True
        else:
            logger.error(
                f"Failed to trim {os.path.basename(input_path)}: {stderr.decode()}"
            )
            return False
    except asyncio.TimeoutError:
        logger.error(
            f"Trim timed out for {os.path.basename(input_path)} after {FFMPEG_TIMEOUT}s"
        )
        return False


async def combine_videos(file_list_path: str, output_path: str) -> bool:
    """
    Combines multiple video files into a single MP4 using FFmpeg concat demuxer.

    Args:
        file_list_path: Path to the filelist.txt containing the list of video files to combine.
        output_path: Path where the combined video will be saved.

    Returns:
        True if successful, False otherwise.
    """
    cmd = [
        "ffmpeg",
        "-y",  # Overwrite output file
        "-f",
        "concat",  # Use concat demuxer
        "-safe",
        "0",  # Allow unsafe file names
        "-i",
        file_list_path,  # Input file list
        "-c:v",
        "copy",  # Copy video stream
        "-c:a",
        "aac",  # Re-encode audio to AAC
        "-b:a",
        "192k",  # Audio bitrate
        output_path,
    ]

    logger.info(f"Running ffmpeg combine command: {' '.join(cmd)}")

    try:
        returncode, _, stderr = await _run_ffmpeg_with_timeout(cmd)

        if returncode == 0:
            logger.info(
                f"Successfully combined videos to {os.path.basename(output_path)}"
            )
            return True
        else:
            logger.error(f"Failed to combine videos: {stderr.decode()}")
            return False

    except asyncio.TimeoutError:
        logger.error(f"Combine timed out after {FFMPEG_TIMEOUT}s")
        return False
    except Exception as e:
        logger.error(f"Error combining videos: {e}")
        return False


async def trim_video_advanced(
    input_path: str, output_path: str, start_time: str, end_time: str
) -> bool:
    """
    Advanced video trimming with frame-drop removal and re-encoding.

    Args:
        input_path: Path to the input video file.
        output_path: Path to save the output trimmed video.
        start_time: The start time for the trim (e.g., "00:00:10").
        end_time: The end time for the trim (e.g., "00:05:00").

    Returns:
        True if successful, False otherwise.
    """
    cmd = [
        "ffmpeg",
        "-y",
        "-fflags",
        "+discardcorrupt",
        "-err_detect",
        "ignore_err",
        "-i",
        input_path,
        "-ss",
        start_time,
        "-to",
        end_time,
        "-vf",
        "mpdecimate,setpts=N/25/TB",
        "-r",
        "25",
        "-c:v",
        "libx264",
        "-crf",
        "18",
        "-preset",
        "slow",
        "-c:a",
        "copy",
        output_path,
    ]

    logger.info(f"Running advanced ffmpeg trim command: {' '.join(cmd)}")

    try:
        returncode, _, stderr = await _run_ffmpeg_with_timeout(cmd)

        if returncode == 0:
            logger.info(
                f"Successfully trimmed video with advanced processing to {os.path.basename(output_path)}"
            )
            return True
        else:
            logger.error(
                f"Failed to trim video with advanced processing: {stderr.decode()}"
            )
            return False

    except asyncio.TimeoutError:
        logger.error(
            f"Advanced trim timed out for {os.path.basename(input_path)} after {FFMPEG_TIMEOUT}s"
        )
        return False
    except Exception as e:
        logger.error(f"Error trimming video with advanced processing: {e}")
        return False
