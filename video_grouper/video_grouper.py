import os
import re
import subprocess
import sys
import httpx
import asyncio
import logging
from datetime import datetime, timedelta
import aiofiles
import configparser
import json
from typing import List, Tuple
import time

# Configure logging
logging.basicConfig(
    level=logging.INFO,  # Change to DEBUG for more verbose logs
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)  # Ensure logs are written to stdout
    ]
)
logger = logging.getLogger(__name__)

# Load configuration
config = configparser.ConfigParser()
config.read("config.ini")

cameraConfig = config["CAMERA"]
DEVICE_IP = cameraConfig["ip_address"]
AUTH_USERNAME = cameraConfig["username"]
AUTH_PASSWORD = cameraConfig["password"]

default_date_format = "%Y-%m-%d %H:%M:%S"
LATEST_VIDEO_FILE = "latest_video.txt"
STATUS_FILE = "processing_status.txt"
MATCH_INFO_TEMPLATE = "match_info.ini.dist"
MATCH_INFO_FILE = "match_info.ini"
CAMERA_STATE_FILE = "camera_state.json"

STATES = ["downloading", "combining", "user_input", "post_processing", "finished"]

# Locks to ensure only one download at a time
download_lock = asyncio.Lock()

# Global queues for different tasks
download_queue = asyncio.Queue()
ffmpeg_queue = asyncio.Queue()

class RecordingFile:
    def __init__(self, start_time: datetime, end_time: datetime, file_path: str):
        self.start_time = start_time
        self.end_time = end_time
        self.file_path = file_path
    
    @classmethod
    def from_response(cls, response_text: str) -> list["RecordingFile"]:
        files = []
        lines = response_text.split("\n")
        current_file = {}

        for line in lines:
            if line.startswith("items["):
                key, value = line.split("=")
                key = key.strip()
                value = value.strip()
                
                if ".StartTime" in key:
                    current_file["start_time"] = datetime.strptime(value, default_date_format)
                elif ".EndTime" in key:
                    current_file["end_time"] = datetime.strptime(value, default_date_format)
                elif ".FilePath" in key:
                    current_file["file_path"] = value
                
                if len(current_file) == 3:  # If we have all necessary values
                    files.append(cls(current_file["start_time"], current_file["end_time"], current_file["file_path"]))
                    current_file = {}  # Reset for next file entry

        return sorted(files, key=lambda x: x.start_time)

def update_status(directory, state):
    if state not in STATES:
        raise ValueError("Invalid state")
    status_file = os.path.join(directory, STATUS_FILE)
    
    with open(status_file, "w") as f:
        f.write(state)

    logger.info(f"Updated status to {state} in {status_file}")

def get_status(directory):
    status_file = os.path.join(directory, STATUS_FILE)
    if os.path.exists(status_file):
        with open(status_file, "r") as f:
            return f.read().strip()
    return None

def parse_match_info(file_path):
    match_info_config = configparser.ConfigParser()
    match_info_config.read(file_path)
    return match_info_config["MATCH"] if "MATCH" in match_info_config else None

def create_directory(path):
    os.makedirs(path, exist_ok=True)

async def make_http_request(url: str, auth: httpx.DigestAuth):
    async with httpx.AsyncClient() as client:
        return await client.get(url, auth=auth)

async def run_ffmpeg(command):
    try:
        # Run ffmpeg in a separate process to avoid blocking
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await process.wait()
    except Exception as e:
        logger.error(f"FFmpeg command failed: {e}")

async def convert_single_dav_to_mp4(input_file_path: str) -> str:
    """Convert a single DAV file to MP4 format and delete the original DAV file.
    
    Args:
        input_file_path: Path to the input DAV file
        
    Returns:
        Path to the converted MP4 file
    """
    output_file_path = input_file_path.replace(".dav", ".mp4")
    if not os.path.exists(output_file_path):
        command = ["ffmpeg", "-i", input_file_path, "-vcodec", "copy", "-acodec", "alac", "-threads", "0", "-async", "1", output_file_path]
        await run_ffmpeg(command)
        
        # Delete the DAV file after successful conversion
        try:
            os.remove(input_file_path)
        except Exception as e:
            logger.error(f"Error deleting DAV file {input_file_path}: {e}")
            
    return output_file_path

async def convert_davs_to_mp4(directory_path):
    """Convert all DAV files in a directory to MP4 format."""
    for input_file in os.listdir(directory_path):
        if input_file.endswith(".dav"):
            input_file_path = os.path.join(directory_path, input_file)
            await convert_single_dav_to_mp4(input_file_path)

def all_fields_filled(match_info):
    required_fields = ["start_time_offset", "my_team_name", "opponent_team_name", "location"]
    return all(match_info.get(field) for field in required_fields)

async def concatenate_videos(directory):
    output_file = os.path.join(directory, "combined.mp4")
    list_file = os.path.join(directory, "video_list.txt")
    
    # Check for existing combined file and remove if invalid
    if os.path.exists(output_file):
        if os.path.getsize(output_file) == 0:
            logger.info(f"Found empty combined.mp4 file, removing it")
            os.remove(output_file)
        else:
            # Try to get duration to verify file is valid
            try:
                duration = await get_video_duration(output_file)
                if duration <= 0:
                    logger.info(f"Found invalid combined.mp4 file, removing it")
                    os.remove(output_file)
            except Exception as e:
                logger.info(f"Found corrupted combined.mp4 file, removing it: {e}")
                os.remove(output_file)
    
    # Check for any unconverted DAV files
    dav_files = [f for f in os.listdir(directory) if f.endswith('.dav')]
    if dav_files:
        logger.info(f"Found {len(dav_files)} unconverted DAV files in {directory}, waiting for conversion to complete")
        return
    
    # Get list of MP4 files and their sizes
    mp4_files = []
    total_size = 0
    for file in sorted(os.listdir(directory)):
        if file.endswith(".mp4"):
            file_path = os.path.join(directory, file)
            file_size = os.path.getsize(file_path)
            mp4_files.append((file, file_size))
            total_size += file_size
    
    if not mp4_files:
        logger.error(f"No MP4 files found in {directory}")
        raise ValueError(f"No MP4 files found in {directory}")
    
    logger.info(f"Found {len(mp4_files)} files to combine (total size: {await format_size(total_size)})")
    
    # Write file list
    with open(list_file, "w") as f:
        for file, _ in mp4_files:
            f.write(f"file '{os.path.join(directory, file)}'\n")

    if not os.path.exists(list_file):
        logger.error(f"Unable to combine videos: missing {list_file}")
        raise ValueError(f"Missing {list_file}")

    logger.info(f"Combining videos in {directory}")
    command = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", 
        "-i", list_file, "-c", "copy",
        "-progress", "pipe:1",  # Output progress to stdout
        output_file
    ]
    
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    
    # Track progress
    last_logged_percentage = 0
    start_time = time.time()
    
    while True:
        line = await process.stdout.readline()
        if not line:
            break
            
        line = line.decode().strip()
        if line.startswith('out_time_ms='):
            # Extract time in microseconds
            time_ms = int(line.split('=')[1])
            # Convert to seconds
            time_sec = time_ms / 1000000
            
            # Get the total duration of all input files
            total_duration = 0
            for file, _ in mp4_files:
                file_path = os.path.join(directory, file)
                duration = await get_video_duration(file_path)
                total_duration += duration
            
            if total_duration > 0:
                progress = (time_sec / total_duration) * 100
                # Log only at 10% intervals
                current_percentage = int(progress / 10) * 10
                if current_percentage > last_logged_percentage:
                    elapsed_time = time.time() - start_time
                    minutes = int(elapsed_time // 60)
                    seconds = int(elapsed_time % 60)
                    logger.info(f"Combining videos: {current_percentage}% (elapsed time: {minutes}m {seconds}s)")
                    last_logged_percentage = current_percentage
    
    # Wait for the process to complete
    await process.wait()
    
    if process.returncode == 0:
        # Verify the output file exists and has content
        if not os.path.exists(output_file):
            raise FileNotFoundError(f"Combination completed but output file not found: {output_file}")
        
        if os.path.getsize(output_file) == 0:
            raise ValueError(f"Combination completed but output file is empty: {output_file}")
            
        # Verify the combined file is valid by checking its duration
        try:
            duration = await get_video_duration(output_file)
            if duration <= 0:
                logger.error("Combined file is invalid (zero duration)")
                os.remove(output_file)
                raise ValueError("Combined file is invalid (zero duration)")
        except Exception as e:
            logger.error(f"Combined file is invalid: {e}")
            os.remove(output_file)
            raise ValueError(f"Combined file is invalid: {e}")
        
        # Calculate and log total combination time
        combination_end_time = time.time()
        combination_duration = combination_end_time - start_time
        minutes = int(combination_duration // 60)
        seconds = int(combination_duration % 60)
        
        logger.info(f"✨ Successfully combined {len(mp4_files)} videos into {os.path.basename(output_file)} (took {minutes}m {seconds}s)")
    else:
        # Collect error output
        error = await process.stderr.read()
        error_msg = error.decode()
        logger.error(f"Error combining videos: {error_msg}")
        
        # Clean up the failed output file if it exists
        if os.path.exists(output_file):
            try:
                os.remove(output_file)
                logger.info(f"Removed failed combined file: {output_file}")
            except Exception as e:
                logger.warning(f"Could not remove failed combined file {output_file}: {e}")
        
        raise RuntimeError(f"FFmpeg combination failed: {error_msg}")

async def process_files():
    while True:
        try:
            storage_path = config["APP"]["video_storage_path"]
            logger.info(f"Processing files in {storage_path}")
            for directory in os.listdir(storage_path):
                full_path = os.path.join(storage_path, directory)
                if not os.path.isdir(full_path):
                    continue
                
                status = get_status(full_path)
                if status == "combining":
                    # Files are already converted to MP4, just need to combine them
                    await concatenate_videos(full_path)
                    match_info_path = os.path.join(full_path, MATCH_INFO_FILE)
                    if not os.path.exists(MATCH_INFO_TEMPLATE):
                        logger.info(f"Missing match info template: {MATCH_INFO_TEMPLATE}")
                        continue

                    if not os.path.exists(match_info_path):
                        with open(MATCH_INFO_TEMPLATE, "r") as template, open(match_info_path, "w") as match_info:
                            match_info.write(template.read())
                    update_status(full_path, "user_input")
                
                elif status == "user_input":
                    match_info = configparser.ConfigParser()
                    match_info_path = os.path.join(full_path, MATCH_INFO_FILE)
                    if not os.path.exists(match_info_path):
                        logger.info(f"No match info file exists in {full_path}")
                        continue

                    match_info.read(match_info_path)
                    if all_fields_filled(match_info["MATCH"]):
                        update_status(full_path, "post_processing")
                    else:
                        logger.info(f"Waiting for match info in {full_path}")

                elif status == "post_processing":
                    match_info_path = os.path.join(full_path, MATCH_INFO_FILE)
                    if not os.path.exists(match_info_path):
                        logger.info(f"Skipping post-processing: {match_info_path} missing")
                        continue
                    
                    match_info = configparser.ConfigParser()
                    match_info.read(match_info_path)
                    if "MATCH" not in match_info:
                        logger.info(f"Skipping post-processing: Invalid {match_info_path}")
                        continue

                    await trim_video(full_path, match_info["MATCH"])
        except Exception as e:
            logger.error(f"Unexpected error processing files: {e}")
        
        await asyncio.sleep(config["APP"].getint("check_interval_seconds"))

def cleanup_dav_files(directory):
    dav_files = [f for f in os.listdir(directory) if f.endswith(".dav")]
    for file in dav_files:
        file_path = os.path.join(directory, file)
        try:
            os.remove(file_path)
        except Exception as e:
            logger.error(f"Error deleting {file}: {e}")
    
    logger.info("Dav file cleanup complete.")

async def trim_video(directory, match_info):
    logger.info("Starting video trimming and renaming process...")
    combined_file = os.path.join(directory, "combined.mp4")
    if not os.path.exists(combined_file):
        logger.info(f"Skipping trim: Missing {combined_file}")
        return

    # Get the total duration of the combined file
    total_duration = await get_video_duration(combined_file)
    if total_duration <= 0:
        logger.error(f"Could not determine duration of {combined_file}")
        return

    dir_date = os.path.basename(directory).split('-')[0]
    formatted_date = datetime.strptime(dir_date, "%Y.%m.%d").strftime("%m-%d-%Y")
    output_dir = os.path.join(directory, f"{dir_date} - {match_info['my_team_name']} vs {match_info['opponent_team_name']} ({str(match_info['location'])})")
    create_directory(output_dir)

    # Validate start_time_offset
    start_time_offset = match_info.get("start_time_offset", "").strip()
    if not start_time_offset:
        logger.info(f"Skipping trim: Missing start_time_offset in {directory}")
        return

    output_file = os.path.join(
        output_dir,
        f"{match_info['my_team_name'].lower().replace(' ', '')}-"
        f"{match_info['opponent_team_name'].lower().replace(' ', '')}-"
        f"{match_info['location'].lower().replace(' ', '')}-{formatted_date}-raw.mp4"
    )

    logger.info(f"Trimming {os.path.basename(combined_file)} starting at {start_time_offset}")
    logger.info(f"Output will be saved to: {output_file}")
    
    command = [
        "ffmpeg", "-y", "-i", combined_file,
        "-ss", start_time_offset,
        "-c", "copy",
        "-progress", "pipe:1",  # Output progress to stdout
        output_file
    ]
    
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    
    # Track progress
    last_logged_percentage = 0
    start_time = time.time()
    
    while True:
        line = await process.stdout.readline()
        if not line:
            break
            
        line = line.decode().strip()
        if line.startswith('out_time_ms='):
            # Extract time in microseconds
            time_ms = int(line.split('=')[1])
            # Convert to seconds
            time_sec = time_ms / 1000000
            
            if total_duration > 0:
                progress = (time_sec / total_duration) * 100
                # Log only at 10% intervals
                current_percentage = int(progress / 10) * 10
                if current_percentage > last_logged_percentage:
                    elapsed_time = time.time() - start_time
                    minutes = int(elapsed_time // 60)
                    seconds = int(elapsed_time % 60)
                    logger.info(f"Trimming video: {current_percentage}% (elapsed time: {minutes}m {seconds}s)")
                    last_logged_percentage = current_percentage
    
    # Wait for the process to complete
    await process.wait()
    
    if process.returncode == 0:
        # Verify the output file exists and has content
        if not os.path.exists(output_file):
            raise FileNotFoundError(f"Trimming completed but output file not found: {output_file}")
        
        if os.path.getsize(output_file) == 0:
            raise ValueError(f"Trimming completed but output file is empty: {output_file}")
        
        # Calculate and log total trimming time
        trim_end_time = time.time()
        trim_duration = trim_end_time - start_time
        minutes = int(trim_duration // 60)
        seconds = int(trim_duration % 60)
        
        logger.info(f"✨ Successfully trimmed video to {os.path.basename(output_file)} (took {minutes}m {seconds}s)")
        
        # Clean up DAV files
        logger.info("Cleaning up DAV files...")
        cleanup_dav_files(directory)
        
        # Update status
        update_status(directory, "finished")
        logger.info(f"✅ Processing complete for {directory}")
    else:
        # Collect error output
        error = await process.stderr.read()
        error_msg = error.decode()
        logger.error(f"Error trimming video: {error_msg}")
        raise RuntimeError(f"FFmpeg trimming failed: {error_msg}")

def load_camera_state():
    """Load the camera state from file."""
    state_file = os.path.join(config["APP"]["video_storage_path"], CAMERA_STATE_FILE)
    if os.path.exists(state_file):
        try:
            with open(state_file, 'r') as f:
                state = json.load(f)
                return {
                    'connection_events': [(datetime.fromisoformat(event['time']), event['type']) 
                                   for event in state.get('connection_events', [])],
                    'is_connected': state.get('is_connected', False)
                }
        except Exception as e:
            logger.error(f"Error loading camera state: {e}")
    return {
        'connection_events': [],
        'is_connected': False
    }

def save_camera_state(state):
    """Save the camera state to file."""
    state_file = os.path.join(config["APP"]["video_storage_path"], CAMERA_STATE_FILE)
    try:
        with open(state_file, 'w') as f:
            json.dump({
                'connection_events': [{'time': time.isoformat(), 'type': event_type} 
                               for time, event_type in state['connection_events']],
                'is_connected': state.get('is_connected', False)
            }, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving camera state: {e}")

async def check_device_availability(auth) -> bool:
    try:
        device_check_url = f"http://{DEVICE_IP}/cgi-bin/recordManager.cgi?action=getCaps"
        logger.info(f"Checking for camera devices available on network: {device_check_url}")
        response = await make_http_request(device_check_url, auth=auth)
        
        current_state = load_camera_state()
        is_available = response.status_code == 200
        
        # Handle state transitions
        if is_available and not current_state['is_connected']:
            # Camera just connected
            # If we have a previous connected event without a disconnected event, remove it
            if current_state['connection_events'] and current_state['connection_events'][-1][1] == 'connected':
                current_state['connection_events'].pop()
                logger.info("Removed orphaned connected event")
            
            current_state['connection_events'].append((datetime.now(), 'connected'))
            current_state['is_connected'] = True
            logger.info(f"Camera connected at {current_state['connection_events'][-1][0]}")
            save_camera_state(current_state)
        elif not is_available and current_state['is_connected']:
            # Camera just disconnected
            current_state['connection_events'].append((datetime.now(), 'disconnected'))
            current_state['is_connected'] = False
            logger.info(f"Camera disconnected at {current_state['connection_events'][-1][0]}")
            save_camera_state(current_state)
        
        if is_available:
            logger.info("Camera is available")
            return True
        else:
            logger.info(f"Camera is not available. Status Code: {response.status_code}")
            return False
    except Exception as e:
        logger.info(f"Camera device was not found at {DEVICE_IP}: {e}")
        return False

async def shutdown_handler():
    """Handle graceful shutdown by adding a disconnected event if the camera was connected."""
    try:
        current_state = load_camera_state()
        if current_state['is_connected']:
            current_state['connection_events'].append((datetime.now(), 'disconnected'))
            current_state['is_connected'] = False
            logger.info(f"Adding disconnected event during shutdown at {current_state['connection_events'][-1][0]}")
            save_camera_state(current_state)
    except Exception as e:
        logger.error(f"Error during shutdown: {e}")

def get_subdirectory_time_ranges(storage_path):
    time_ranges = []

    # Regex pattern to match subdirectory names (YYYY.MM.DD)
    dir_pattern = re.compile(r"(\d{4})\.(\d{2})\.(\d{2})")

    # Regex pattern to match filenames with time ranges (HH.MM.SS-HH.MM.SS)
    file_pattern = re.compile(r"(\d{2})\.(\d{2})\.(\d{2})-(\d{2})\.(\d{2})\.(\d{2})")

    for dirname in sorted(os.listdir(storage_path)):  # Ensure directories are processed in order
        dir_match = dir_pattern.match(dirname)
        if not dir_match:
            continue  # Skip non-matching directories

        year, month, day = map(int, dir_match.groups())
        dir_path = os.path.join(storage_path, dirname)

        if not os.path.isdir(dir_path):
            continue  # Skip files, only process directories

        start_time = None
        end_time = None

        for filename in sorted(os.listdir(dir_path)):  # Ensure files are processed in order
            file_match = file_pattern.match(filename)
            if not file_match:
                continue  # Skip non-matching files

            # Extract times from filename
            start_h, start_m, start_s, end_h, end_m, end_s = map(int, file_match.groups())

            # Convert to datetime for proper sorting
            file_start_time = datetime(year, month, day, start_h, start_m, start_s)
            file_end_time = datetime(year, month, day, end_h, end_m, end_s)

            # Update start and end times
            if start_time is None or file_start_time < start_time:
                start_time = file_start_time
            if end_time is None or file_end_time > end_time:
                end_time = file_end_time

        if start_time and end_time:
            time_ranges.append((start_time, end_time, dirname))

    return time_ranges

async def stop_recording(auth) -> bool:
    try:
        stop_url = f"http://{DEVICE_IP}/cgi-bin/configManager.cgi?action=setConfig&RecordMode[0].Mode=2"
        response = await make_http_request(stop_url, auth=auth)
        if response.status_code == 200 and response.text.strip() == "OK":
            logger.info("Successfully stopped recording")
            return True
        else:
            logger.error(f"Failed to stop recording. Status code: {response.status_code}, Response: {response.text}")
            return False
    except Exception as e:
        logger.error(f"Error stopping recording: {e}")
        return False

async def verify_file_complete(file_path: str, server_path: str) -> bool:
    """Verify if a file download is complete by checking its size against the server.
    If the DAV file is missing but an MP4 exists with the same base name, consider it complete."""
    try:
        # If the file is a DAV file and it's missing, check for MP4
        if file_path.endswith('.dav'):
            mp4_path = file_path.replace('.dav', '.mp4')
            if not os.path.exists(file_path) and os.path.exists(mp4_path):
                logger.info(f"Found completed MP4 file for {file_path}")
                return True

        # Get file size from server
        async with httpx.AsyncClient() as client:
            response = await client.head(f"http://{DEVICE_IP}/cgi-bin/RPC_Loadfile{server_path}", auth=httpx.DigestAuth(AUTH_USERNAME, AUTH_PASSWORD))
            if response.status_code == 200:
                server_size = int(response.headers.get('content-length', 0))
                if not os.path.exists(file_path):
                    return False
                local_size = os.path.getsize(file_path)
                return local_size == server_size
    except Exception as e:
        logger.error(f"Error verifying file completion: {e}")
    return False

def delete_incomplete_file(file_path: str):
    """Delete an incomplete file."""
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
            logger.info(f"Deleted incomplete file: {file_path}")
    except Exception as e:
        logger.error(f"Error deleting incomplete file {file_path}: {e}")

async def format_size(size_bytes: int) -> str:
    """Format size in bytes to human readable format."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f}{unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f}TB"

async def download_with_progress(client: httpx.AsyncClient, url: str, file_path: str, auth: httpx.DigestAuth, total_size: int, directory: str = None):
    """Download a file with progress tracking."""
    filename = os.path.basename(file_path)
    dir_name = os.path.basename(directory) if directory else "unknown directory"
    
    async with client.stream("GET", url, auth=auth, timeout=1200.0, follow_redirects=True) as response:
        if response.status_code != 200:
            raise httpx.HTTPStatusError(f"HTTP {response.status_code}", request=response.request, response=response)
        
        downloaded = 0
        last_log_time = 0
        last_log_size = 0
        async with aiofiles.open(file_path, "wb") as f:
            async for chunk in response.aiter_bytes():
                await f.write(chunk)
                downloaded += len(chunk)
                
                # Log progress every second
                current_time = time.time()
                if current_time - last_log_time >= 1.0:
                    # Calculate speed
                    speed = (downloaded - last_log_size) / (current_time - last_log_time)
                    speed_str = await format_size(speed) + "/s"
                    
                    # Calculate progress
                    progress = (downloaded / total_size) * 100
                    downloaded_str = await format_size(downloaded)
                    total_str = await format_size(total_size)
                    
                    # Create progress bar
                    bar_length = 20
                    filled_length = int(bar_length * downloaded / total_size)
                    bar = '█' * filled_length + '░' * (bar_length - filled_length)
                    
                    # Log with progress bar
                    logger.info(f"Downloading {filename} to {dir_name}: [{bar}] {progress:.1f}% ({downloaded_str}/{total_str}) @ {speed_str}")
                    
                    last_log_time = current_time
                    last_log_size = downloaded

async def process_ffmpeg_queue():
    """Process ffmpeg conversion tasks in the background."""
    while True:
        try:
            file_path, latest_file_path, end_time = await ffmpeg_queue.get()
            filename = os.path.basename(file_path)
            logger.info(f"🔄 Converting {filename}")
            
            # Create a task for the conversion without waiting
            asyncio.create_task(async_convert_file(file_path, latest_file_path, end_time, filename))
            
            ffmpeg_queue.task_done()
        except Exception as e:
            logger.error(f"Error in ffmpeg queue: {e}")
        await asyncio.sleep(0.1)

async def async_convert_file(file_path, latest_file_path, end_time, filename):
    """Convert a single file asynchronously."""
    try:
        # Start timing
        start_time = time.time()
        
        # Verify input file exists and is readable
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Input file not found: {file_path}")
        
        if not os.access(file_path, os.R_OK):
            raise PermissionError(f"Cannot read input file: {file_path}")
            
        mp4_path = file_path.replace('.dav', '.mp4')
        
        # Check if output directory is writable
        output_dir = os.path.dirname(mp4_path)
        if not os.access(output_dir, os.W_OK):
            raise PermissionError(f"Cannot write to output directory: {output_dir}")
        
        # Run ffmpeg with progress monitoring
        command = [
            "ffmpeg", "-i", file_path,
            "-vcodec", "copy", "-acodec", "alac",
            "-threads", "0", "-async", "1",
            "-progress", "pipe:1",  # Output progress to stdout
            mp4_path
        ]
        
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        # Track last logged percentage
        last_logged_percentage = 0
        error_output = []
        
        # Monitor conversion progress
        while True:
            line = await process.stdout.readline()
            if not line:
                break
                
            line = line.decode().strip()
            if line.startswith('out_time_ms='):
                # Extract time in microseconds
                time_ms = int(line.split('=')[1])
                # Convert to seconds
                time_sec = time_ms / 1000000
                
                # Get the video duration using ffprobe
                duration = await get_video_duration(file_path)
                if duration > 0:
                    progress = (time_sec / duration) * 100
                    # Log only at 10% intervals
                    current_percentage = int(progress / 10) * 10
                    if current_percentage > last_logged_percentage:
                        logger.info(f"Converting {filename}: {current_percentage}%")
                        last_logged_percentage = current_percentage
        
        # Wait for the process to complete
        await process.wait()
        
        if process.returncode == 0:
            # Verify the output file exists and has content
            if not os.path.exists(mp4_path):
                raise FileNotFoundError(f"Conversion completed but output file not found: {mp4_path}")
            
            if os.path.getsize(mp4_path) == 0:
                raise ValueError(f"Conversion completed but output file is empty: {mp4_path}")
            
            # Calculate and log total conversion time
            conversion_end_time = time.time()
            conversion_duration = conversion_end_time - start_time
            minutes = int(conversion_duration // 60)
            seconds = int(conversion_duration % 60)
            
            mp4_filename = os.path.basename(mp4_path)
            directory = os.path.basename(os.path.dirname(mp4_path))
            logger.info(f"✨ {mp4_filename} in {directory} (conversion took {minutes}m {seconds}s)")
            
            # Update latest_video.txt with the end time of the processed video
            try:
                timestamp = end_time.strftime(default_date_format)
                if not timestamp:
                    raise ValueError("Generated timestamp is empty")
                    
                # Create a temporary file first
                temp_file = latest_file_path + ".tmp"
                with open(temp_file, "w") as latest_file:
                    latest_file.write(timestamp)
                
                # Verify the temp file has content
                with open(temp_file, "r") as f:
                    if not f.read().strip():
                        raise ValueError("Temporary file is empty after write")
                
                # If everything is good, rename the temp file to the actual file
                os.replace(temp_file, latest_file_path)
                logger.info(f"Updated latest_video.txt with timestamp: {timestamp}")
                
            except Exception as e:
                logger.error(f"Error updating latest_video.txt: {e}")
                if os.path.exists(temp_file):
                    os.remove(temp_file)
                raise
                
            # Clean up the original DAV file only after successful conversion
            try:
                os.remove(file_path)
                logger.info(f"Removed original file: {file_path}")
            except Exception as e:
                logger.warning(f"Could not remove original file {file_path}: {e}")
                
        else:
            # Collect error output
            error = await process.stderr.read()
            error_msg = error.decode()
            logger.error(f"Error converting {filename}: {error_msg}")
            
            # Clean up partial output file if it exists
            if os.path.exists(mp4_path):
                try:
                    os.remove(mp4_path)
                    logger.info(f"Removed incomplete output file: {mp4_path}")
                except Exception as e:
                    logger.warning(f"Could not remove incomplete output file {mp4_path}: {e}")
            
            raise RuntimeError(f"FFmpeg conversion failed: {error_msg}")
            
    except FileNotFoundError as e:
        logger.error(f"File error during conversion of {filename}: {e}")
        raise
    except PermissionError as e:
        logger.error(f"Permission error during conversion of {filename}: {e}")
        raise
    except ValueError as e:
        logger.error(f"Invalid data during conversion of {filename}: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error converting file {filename}: {e}")
        raise

class DirectoryPlan:
    def __init__(self, directory_path: str):
        self.directory_path = directory_path
        self.expected_files = []  # List of RecordingFile objects
        self.processed_files = set()  # Set of filenames that have been processed
        self.status = "pending"  # pending, downloading, combining, user_input, post_processing, finished

    def add_file(self, file: RecordingFile):
        self.expected_files.append(file)
        # Sort files by start time
        self.expected_files.sort(key=lambda x: x.start_time)

    def is_complete(self) -> bool:
        return len(self.processed_files) == len(self.expected_files)

    def get_next_file(self) -> RecordingFile:
        for file in self.expected_files:
            if os.path.basename(file.file_path) not in self.processed_files:
                return file
        return None

    def mark_file_processed(self, filename: str):
        self.processed_files.add(filename)

    def to_dict(self):
        return {
            'directory_path': self.directory_path,
            'expected_files': [os.path.basename(f.file_path).replace('.dav', '.mp4') for f in self.expected_files],
            'processed_files': list(self.processed_files),
            'status': self.status
        }

    @classmethod
    def from_dict(cls, data):
        plan = cls(data['directory_path'])
        plan.processed_files = set(data['processed_files'])
        plan.status = data['status']
        return plan

async def verify_mp4_duration(dav_path: str, mp4_path: str) -> bool:
    """Verify that the MP4 file has roughly the same duration as the DAV file."""
    try:
        # Get DAV duration
        dav_duration = await get_video_duration(dav_path)
        if dav_duration <= 0:
            logger.warning(f"Could not get duration for DAV file: {dav_path}")
            return False
            
        # Get MP4 duration
        mp4_duration = await get_video_duration(mp4_path)
        if mp4_duration <= 0:
            logger.warning(f"Could not get duration for MP4 file: {mp4_path}")
            return False
            
        # Allow for 1 second difference to account for rounding
        duration_diff = abs(dav_duration - mp4_duration)
        if duration_diff > 1:
            logger.warning(f"Duration mismatch: DAV={dav_duration:.1f}s, MP4={mp4_duration:.1f}s, diff={duration_diff:.1f}s")
            return False
            
        return True
    except Exception as e:
        logger.error(f"Error verifying MP4 duration: {e}")
        return False

async def find_and_download_files(auth):
    async with download_lock:
        if not await check_device_availability(auth):
            logger.info("Camera is not available - skipping download")
            return

        # Get the current time window
        current_time = datetime.now()
        window_start = current_time.replace(hour=0, minute=0, second=0, microsecond=0)
        window_end = current_time

        # Get the camera's connection events
        camera_state = load_camera_state()
        connection_events = camera_state['connection_events']
        
        # Get the latest processed video time
        latest_file_path = os.path.join(config["APP"]["video_storage_path"], LATEST_VIDEO_FILE)
        if os.path.exists(latest_file_path):
            try:
                with open(latest_file_path, "r") as latest_file:
                    latest_video_timestamp = latest_file.read().strip()
                    if latest_video_timestamp:  # Only parse if we have content
                        window_start = datetime.strptime(latest_video_timestamp, default_date_format)
                    else:
                        logger.info("latest_video.txt is empty, using start of day as window start")
            except Exception as e:
                logger.warning(f"Error reading latest_video.txt: {e}, using start of day as window start")

        # Query for all files since latest_video.txt
        start_time_formatted = window_start.strftime("%Y-%m-%d%%20%H:%M:%S")
        end_time_formatted = window_end.strftime("%Y-%m-%d%%20%H:%M:%S")

        logger.info(f"Searching for files between {window_start} and {window_end}")

        # Get all files from the API
        response = await make_http_request(f"http://{DEVICE_IP}/cgi-bin/mediaFileFind.cgi?action=factory.create", auth=auth)
        if response.status_code != 200:
            logger.info("Failed to create media file finder factory.")
            return

        object_id = response.text.split('=')[1].strip()

        findfile_url = f"http://{DEVICE_IP}/cgi-bin/mediaFileFind.cgi?action=findFile&object={object_id}&condition.Channel=1&condition.Types[0]=dav&condition.StartTime={start_time_formatted}&condition.EndTime={end_time_formatted}&condition.VideoStream=Main"
        response = await make_http_request(findfile_url, auth=auth)
        if response.status_code != 200:
            logger.info("Failed to find media files.")
            return

        response = await make_http_request(f"http://{DEVICE_IP}/cgi-bin/mediaFileFind.cgi?action=findNextFile&object={object_id}&count=100", auth=auth)
        if response.status_code != 200:
            logger.info("Failed to retrieve media file list.")
            return

        # Write the API response to a file for debugging
        debug_file = os.path.join(config["APP"]["video_storage_path"], "camera_api_response.txt")
        try:
            with open(debug_file, "w") as f:
                f.write(f"API Response at {datetime.now()}:\n")
                f.write(f"URL: {findfile_url}\n")
                f.write("Response:\n")
                f.write(response.text)
            logger.info(f"Wrote API response to {debug_file}")
        except Exception as e:
            logger.error(f"Error writing API response to file: {e}")

        files = RecordingFile.from_response(response.text)
        if not files:
            logger.info("No new files found")
            return

        # Filter out files that were recorded during connection periods
        filtered_files = []
        for file in files:
            # Check if file was recorded during any connection period
            skip_file = False
            for i in range(0, len(connection_events), 2):
                if i + 1 >= len(connection_events):
                    # Last connected event without a matching disconnected
                    connected_time = connection_events[i][0]
                    disconnected_time = current_time
                else:
                    connected_time = connection_events[i][0]
                    disconnected_time = connection_events[i + 1][0]

                # If file was recorded during this connection period, skip it
                if connected_time <= file.start_time <= disconnected_time:
                    logger.info(f"Skipping {file.file_path} (recorded during connection period {connected_time} to {disconnected_time})")
                    skip_file = True
                    break

            if not skip_file:
                filtered_files.append(file)

        if not filtered_files:
            logger.info("No files to process after filtering connection periods")
            return

        # Create directory plans
        storage_path = config["APP"]["video_storage_path"]
        directory_plans = {}  # directory_path -> DirectoryPlan
        current_plan = None

        for file in filtered_files:
            # Extract just the filename from the server path
            filename = os.path.basename(file.file_path)
            
            # If we don't have a current plan or the file is too far from the last file in the current plan
            if (current_plan is None or 
                (current_plan.expected_files and 
                 (file.start_time - current_plan.expected_files[-1].end_time).total_seconds() > 60)):
                # Create new plan
                dir_name = file.start_time.strftime("%Y.%m.%d-%H.%M.%S")
                dir_path = os.path.join(storage_path, dir_name)
                current_plan = DirectoryPlan(dir_path)
                directory_plans[dir_path] = current_plan
                create_directory(dir_path)
                logger.info(f"📁 Created new directory plan: {dir_path}")

            # Add file to current plan
            current_plan.add_file(file)
            logger.info(f"📝 Added {filename} to plan for {current_plan.directory_path}")

        # Save the plans
        plans_file = os.path.join(storage_path, "directory_plans.json")
        with open(plans_file, "w") as f:
            json.dump({path: plan.to_dict() for path, plan in directory_plans.items()}, f, indent=2)

        # Process each plan
        for dir_path, plan in directory_plans.items():
            if plan.status == "pending":
                update_status(dir_path, "downloading")
                plan.status = "downloading"

            while not plan.is_complete():
                next_file = plan.get_next_file()
                if not next_file:
                    break

                # Extract just the filename from the server path
                filename = os.path.basename(next_file.file_path)
                full_download_path = os.path.join(dir_path, filename)

                # Skip if MP4 already exists
                mp4_path = full_download_path.replace('.dav', '.mp4')
                if os.path.exists(mp4_path):
                    # Verify the MP4 duration matches the DAV file
                    if await verify_mp4_duration(full_download_path, mp4_path):
                        logger.info(f"🟡 Skipping {filename} (MP4 already exists and verified)")
                        plan.mark_file_processed(filename)
                    else:
                        logger.info(f"🔄 MP4 exists but duration mismatch, will reprocess {filename}")
                        try:
                            os.remove(mp4_path)
                            logger.info(f"Removed invalid MP4 file: {mp4_path}")
                            # Add to ffmpeg queue for reprocessing
                            asyncio.create_task(ffmpeg_queue.put((full_download_path, latest_file_path, next_file.end_time)))
                        except Exception as e:
                            logger.error(f"Could not remove invalid MP4 file {mp4_path}: {e}")
                    continue
                
                # Download if needed
                if not os.path.exists(full_download_path) or not await verify_file_complete(full_download_path, next_file.file_path):
                    if os.path.exists(full_download_path):
                        delete_incomplete_file(full_download_path)
                    
                    try:
                        async with httpx.AsyncClient() as client:
                            # Get file size
                            head_response = await client.head(f"http://{DEVICE_IP}/cgi-bin/RPC_Loadfile{next_file.file_path}", auth=auth)
                            if head_response.status_code != 200:
                                logger.error(f"Failed to get file size: {next_file.file_path}")
                                continue
                                
                            total_size = int(head_response.headers.get('content-length', 0))
                            logger.info(f"Downloading {filename} ({await format_size(total_size)})")
                            
                            try:
                                await download_with_progress(
                                    client,
                                    f"http://{DEVICE_IP}/cgi-bin/RPC_Loadfile{next_file.file_path}",
                                    full_download_path,
                                    auth,
                                    total_size,
                                    dir_path
                                )
                                
                                if await verify_file_complete(full_download_path, next_file.file_path):
                                    logger.info(f"✅ {filename}")
                                    # Add to ffmpeg queue for processing
                                    asyncio.create_task(ffmpeg_queue.put((full_download_path, latest_file_path, next_file.end_time)))
                                    plan.mark_file_processed(filename)
                                else:
                                    logger.error(f"❌ Download incomplete: {next_file.file_path}")
                                    delete_incomplete_file(full_download_path)
                            except Exception as e:
                                logger.error(f"❌ Error downloading {next_file.file_path}: {e}")
                                delete_incomplete_file(full_download_path)
                    except Exception as e:
                        logger.error(f"❌ Error setting up download for {next_file.file_path}: {e}")
                else:
                    logger.info(f"🟡 Skipping {filename} (already exists and complete)")
                    plan.mark_file_processed(filename)

            # If all files in this plan are processed, mark it for combining
            if plan.is_complete():
                logger.info(f"🎉 All files processed for {dir_path}, marking for combining")
                update_status(dir_path, "combining")
                plan.status = "combining"

        # Save updated plans
        with open(plans_file, "w") as f:
            json.dump({path: plan.to_dict() for path, plan in directory_plans.items()}, f, indent=2)

async def download_files():
    auth = httpx.DigestAuth(AUTH_USERNAME, AUTH_PASSWORD)
    while True:
        try:
            logger.info("Checking for new files...")
            if await check_device_availability(auth):
                await find_and_download_files(auth)
        except Exception as e:
            logger.error(f"Unexpected error downloading files: {e}")
        
        await asyncio.sleep(config["APP"].getint("check_interval_seconds"))

async def get_video_duration(file_path: str) -> float:
    """Get the duration of a video file in seconds using ffprobe."""
    try:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            file_path
        ]
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode == 0:
            output = stdout.decode().strip()
            if output == 'N/A':
                # This is expected for some files, not an error
                return 0
            try:
                return float(output)
            except ValueError:
                # Only log if it's not 'N/A'
                if output != 'N/A':
                    logger.error(f"Invalid duration output from ffprobe: {output}")
                return 0
        else:
            # Only log actual errors, not 'N/A' cases
            error_msg = stderr.decode()
            if 'N/A' not in error_msg:
                logger.error(f"Error getting video duration: {error_msg}")
            return 0
    except Exception as e:
        # Only log if it's not related to 'N/A'
        if 'N/A' not in str(e):
            logger.error(f"Error getting video duration: {e}")
        return 0

class FileState:
    def __init__(self, file_path: str, group_dir: str, total_size: int = 0, downloaded_bytes: int = 0, status: str = "pending", start_time: datetime = None, end_time: datetime = None):
        self.file_path = file_path
        self.group_dir = group_dir
        self.total_size = total_size
        self.downloaded_bytes = downloaded_bytes
        self.status = status  # pending, downloading, downloaded, converting, converted, error
        self.last_updated = datetime.now()
        self.error_message = None
        self.mp4_path = file_path.replace('.dav', '.mp4')
        self.start_time = start_time
        self.end_time = end_time

    def to_dict(self):
        return {
            'file_path': self.file_path,
            'group_dir': self.group_dir,
            'total_size': self.total_size,
            'downloaded_bytes': self.downloaded_bytes,
            'status': self.status,
            'last_updated': self.last_updated.isoformat(),
            'error_message': self.error_message,
            'start_time': self.start_time.isoformat() if self.start_time else None,
            'end_time': self.end_time.isoformat() if self.end_time else None
        }

    @classmethod
    def from_dict(cls, data):
        state = cls(
            file_path=data['file_path'],
            group_dir=data['group_dir'],
            total_size=data['total_size'],
            downloaded_bytes=data['downloaded_bytes'],
            status=data['status']
        )
        state.last_updated = datetime.fromisoformat(data['last_updated'])
        state.error_message = data['error_message']
        if data['start_time']:
            state.start_time = datetime.fromisoformat(data['start_time'])
        if data['end_time']:
            state.end_time = datetime.fromisoformat(data['end_time'])
        return state

class ProcessingState:
    def __init__(self, storage_path: str):
        self.storage_path = storage_path
        self.state_file = os.path.join(storage_path, "processing_state.json")
        self.files = {}  # file_path -> FileState
        logger.info(f"Initializing processing state at {self.state_file}")
        self.load_state()

    def load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r') as f:
                    data = json.load(f)
                    for file_path, state_data in data.items():
                        self.files[file_path] = FileState.from_dict(state_data)
                logger.info(f"Loaded state from {self.state_file} with {len(self.files)} files")
            except Exception as e:
                logger.error(f"Error loading state from {self.state_file}: {e}")
        else:
            logger.info(f"No existing state file at {self.state_file}")

    def save_state(self):
        try:
            # Ensure the directory exists
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            
            data = {
                file_path: state.to_dict()
                for file_path, state in self.files.items()
            }
            with open(self.state_file, 'w') as f:
                json.dump(data, f, indent=2)
            logger.info(f"Saved state to {self.state_file} with {len(self.files)} files")
        except Exception as e:
            logger.error(f"Error saving state to {self.state_file}: {e}")

    def update_file_state(self, file_path: str, **kwargs):
        if file_path not in self.files:
            self.files[file_path] = FileState(file_path=file_path, group_dir="")
        
        state = self.files[file_path]
        for key, value in kwargs.items():
            setattr(state, key, value)
        state.last_updated = datetime.now()
        self.save_state()

    def get_pending_files(self):
        return [f for f in self.files.values() if f.status in ["pending", "error"]]

    def get_unconverted_files(self):
        return [f for f in self.files.values() if f.status == "downloaded"]

    def is_file_processed(self, file_path: str) -> bool:
        """Check if a file has been processed (either in state or has MP4)."""
        # If DAV file still exists, it wasn't processed successfully
        if os.path.exists(file_path):
            mp4_path = file_path.replace('.dav', '.mp4')
            if os.path.exists(mp4_path):
                logger.warning(f"Found incomplete conversion: {file_path} still exists, will reprocess")
                try:
                    os.remove(mp4_path)
                    logger.info(f"Removed incomplete MP4 file: {mp4_path}")
                except Exception as e:
                    logger.error(f"Could not remove incomplete MP4 file {mp4_path}: {e}")
            return False
        
        # Check if MP4 exists
        mp4_path = file_path.replace('.dav', '.mp4')
        if os.path.exists(mp4_path):
            # If MP4 exists and DAV is gone, add it to state
            group_dir = os.path.dirname(mp4_path)
            logger.info(f"Found valid MP4 at {mp4_path}, adding to state with group {group_dir}")
            self.update_file_state(
                file_path,
                group_dir=group_dir,
                status="converted"
            )
            # Force a save of the state
            self.save_state()
            return True
        
        return False

def find_group_directory(file_path: str, storage_path: str, processing_state: ProcessingState) -> str:
    """Find an appropriate group directory for a new file based on time proximity.
    Uses the state file to find a file whose end time is within 5 seconds of the new file's start time."""
    filename = os.path.basename(file_path)
    # Extract time from filename
    time_match = re.match(r"(\d{2})\.(\d{2})\.(\d{2})-(\d{2})\.(\d{2})\.(\d{2})", filename)
    if not time_match:
        return os.path.dirname(file_path)

    start_h, start_m, start_s, end_h, end_m, end_s = map(int, time_match.groups())
    file_start_time = datetime.now().replace(hour=start_h, minute=start_m, second=start_s)

    # Look through all files in state to find one whose end time is within 5 seconds
    for state in processing_state.files.values():
        if state.end_time and 0 <= (file_start_time - state.end_time).total_seconds() <= 5:
            logger.info(f"Found matching group directory {state.group_dir} for {filename}")
            return state.group_dir

    # If no suitable directory found, create a new one using the new file's start time
    new_dir = os.path.join(storage_path, file_start_time.strftime("%Y.%m.%d-%H.%M.%S"))
    create_directory(new_dir)
    logger.info(f"Created new group directory {new_dir} for {filename}")
    return new_dir

async def scan_for_unprocessed_files(storage_path: str, processing_state: ProcessingState):
    """Scan for any DAV files that haven't been processed yet.
    Only looks at directories with dates after the latest processed video."""
    latest_file_path = os.path.join(storage_path, LATEST_VIDEO_FILE)
    latest_processed_date = None
    
    if os.path.exists(latest_file_path):
        with open(latest_file_path, "r") as latest_file:
            latest_processed_date = datetime.strptime(latest_file.read().strip(), default_date_format)
    
    # First, scan for any existing MP4 files and add them to state
    logger.info("Scanning for existing MP4 files...")
    for root, _, files in os.walk(storage_path):
        for file in files:
            if file.endswith('.mp4'):
                mp4_path = os.path.join(root, file)
                dav_path = mp4_path.replace('.mp4', '.dav')
                if dav_path not in processing_state.files:
                    logger.info(f"Found existing MP4 at {mp4_path}, adding to state")
                    processing_state.update_file_state(
                        dav_path,
                        group_dir=root,
                        status="converted"
                    )
    
    # Force a save after scanning for MP4s
    processing_state.save_state()
    
    # Now scan for unprocessed DAV files
    logger.info("Scanning for unprocessed DAV files...")
    for root, _, files in os.walk(storage_path):
        # Skip if this directory's date is before or equal to latest processed date
        dir_name = os.path.basename(root)
        if dir_name.count('.') >= 2:  # Check if it's a date-formatted directory
            try:
                dir_date = datetime.strptime(dir_name.split('-')[0], "%Y.%m.%d")
                if latest_processed_date and dir_date <= latest_processed_date:
                    continue
            except ValueError:
                pass  # Not a date-formatted directory, continue processing
        
        for file in files:
            if file.endswith('.dav'):
                file_path = os.path.join(root, file)
                
                # Skip if file is already processed
                if processing_state.is_file_processed(file_path):
                    logger.info(f"Skipping {file} (already processed)")
                    continue
                
                # If DAV exists but not processed, add to processing state
                if file_path not in processing_state.files:
                    # For new files, determine the appropriate group directory
                    group_dir = find_group_directory(file_path, storage_path, processing_state)
                    
                    # Get the start and end times from the API response
                    auth = httpx.DigestAuth(AUTH_USERNAME, AUTH_PASSWORD)
                    try:
                        # Create a media file finder factory
                        response = await make_http_request(f"http://{DEVICE_IP}/cgi-bin/mediaFileFind.cgi?action=factory.create", auth=auth)
                        if response.status_code != 200:
                            logger.error("Failed to create media file finder factory")
                            continue
                            
                        object_id = response.text.split('=')[1].strip()
                        
                        # Get the file info from the API
                        filename = os.path.basename(file_path)
                        date_part = os.path.basename(os.path.dirname(file_path)).split('-')[0]  # Get YYYY.MM.DD
                        hour_part = filename.split('-')[0]  # Get HH.MM.SS
                        server_path = f"/mnt/dvr/mmc1p2_0/{date_part}/0/dav/{hour_part[:2]}/{filename}"
                        
                        # Search for the file
                        start_time = datetime.strptime(date_part, "%Y.%m.%d").strftime("%Y-%m-%d%%20%H:%M:%S")
                        end_time = (datetime.strptime(date_part, "%Y.%m.%d") + timedelta(days=1)).strftime("%Y-%m-%d%%20%H:%M:%S")
                        
                        findfile_url = f"http://{DEVICE_IP}/cgi-bin/mediaFileFind.cgi?action=findFile&object={object_id}&condition.Channel=1&condition.Types[0]=dav&condition.StartTime={start_time}&condition.EndTime={end_time}&condition.VideoStream=Main"
                        response = await make_http_request(findfile_url, auth=auth)
                        if response.status_code != 200:
                            logger.error("Failed to find media files")
                            continue
                            
                        response = await make_http_request(f"http://{DEVICE_IP}/cgi-bin/mediaFileFind.cgi?action=findNextFile&object={object_id}&count=100", auth=auth)
                        if response.status_code != 200:
                            logger.error("Failed to retrieve media file list")
                            continue
                            
                        # Parse the response to get start and end times
                        files = RecordingFile.from_response(response.text)
                        matching_file = next((f for f in files if f.file_path == server_path), None)
                        
                        if matching_file:
                            processing_state.update_file_state(
                                file_path,
                                group_dir=group_dir,
                                status="pending",
                                start_time=matching_file.start_time,
                                end_time=matching_file.end_time
                            )
                            logger.info(f"Found unprocessed file: {file_path} with times {matching_file.start_time} - {matching_file.end_time}")
                        else:
                            logger.error(f"Could not find file info in API response: {file_path}")
                            continue
                            
                    except Exception as e:
                        logger.error(f"Error getting file info from API: {e}")
                        continue
    
    # Force a final save after scanning
    processing_state.save_state()

async def process_pending_files(processing_state: ProcessingState, auth):
    """Process any pending files in the state."""
    latest_file_path = os.path.join(processing_state.storage_path, LATEST_VIDEO_FILE)
    
    while True:
        try:
            # Process pending downloads
            pending_files = processing_state.get_pending_files()
            for file_state in pending_files:
                if file_state.status == "pending":
                    await download_file(file_state, auth, processing_state)
                elif file_state.status == "error":
                    # Retry failed downloads
                    logger.info(f"Retrying failed download: {file_state.file_path}")
                    await download_file(file_state, auth, processing_state)

            # Process unconverted files
            unconverted_files = processing_state.get_unconverted_files()
            for file_state in unconverted_files:
                await convert_single_dav_to_mp4(file_state.file_path)
                processing_state.update_file_state(file_state.file_path, status="converted")
                processing_state.save_state()
                
                # Update latest_video.txt with the end time
                if file_state.end_time:
                    with open(latest_file_path, "w") as latest_file:
                        latest_file.write(file_state.end_time.strftime(default_date_format))

            await asyncio.sleep(1)  # Prevent tight loop
        except Exception as e:
            logger.error(f"Error processing pending files: {e}")
            await asyncio.sleep(5)  # Back off on error

async def download_file(file_state: FileState, auth, processing_state: ProcessingState):
    """Download a file with progress tracking and state management."""
    try:
        processing_state.update_file_state(file_state.file_path, status="downloading")
        
        # Extract the server path from the filename
        filename = os.path.basename(file_state.file_path)
        # Assuming the filename format is like "HH.MM.SS-HH.MM.SS[F][0@0][number].dav"
        # We need to construct the server path
        date_part = os.path.basename(os.path.dirname(file_state.file_path)).split('-')[0]  # Get YYYY.MM.DD
        hour_part = filename.split('-')[0]  # Get HH.MM.SS
        server_path = f"/mnt/dvr/mmc1p2_0/{date_part}/0/dav/{hour_part[:2]}/{filename}"
        
        async with httpx.AsyncClient() as client:
            # Get file size
            head_response = await client.head(f"http://{DEVICE_IP}/cgi-bin/RPC_Loadfile{server_path}", auth=auth)
            if head_response.status_code != 200:
                raise Exception(f"Failed to get file size: {head_response.status_code}")

            total_size = int(head_response.headers.get('content-length', 0))
            processing_state.update_file_state(file_state.file_path, total_size=total_size)

            # Download with progress tracking
            async with client.stream("GET", f"http://{DEVICE_IP}/cgi-bin/RPC_Loadfile{server_path}", 
                                   auth=auth, timeout=1200.0) as response:
                if response.status_code != 200:
                    raise Exception(f"Download failed: {response.status_code}")

                downloaded = 0
                async with aiofiles.open(file_state.file_path, "wb") as f:
                    async for chunk in response.aiter_bytes():
                        await f.write(chunk)
                        downloaded += len(chunk)
                        processing_state.update_file_state(
                            file_state.file_path,
                            downloaded_bytes=downloaded,
                            status="downloading"
                        )

                if downloaded == total_size:
                    processing_state.update_file_state(file_state.file_path, status="downloaded")
                else:
                    raise Exception("Download incomplete")

    except Exception as e:
        logger.error(f"Error downloading {file_state.file_path}: {e}")
        processing_state.update_file_state(
            file_state.file_path,
            status="error",
            error_message=str(e)
        )

async def main():
    try:
        storage_path = config["APP"]["video_storage_path"]
        processing_state = ProcessingState(storage_path)
        
        # Start background tasks
        asyncio.create_task(process_ffmpeg_queue())
        asyncio.create_task(process_pending_files(processing_state, httpx.DigestAuth(AUTH_USERNAME, AUTH_PASSWORD)))
        
        # Initial scan for unprocessed files
        await scan_for_unprocessed_files(storage_path, processing_state)
        
        # Run the main tasks
        await asyncio.gather(
            download_files(),
            process_files()
        )
    except asyncio.CancelledError:
        logger.error("Shutting down gracefully...")
        await shutdown_handler()
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        await shutdown_handler()

if __name__ == "__main__":
    logger.info("Starting video processing...")
    asyncio.run(main())
