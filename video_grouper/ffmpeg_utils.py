import os
import time
import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

ffmpeg_lock = asyncio.Lock()

def get_default_date_format():
    return "%Y-%m-%d %H:%M:%S"

async def get_video_duration(file_path: str) -> Optional[float]:
    """Get the duration of a video file using ffprobe."""
    try:
        cmd = [
            'ffprobe',
            '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            file_path
        ]
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        
        if process.returncode != 0:
            logger.error(f"Error getting video duration: {stderr.decode()}")
            return None
            
        return float(stdout.decode().strip())
        
    except Exception as e:
        logger.error(f"Error getting video duration: {e}")
        return None

async def verify_mp4_duration(dav_file: str, mp4_file: str, tolerance: float = 0.1) -> bool:
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
            stderr=asyncio.subprocess.DEVNULL
        )
        await process.wait()
    except Exception as e:
        logger.error(f"FFmpeg command failed: {e}")

async def async_convert_file(file_path, latest_file_path, end_time, filename):
    """Convert a single file asynchronously."""
    async with ffmpeg_lock:
        try:
            start_time = time.time()
            
            if not os.path.exists(file_path):
                raise FileNotFoundError(f"Input file not found: {file_path}")
            
            if not os.access(file_path, os.R_OK):
                raise PermissionError(f"Cannot read input file: {file_path}")
                
            mp4_path = file_path.replace('.dav', '.mp4')
            
            output_dir = os.path.dirname(mp4_path)
            if not os.access(output_dir, os.W_OK):
                raise PermissionError(f"Cannot write to output directory: {output_dir}")
            
            command = [
                "ffmpeg", "-i", file_path,
                "-vcodec", "copy", "-acodec", "alac",
                "-threads", "0", "-async", "1",
                "-progress", "pipe:1",
                mp4_path
            ]
            
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            last_logged_percentage = 0
            error_output = []
            
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                    
                line = line.decode().strip()
                if "out_time_ms=" in line:
                    try:
                        time_ms = int(line.split("=")[1])
                        percentage = min(100, int((time_ms / 1000000) * 100))
                        if percentage > last_logged_percentage + 9:
                            logger.info(f"Converting {filename}: {percentage}%")
                            last_logged_percentage = percentage
                    except (ValueError, IndexError):
                        pass
            
            await process.wait()
            
            if process.returncode != 0:
                error = await process.stderr.read()
                error_output.append(error.decode())
                raise Exception(f"FFmpeg conversion failed: {''.join(error_output)}")
            
            # Update latest video file
            with open(latest_file_path, "w") as f:
                f.write(end_time.strftime(get_default_date_format()))
            
            duration = time.time() - start_time
            logger.info(f"Converted {filename} in {duration:.2f} seconds")
            
        except Exception as e:
            logger.error(f"Error converting {filename}: {e}")
            raise 